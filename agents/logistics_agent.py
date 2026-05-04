import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.logistics_tools import (get_available_routes, get_routes_by_supplier_region,
                                    select_optimal_route, estimate_delivery,
                                    track_shipment)
from tools.vendor_tools import get_supplier_info
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_FLASH_MODEL
from datetime import datetime

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


class LogisticsAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
        super().__init__(
            name="logistics",
            model=GEMINI_FLASH_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_routes_by_supplier_region": get_routes_by_supplier_region,
                "get_available_routes": get_available_routes,
                "estimate_delivery": estimate_delivery,
                "track_shipment": track_shipment,
                "get_supplier_info": get_supplier_info,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
        )

    def run(self, task: dict) -> dict:
        purchase_orders = task.get("purchase_orders", [])
        self._log(f"Assigning logistics for {len(purchase_orders)} purchase orders...")
        self.save_state({"status": "running", "po_count": len(purchase_orders)})

        if not purchase_orders:
            return {"agent": self.name, "assignments": [], "message": "No POs to process"}

        prompt = f"""Assign optimal logistics routes for these purchase orders:

{purchase_orders}

For each PO:
1. Get the supplier's info to determine their region (EMEA/APAC/AMER)
2. Get available routes from that supplier's region using get_routes_by_supplier_region
3. Select optimal route based on urgency:
   - urgent/critical POs: choose lowest transit_days route
   - normal POs: choose best balance of cost and reliability
4. Estimate delivery time and cost using estimate_delivery

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
  "total_shipping_cost": <number>,
  "avg_transit_days": <number>
}}"""

        response = self._call_gemini(prompt)
        self._log("Logistics assignments complete.")

        assignments = []
        try:
            import re, json
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                for a in parsed.get("assignments", []):
                    self.sqlite.update_po_status(
                        po_number=a["po_number"],
                        status="in_transit",
                        route_id=a.get("selected_route_id"),
                        expected_delivery=a.get("estimated_arrival")
                    )
                    assignments.append(a)
                    self._log(f"PO {a['po_number']}: {a.get('carrier')} via {a.get('mode')}, ETA {a.get('estimated_arrival')}")
        except Exception as e:
            self._log(f"Warning: Could not parse logistics JSON: {e}")

        self.save_state({"status": "completed", "assigned": len(assignments)})
        return {"agent": self.name, "assignments": assignments, "raw_response": response}
