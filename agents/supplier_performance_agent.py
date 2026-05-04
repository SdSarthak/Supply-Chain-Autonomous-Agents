import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.vendor_tools import get_supplier_info
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_PRO_MODEL
from datetime import datetime

SYSTEM_PROMPT = """You are a Supplier Performance Agent for an industrial supply chain company.
Your role is to evaluate and score all suppliers based on their delivery performance,
quality, and pricing competitiveness. You maintain relationship scores over time.

Scoring methodology:
- Delivery score (40% weight): on_time_delivery_rate from supplier data + recent PO performance
- Quality score (35% weight): 1 - quality_rejection_rate, adjusted for recent issues
- Price score (25% weight): competitiveness vs market benchmark

Flag suppliers with overall score < 0.75 as underperforming.
Flag suppliers with overall score < 0.65 for review/replacement.
Return structured JSON with all scores and recommendations."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_supplier_info",
        description="Get detailed supplier information including reliability and delivery rates",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"supplier_id": glm.Schema(type=glm.Type.STRING)},
            required=["supplier_id"]
        )
    ),
]


class SupplierPerformanceAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
        super().__init__(
            name="supplier_performance",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={"get_supplier_info": get_supplier_info},
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
        )

    def run(self, task: dict) -> dict:
        supplier_ids = task.get("supplier_ids",
                                [f"SUP-{str(i).zfill(3)}" for i in range(1, 9)])
        po_outcomes = task.get("po_outcomes", [])
        self._log(f"Scoring {len(supplier_ids)} suppliers...")
        self.save_state({"status": "running", "supplier_count": len(supplier_ids)})

        prompt = f"""Evaluate and score the following suppliers: {supplier_ids}

Recent PO outcomes to factor in: {po_outcomes}

For each supplier:
1. Get their supplier info (reliability_score, on_time_delivery_rate, quality_rejection_rate)
2. Calculate scores:
   - delivery_score = on_time_delivery_rate (adjusted -0.05 for each recent late delivery)
   - quality_score = 1 - quality_rejection_rate (adjusted for recent quality failures)
   - price_score = reliability_score * 0.9 (proxy for value)
   - overall = delivery_score*0.40 + quality_score*0.35 + price_score*0.25
3. Classify tier: preferred (>=0.90), approved (0.80-0.89), conditional (0.70-0.79), at_risk (<0.70)

Return JSON:
{{
  "supplier_scores": [
    {{
      "supplier_id": "...",
      "supplier_name": "...",
      "country": "...",
      "delivery_score": <0.0-1.0>,
      "quality_score": <0.0-1.0>,
      "price_score": <0.0-1.0>,
      "overall_score": <0.0-1.0>,
      "tier": "preferred|approved|conditional|at_risk",
      "flag": null,
      "recommendation": "brief action recommendation"
    }}
  ],
  "preferred_suppliers": ["SUP-XXX"],
  "at_risk_suppliers": ["SUP-XXX"],
  "avg_network_score": <number>
}}"""

        response = self._call_gemini(prompt)
        self._log("Supplier scoring complete.")

        scores = []
        try:
            import re, json
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                for s in parsed.get("supplier_scores", []):
                    self.sqlite.upsert_supplier_score(
                        supplier_id=s["supplier_id"],
                        delivery=float(s.get("delivery_score", 0.8)),
                        quality=float(s.get("quality_score", 0.8)),
                        price=float(s.get("price_score", 0.8))
                    )
                    scores.append(s)
                    tier = s.get("tier", "approved")
                    self._log(f"{s['supplier_id']} ({s.get('supplier_name','?')}): {s.get('overall_score',0):.2f} [{tier}]")
        except Exception as e:
            self._log(f"Warning: Could not parse scores JSON: {e}")

        self.save_state({"status": "completed", "scored": len(scores)})
        return {"agent": self.name, "scores": scores, "raw_response": response}
