from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.vendor_tools import (get_supplier_info, get_market_price_benchmark,
                                list_supplier_ids)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import (GEMINI_PRO_MODEL, SCORE_WEIGHT_DELIVERY, SCORE_WEIGHT_QUALITY,
                    SCORE_WEIGHT_PRICE, classify_supplier_tier)

# Penalty applied per recorded failure when adjusting the base rates.
LATE_DELIVERY_PENALTY = 0.05
QUALITY_FAILURE_PENALTY = 0.05
WALK_AWAY_PENALTY = 0.05
# Score bands that trigger commercial action.
UNDERPERFORMING_THRESHOLD = 0.75
REPLACEMENT_THRESHOLD = 0.65

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
        description="Get detailed supplier information including reliability, delivery rates and past deliveries",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"supplier_id": glm.Schema(type=glm.Type.STRING)},
            required=["supplier_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_market_price_benchmark",
        description="Get the market price range for a SKU, for judging price competitiveness",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
]

PROMPT_TEMPLATE = """Evaluate and score the following suppliers: {supplier_ids}

Recent PO outcomes to factor in: {po_outcomes}

For each supplier:
1. Get their supplier info (reliability_score, on_time_delivery_rate,
   quality_rejection_rate, past_deliveries)
2. Calculate scores:
   - delivery_score = on_time_delivery_rate, minus {late_penalty} for each late past delivery
   - quality_score = 1 - quality_rejection_rate, minus {quality_penalty} for each past
     delivery that failed quality
   - price_score = how their list prices compare to the market benchmark for the SKUs
     they supply (use get_market_price_benchmark), minus {walk_penalty} for each
     negotiation they walked away from
   - overall = delivery_score*{w_delivery} + quality_score*{w_quality} + price_score*{w_price}
3. Classify tier: preferred (>=0.90), approved (0.80-0.89), conditional (0.70-0.79), at_risk (<0.70)
4. Flag suppliers below {underperforming} as underperforming and below {replacement} for replacement

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


class SupplierPerformanceAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
        super().__init__(
            name="supplier_performance",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_supplier_info": get_supplier_info,
                "get_market_price_benchmark": get_market_price_benchmark,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
            offline=offline,
        )

    # ── deterministic engine ─────────────────────────────────
    @staticmethod
    def _price_score(supplier: dict) -> float:
        """Average of market_avg / their price across the SKUs they carry."""
        ratios = []
        for sku_id, pricing in supplier.get("pricing", {}).items():
            benchmark = get_market_price_benchmark(sku_id)
            price = pricing.get("list_price", 0.0)
            if "error" in benchmark or not price:
                continue
            ratios.append(benchmark["market_avg"] / price)
        if not ratios:
            return 0.75
        return max(0.0, min(1.0, sum(ratios) / len(ratios)))

    @staticmethod
    def _recommendation(overall: float, tier: str) -> tuple:
        if overall < REPLACEMENT_THRESHOLD:
            return "review_replacement", "Start qualifying a replacement supplier"
        if overall < UNDERPERFORMING_THRESHOLD:
            return "underperforming", "Put on a performance improvement plan and cap new volume"
        if tier == "preferred":
            return None, "Consolidate volume here and negotiate a longer-term agreement"
        return None, "Maintain current allocation and re-review next cycle"

    def _offline_result(self, task: dict) -> dict:
        supplier_ids = task.get("supplier_ids") or list_supplier_ids()
        walk_aways: dict[str, int] = {}
        for outcome in task.get("po_outcomes", []):
            if isinstance(outcome, dict) and outcome.get("outcome") == "walk_away":
                supplier_id = outcome.get("supplier_id")
                if supplier_id:
                    walk_aways[supplier_id] = walk_aways.get(supplier_id, 0) + 1

        scores = []
        for supplier_id in supplier_ids:
            supplier = get_supplier_info(supplier_id)
            if "error" in supplier:
                continue

            past = supplier.get("past_deliveries", [])
            late = sum(1 for d in past if not d.get("on_time", True))
            quality_failures = sum(1 for d in past if not d.get("quality_ok", True))

            delivery = max(0.0, supplier["on_time_delivery_rate"] - LATE_DELIVERY_PENALTY * late)
            quality = max(0.0, (1 - supplier["quality_rejection_rate"])
                          - QUALITY_FAILURE_PENALTY * quality_failures)
            price = max(0.0, self._price_score(supplier)
                        - WALK_AWAY_PENALTY * walk_aways.get(supplier_id, 0))

            overall = round(delivery * SCORE_WEIGHT_DELIVERY
                            + quality * SCORE_WEIGHT_QUALITY
                            + price * SCORE_WEIGHT_PRICE, 4)
            tier = classify_supplier_tier(overall)
            flag, recommendation = self._recommendation(overall, tier)

            scores.append({
                "supplier_id": supplier_id,
                "supplier_name": supplier["name"],
                "country": supplier["country"],
                "region": supplier["region"],
                "delivery_score": round(delivery, 4),
                "quality_score": round(quality, 4),
                "price_score": round(price, 4),
                "overall_score": overall,
                "tier": tier,
                "flag": flag,
                "late_deliveries": late,
                "quality_failures": quality_failures,
                "recommendation": recommendation,
            })

        scores.sort(key=lambda s: s["overall_score"], reverse=True)
        avg = round(sum(s["overall_score"] for s in scores) / len(scores), 4) if scores else 0.0
        return {
            "supplier_scores": scores,
            "preferred_suppliers": [s["supplier_id"] for s in scores if s["tier"] == "preferred"],
            "at_risk_suppliers": [s["supplier_id"] for s in scores if s["tier"] == "at_risk"],
            "avg_network_score": avg,
        }

    # ── cycle step ───────────────────────────────────────────
    def _persist(self, scores: list) -> list:
        saved = []
        for s in scores:
            supplier_id = s.get("supplier_id")
            if not supplier_id:
                continue
            try:
                delivery = float(s.get("delivery_score", 0.8))
                quality = float(s.get("quality_score", 0.8))
                price = float(s.get("price_score", 0.8))
            except (TypeError, ValueError):
                continue
            self.sqlite.upsert_supplier_score(
                supplier_id=supplier_id, delivery=delivery, quality=quality, price=price
            )
            saved.append(s)
            self._log(f"{supplier_id} ({s.get('supplier_name', '?')}): "
                      f"{float(s.get('overall_score', 0)):.2f} [{s.get('tier', 'approved')}]")
        return saved

    def run(self, task: dict) -> dict:
        supplier_ids = task.get("supplier_ids") or list_supplier_ids()
        task = dict(task, supplier_ids=supplier_ids)
        po_outcomes = task.get("po_outcomes", [])
        self._log(f"Scoring {len(supplier_ids)} suppliers...")
        self.save_state({"status": "running", "supplier_count": len(supplier_ids)})

        prompt = PROMPT_TEMPLATE.format(
            supplier_ids=supplier_ids,
            po_outcomes=po_outcomes,
            late_penalty=LATE_DELIVERY_PENALTY,
            quality_penalty=QUALITY_FAILURE_PENALTY,
            walk_penalty=WALK_AWAY_PENALTY,
            w_delivery=SCORE_WEIGHT_DELIVERY,
            w_quality=SCORE_WEIGHT_QUALITY,
            w_price=SCORE_WEIGHT_PRICE,
            underperforming=UNDERPERFORMING_THRESHOLD,
            replacement=REPLACEMENT_THRESHOLD,
        )
        parsed, raw = self._reason(prompt, task)
        scores = self._persist(parsed.get("supplier_scores", []))

        self._log(f"Supplier scoring complete — {len(scores)} suppliers scored.")
        self.save_state({"status": "completed", "scored": len(scores)})
        return {
            "agent": self.name,
            "scores": scores,
            "preferred_suppliers": parsed.get("preferred_suppliers", []),
            "at_risk_suppliers": parsed.get("at_risk_suppliers", []),
            "avg_network_score": parsed.get("avg_network_score", 0.0),
            "raw_response": raw,
        }
