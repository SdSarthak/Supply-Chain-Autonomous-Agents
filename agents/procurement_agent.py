import uuid
from datetime import datetime
from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.inventory_tools import get_inventory_by_sku, get_reorder_alerts
from tools.vendor_tools import (get_qualified_suppliers, get_supplier_info,
                                get_market_price_benchmark, get_tier_price)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import (GEMINI_PRO_MODEL, PROCUREMENT_TARGET_FILL, SUPPLIER_MIN_RELIABILITY,
                    SUPPLIER_PREFERRED_RELIABILITY, SUPPLIER_PREFERRED_OTD)

# Weights used to rank qualified suppliers for a line item.
SUPPLIER_SELECTION_WEIGHTS = {
    "reliability": 0.35,
    "on_time": 0.25,
    "price": 0.25,
    "lead_time": 0.15,
}
# Longest lead time considered acceptable when normalising the lead-time score.
LEAD_TIME_CEILING_DAYS = 30

SYSTEM_PROMPT = """You are a Procurement Agent for an industrial supply chain company.
Your role is to make intelligent purchasing decisions based on demand forecasts,
current inventory levels, supplier availability, and budget constraints.

Decision framework:
- Only procure when inventory is below reorder point or demand forecast indicates imminent stockout
- Select suppliers based on reliability score (>= 0.85 preferred), lead time, and price
- Prefer suppliers with reliability_score >= 0.88 and on_time_delivery_rate >= 0.90
- Calculate order quantity to reach ~80% of max_stock minus current available
- Always check multiple suppliers and justify your supplier selection

Return structured JSON with your procurement decisions."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_inventory_by_sku",
        description="Get current inventory levels for a SKU",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_reorder_alerts",
        description="Get all SKUs that need reordering",
        parameters=glm.Schema(type=glm.Type.OBJECT, properties={})
    ),
    glm.FunctionDeclaration(
        name="get_qualified_suppliers",
        description="Get all suppliers who can supply a given SKU with pricing details",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_supplier_info",
        description="Get detailed information about a specific supplier",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"supplier_id": glm.Schema(type=glm.Type.STRING)},
            required=["supplier_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_market_price_benchmark",
        description="Get the market price range for a SKU across all suppliers",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
]

PROMPT_TEMPLATE = """Make procurement decisions based on the following data:

Inventory alerts (SKUs needing reorder): {inventory_alerts}
Demand forecasts: {forecasts}

For each SKU that needs procurement:
1. Check current inventory levels
2. Get qualified suppliers for that SKU
3. Select the best supplier — reliability must be at least {min_reliability}, and prefer
   suppliers at or above {preferred_reliability} reliability with {preferred_otd} on-time delivery
4. Calculate order quantity: target = {target_fill:.0%} of max_stock - current_available,
   raised to cover the 30-day forecast if that is larger, then capped at
   max_stock - current_available so the order still fits in the warehouses
5. Ensure the quantity meets the supplier's min_order_qty and use its volume-tier price
6. Never order the same SKU twice in one cycle

Return a JSON object:
{{
  "procurement_decisions": [
    {{
      "sku_id": "...",
      "sku_name": "...",
      "order_quantity": <number>,
      "selected_supplier_id": "...",
      "selected_supplier_name": "...",
      "target_price": <unit price>,
      "total_estimated_value": <number>,
      "urgency": "normal|urgent|critical",
      "justification": "brief reason for supplier selection",
      "action": "create_po"
    }}
  ],
  "skus_skipped": ["SKU-XXX"],
  "total_procurement_value": <number>
}}"""


class ProcurementAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
        super().__init__(
            name="procurement",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_inventory_by_sku": get_inventory_by_sku,
                "get_reorder_alerts": get_reorder_alerts,
                "get_qualified_suppliers": get_qualified_suppliers,
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
    def _candidate_skus(task: dict) -> list:
        """SKUs to consider this cycle, de-duplicated, alerts first."""
        ordered = []
        for alert in task.get("inventory_alerts", []):
            sku_id = alert.get("sku_id")
            if sku_id and sku_id not in ordered:
                ordered.append(sku_id)
        if not ordered:
            # Nothing handed down — fall back to the live reorder report.
            for alert in get_reorder_alerts()["alerts"]:
                if alert["sku_id"] not in ordered:
                    ordered.append(alert["sku_id"])
        return ordered

    @staticmethod
    def _score_supplier(supplier: dict, quantity: int, market_avg: float) -> float:
        price = get_tier_price(
            {
                "list_price": supplier["list_price"],
                "tier2_qty": supplier.get("tier2_qty"),
                "tier2_price": supplier.get("tier2_price"),
                "tier3_qty": supplier.get("tier3_qty"),
                "tier3_price": supplier.get("tier3_price"),
            },
            quantity,
        )
        price_score = max(0.0, min(1.0, market_avg / price)) if price else 0.0
        lead_score = max(0.0, 1 - supplier["lead_time_days"] / LEAD_TIME_CEILING_DAYS)
        score = (
            supplier["reliability_score"] * SUPPLIER_SELECTION_WEIGHTS["reliability"]
            + supplier["on_time_delivery_rate"] * SUPPLIER_SELECTION_WEIGHTS["on_time"]
            + price_score * SUPPLIER_SELECTION_WEIGHTS["price"]
            + lead_score * SUPPLIER_SELECTION_WEIGHTS["lead_time"]
        )
        if (supplier["reliability_score"] >= SUPPLIER_PREFERRED_RELIABILITY
                and supplier["on_time_delivery_rate"] >= SUPPLIER_PREFERRED_OTD):
            score += 0.05
        return score

    def _offline_result(self, task: dict) -> dict:
        forecast_by_sku = {f.get("sku_id"): f for f in task.get("forecasts", [])
                           if isinstance(f, dict)}
        urgency_by_sku = {a.get("sku_id"): a.get("urgency") for a in
                          task.get("inventory_alerts", []) if isinstance(a, dict)}

        decisions, skipped = [], []
        for sku_id in self._candidate_skus(task):
            inventory = get_inventory_by_sku(sku_id)
            if "error" in inventory:
                skipped.append(sku_id)
                continue

            available = inventory["total_available"]
            target = int(inventory["max_stock"] * PROCUREMENT_TARGET_FILL) - available
            forecast = forecast_by_sku.get(sku_id, {})
            forecast_gap = int(forecast.get("forecast_30d", 0) or 0) - available
            # Cover the larger of the fill target and 30-day demand, but never
            # order more than the warehouses can physically hold.
            quantity = min(max(target, forecast_gap), inventory["max_stock"] - available)
            if quantity <= 0:
                skipped.append(sku_id)
                continue

            candidates = get_qualified_suppliers(sku_id)["qualified_suppliers"]
            eligible = [s for s in candidates
                        if s["reliability_score"] >= SUPPLIER_MIN_RELIABILITY]
            if not eligible:
                # No supplier clears the bar — order from the most reliable
                # available rather than stocking out, and say so.
                eligible = candidates
            if not eligible:
                skipped.append(sku_id)
                continue

            benchmark = get_market_price_benchmark(sku_id)
            market_avg = benchmark.get("market_avg") or 0.0
            best = max(eligible, key=lambda s: self._score_supplier(s, quantity, market_avg))
            quantity = max(quantity, best["min_order_qty"])
            unit_price = get_tier_price(
                {
                    "list_price": best["list_price"],
                    "tier2_qty": best.get("tier2_qty"),
                    "tier2_price": best.get("tier2_price"),
                    "tier3_qty": best.get("tier3_qty"),
                    "tier3_price": best.get("tier3_price"),
                },
                quantity,
            )

            urgency = forecast.get("reorder_urgency") or urgency_by_sku.get(sku_id) or "normal"
            urgency = {"high": "urgent", "warning": "urgent", "low": "normal",
                       "medium": "normal"}.get(urgency, urgency)

            below_bar = best["reliability_score"] < SUPPLIER_MIN_RELIABILITY
            justification = (
                f"reliability {best['reliability_score']:.2f}, "
                f"on-time {best['on_time_delivery_rate']:.2f}, "
                f"lead time {best['lead_time_days']}d, "
                f"unit ${unit_price:.2f} vs market avg ${market_avg:.2f}"
            )
            if below_bar:
                justification += (f" — no supplier met the {SUPPLIER_MIN_RELIABILITY:.2f} "
                                  f"reliability bar, selected best available")

            decisions.append({
                "sku_id": sku_id,
                "sku_name": inventory.get("sku_name", sku_id),
                "order_quantity": int(quantity),
                "selected_supplier_id": best["supplier_id"],
                "selected_supplier_name": best["name"],
                "target_price": round(unit_price, 2),
                "total_estimated_value": round(quantity * unit_price, 2),
                "urgency": urgency,
                "justification": justification,
                "action": "create_po",
            })

        return {
            "procurement_decisions": decisions,
            "skus_skipped": skipped,
            "total_procurement_value": round(
                sum(d["total_estimated_value"] for d in decisions), 2),
        }

    # ── cycle step ───────────────────────────────────────────
    def _persist(self, decisions: list) -> list:
        created = []
        seen_skus = set()
        for d in decisions:
            sku_id = d.get("sku_id")
            supplier_id = d.get("selected_supplier_id")
            if not sku_id or not supplier_id or sku_id in seen_skus:
                continue
            try:
                quantity = int(d.get("order_quantity", 0))
                unit_price = float(d.get("target_price", 0.0))
            except (TypeError, ValueError):
                self._log(f"Skipping malformed decision for {sku_id}")
                continue
            if quantity <= 0:
                continue

            po_number = f"PO-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
            d["po_number"] = po_number
            d["order_quantity"] = quantity
            d["target_price"] = unit_price
            d.setdefault("total_estimated_value", round(quantity * unit_price, 2))
            self.sqlite.create_purchase_order(
                po_number=po_number,
                supplier_id=supplier_id,
                sku_id=sku_id,
                quantity=quantity,
                unit_price=unit_price,
            )
            seen_skus.add(sku_id)
            created.append(d)
            self._log(f"Created PO {po_number} for {sku_id} qty={quantity} "
                      f"@ ${unit_price:.2f} from {d.get('selected_supplier_name', supplier_id)}")
        return created

    def run(self, task: dict) -> dict:
        inventory_alerts = task.get("inventory_alerts", [])
        forecasts = task.get("forecasts", [])
        self._log(f"Processing {len(inventory_alerts)} alerts and {len(forecasts)} forecasts...")
        self.save_state({"status": "running", "started_at": datetime.utcnow().isoformat()})

        prompt = PROMPT_TEMPLATE.format(
            inventory_alerts=inventory_alerts,
            forecasts=forecasts,
            min_reliability=SUPPLIER_MIN_RELIABILITY,
            preferred_reliability=SUPPLIER_PREFERRED_RELIABILITY,
            preferred_otd=SUPPLIER_PREFERRED_OTD,
            target_fill=PROCUREMENT_TARGET_FILL,
        )
        parsed, raw = self._reason(prompt, task)
        decisions = self._persist(parsed.get("procurement_decisions", []))

        self._log(f"Procurement complete — {len(decisions)} purchase orders created.")
        self.save_state({"status": "completed", "pos_created": len(decisions)})
        return {
            "agent": self.name,
            "decisions": decisions,
            "skus_skipped": parsed.get("skus_skipped", []),
            "total_procurement_value": round(
                sum(d.get("total_estimated_value", 0) for d in decisions), 2),
            "raw_response": raw,
        }
