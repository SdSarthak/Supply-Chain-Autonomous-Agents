from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.logistics_tools import (get_available_routes, get_routes_by_supplier_region,
                                    select_route_for_region, estimate_delivery,
                                    track_shipment)
from tools.vendor_tools import get_supplier_info
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_FLASH_MODEL

SYSTEM_PROMPT = """You are a Logistics Agent for an industrial supply chain company.
Your role is to plan optimal shipping routes for purchase orders, assign carriers,
estimate delivery times, and track active shipments.

Route selection criteria:
- For critical/urgent orders: prioritize speed (air or fast road)
- For standard orders: balance cost and reliability
- Always consider carrier reliability score
- Calculate total shipping cost and CO2 impact
- Update PO status after route assignment

Return structured JSON with logistics assignments."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_routes_by_supplier_region",
        description="Get available inbound routes from a supplier's region to warehouses",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "supplier_region": glm.Schema(type=glm.Type.STRING, description="EMEA, APAC, or AMER"),
                "warehouse": glm.Schema(type=glm.Type.STRING, description="Optional warehouse destination"),
            },
            required=["supplier_region"]
        )
    ),
    glm.FunctionDeclaration(
        name="select_route_for_region",
        description=("Pick the best inbound route from a supplier region for a given "
                     "urgency (critical/urgent optimise transit time, normal balances "
                     "cost and reliability, low optimises cost)"),
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "supplier_region": glm.Schema(type=glm.Type.STRING, description="EMEA, APAC, or AMER"),
                "urgency": glm.Schema(type=glm.Type.STRING, description="critical, urgent, normal or low"),
                "warehouse": glm.Schema(type=glm.Type.STRING, description="Optional warehouse destination"),
            },
            required=["supplier_region"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_available_routes",
        description="Get routes between a specific origin and destination",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "origin": glm.Schema(type=glm.Type.STRING),
                "destination": glm.Schema(type=glm.Type.STRING),
            },
            required=["origin", "destination"]
        )
    ),
    glm.FunctionDeclaration(
        name="estimate_delivery",
        description="Estimate delivery time and cost for a route and quantity",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "route_id": glm.Schema(type=glm.Type.STRING),
                "quantity": glm.Schema(type=glm.Type.INTEGER),
            },
            required=["route_id", "quantity"]
        )
    ),
    glm.FunctionDeclaration(
        name="track_shipment",
        description="Get the current tracking status of a shipment by PO number",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"po_number": glm.Schema(type=glm.Type.STRING)},
            required=["po_number"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_supplier_info",
        description="Get supplier details including region for route planning",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"supplier_id": glm.Schema(type=glm.Type.STRING)},
            required=["supplier_id"]
        )
    ),
]

PROMPT_TEMPLATE = """Assign optimal logistics routes for these purchase orders:

{purchase_orders}

For each PO:
1. Get the supplier's info to determine their region (EMEA/APAC/AMER)
2. Call select_route_for_region with that region and the PO's urgency
3. Estimate delivery time and cost for the selected route using estimate_delivery
   with the PO quantity

Return JSON:
{{
  "assignments": [
    {{
      "po_number": "...",
      "supplier_id": "...",
      "sku_id": "...",
      "selected_route_id": "...",
      "carrier": "...",
      "mode": "air|sea|road",
      "transit_days": <number>,
      "estimated_arrival": "YYYY-MM-DD",
      "shipping_cost_usd": <number>,
      "co2_kg": <number>,
      "warehouse_destination": "..."
    }}
  ],
  "unassigned": [{{"po_number": "...", "reason": "..."}}],
  "total_shipping_cost": <number>,
  "avg_transit_days": <number>
}}"""


class LogisticsAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
        super().__init__(
            name="logistics",
            model=GEMINI_FLASH_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_routes_by_supplier_region": get_routes_by_supplier_region,
                "select_route_for_region": select_route_for_region,
                "get_available_routes": get_available_routes,
                "estimate_delivery": estimate_delivery,
                "track_shipment": track_shipment,
                "get_supplier_info": get_supplier_info,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
            offline=offline,
        )

    # ── deterministic engine ─────────────────────────────────
    def _offline_result(self, task: dict) -> dict:
        assignments, unassigned = [], []
        for po in task.get("purchase_orders", []):
            po_number = po.get("po_number")
            supplier = get_supplier_info(po.get("supplier_id", ""))
            if "error" in supplier:
                unassigned.append({"po_number": po_number, "reason": supplier["error"]})
                continue

            selection = select_route_for_region(supplier["region"],
                                                po.get("urgency", "normal"))
            if "error" in selection:
                unassigned.append({"po_number": po_number, "reason": selection["error"]})
                continue

            route = selection["selected_route"]
            quantity = int(po.get("quantity", 0) or 0)
            estimate = estimate_delivery(route["route_id"], quantity)
            assignments.append({
                "po_number": po_number,
                "supplier_id": supplier["supplier_id"],
                "sku_id": po.get("sku_id"),
                "selected_route_id": route["route_id"],
                "carrier": route["carrier"],
                "mode": route["mode"],
                "transit_days": route["transit_days"],
                "estimated_arrival": estimate["estimated_arrival"],
                "shipping_cost_usd": estimate["total_shipping_cost"],
                "co2_kg": estimate["co2_kg"],
                "warehouse_destination": route["destination"],
                "selection_criteria": selection["selection_criteria"],
            })

        total_cost = round(sum(a["shipping_cost_usd"] for a in assignments), 2)
        avg_transit = round(
            sum(a["transit_days"] for a in assignments) / len(assignments), 1
        ) if assignments else 0.0
        return {
            "assignments": assignments,
            "unassigned": unassigned,
            "total_shipping_cost": total_cost,
            "avg_transit_days": avg_transit,
        }

    # ── cycle step ───────────────────────────────────────────
    def _persist(self, assignments: list) -> list:
        applied = []
        for a in assignments:
            po_number = a.get("po_number")
            if not po_number:
                continue
            self.sqlite.update_po_status(
                po_number=po_number,
                status="in_transit",
                route_id=a.get("selected_route_id"),
                expected_delivery=a.get("estimated_arrival"),
            )
            applied.append(a)
            self._log(f"PO {po_number}: {a.get('carrier')} via {a.get('mode')} "
                      f"to {a.get('warehouse_destination')}, ETA {a.get('estimated_arrival')}")
        return applied

    def run(self, task: dict) -> dict:
        purchase_orders = task.get("purchase_orders", [])
        self._log(f"Assigning logistics for {len(purchase_orders)} purchase orders...")
        self.save_state({"status": "running", "po_count": len(purchase_orders)})

        if not purchase_orders:
            self.save_state({"status": "completed", "assigned": 0})
            return {"agent": self.name, "assignments": [], "unassigned": [],
                    "total_shipping_cost": 0.0, "avg_transit_days": 0.0,
                    "message": "No POs to process"}

        prompt = PROMPT_TEMPLATE.format(purchase_orders=purchase_orders)
        parsed, raw = self._reason(prompt, task)
        assignments = self._persist(parsed.get("assignments", []))

        self._log(f"Logistics assignments complete — {len(assignments)} routed.")
        self.save_state({"status": "completed", "assigned": len(assignments)})
        return {
            "agent": self.name,
            "assignments": assignments,
            "unassigned": parsed.get("unassigned", []),
            "total_shipping_cost": parsed.get("total_shipping_cost", 0.0),
            "avg_transit_days": parsed.get("avg_transit_days", 0.0),
            "raw_response": raw,
        }
