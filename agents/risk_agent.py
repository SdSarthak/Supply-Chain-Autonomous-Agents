import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.vendor_tools import get_supplier_info, get_alternative_supplier
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_PRO_MODEL, COUNTRY_RISK_SCORES
from datetime import datetime

SYSTEM_PROMPT = """You are a Risk and Resilience Agent for an industrial supply chain company.
Your role is to identify, assess, and mitigate supply chain risks including:
- Supplier concentration risk (too dependent on one supplier)
- Geographic/geopolitical risk (high-risk regions)
- Lead time risk (long lead times in volatile categories)
- Single-source risk (no backup supplier for critical SKUs)
- Active disruption events

Risk severity levels: 1=low, 2=medium, 3=high, 4=critical

For each risk, provide a concrete mitigation recommendation.
Return structured JSON with complete risk assessment."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_supplier_info",
        description="Get supplier details for risk assessment",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"supplier_id": glm.Schema(type=glm.Type.STRING)},
            required=["supplier_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_alternative_supplier",
        description="Find an alternative supplier for a SKU if primary supplier fails",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "sku_id": glm.Schema(type=glm.Type.STRING),
                "exclude_supplier_id": glm.Schema(type=glm.Type.STRING),
            },
            required=["sku_id", "exclude_supplier_id"]
        )
    ),
]

COUNTRY_RISK_SCORES_DESC = {k: f"{v:.0%} risk" for k, v in COUNTRY_RISK_SCORES.items()}


class RiskAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
        super().__init__(
            name="risk",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_supplier_info": get_supplier_info,
                "get_alternative_supplier": get_alternative_supplier,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
        )

    def run(self, task: dict) -> dict:
        active_pos = task.get("active_pos", [])
        supplier_scores = task.get("supplier_scores", [])
        self._log("Running supply chain risk assessment...")
        self.save_state({"status": "running", "started_at": datetime.utcnow().isoformat()})

        country_risk_context = str(COUNTRY_RISK_SCORES_DESC)

        prompt = f"""Perform a comprehensive supply chain risk assessment.

Active purchase orders: {active_pos}
Current supplier scores: {supplier_scores}
Country risk levels: {country_risk_context}

Analysis tasks:
1. For each unique supplier in active POs, get_supplier_info and check:
   - Country risk based on provided country risk levels
   - Reliability score < 0.85 = at-risk
   - Lead time > 20 days = vulnerability
2. Identify any at-risk suppliers and find alternatives using get_alternative_supplier
3. Check for geographic concentration (>50% of POs from one region)
4. Evaluate single-source risk for critical SKUs

Return JSON:
{{
  "overall_risk_level": "low|medium|high|critical",
  "risk_score": <0.0-10.0>,
  "risks": [
    {{
      "risk_id": "R-001",
      "type": "supplier_reliability|geographic|single_source|lead_time|concentration",
      "severity": <1-4>,
      "description": "...",
      "affected_skus": ["SKU-XXX"],
      "affected_supplier": "SUP-XXX",
      "mitigation": "specific action to take",
      "alternative_supplier": null
    }}
  ],
  "critical_actions": ["..."],
  "resilience_recommendations": ["..."],
  "disruptions_logged": <number>
}}"""

        response = self._call_gemini(prompt)
        self._log("Risk assessment complete.")

        risks = []
        try:
            import re, json
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                risks = parsed.get("risks", [])
                critical = [r for r in risks if r.get("severity", 0) >= 3]
                for r in critical:
                    self.sqlite.log_disruption(
                        event_type=r.get("type", "unknown"),
                        region="GLOBAL",
                        severity=r.get("severity", 3),
                        affected_skus=r.get("affected_skus", []),
                        description=r.get("description", "")
                    )
                level = parsed.get("overall_risk_level", "medium")
                self._log(f"Risk level: {level.upper()} | {len(risks)} risks | {len(critical)} critical/high")
        except Exception as e:
            self._log(f"Warning: Could not parse risk JSON: {e}")

        self.save_state({"status": "completed", "risk_count": len(risks)})
        return {"agent": self.name, "risks": risks, "raw_response": response}
