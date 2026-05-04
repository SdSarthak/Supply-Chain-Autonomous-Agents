import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.inventory_tools import get_inventory_by_sku, get_reorder_alerts
from tools.vendor_tools import get_qualified_suppliers, get_supplier_info
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_PRO_MODEL
from datetime import datetime
import uuid

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
]


class ProcurementAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
        super().__init__(
            name="procurement",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_inventory_by_sku": get_inventory_by_sku,
                "get_reorder_alerts": get_reorder_alerts,
                "get_qualified_suppliers": get_qualified_suppliers,
                "get_supplier_info": get_supplier_info,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
        )

    def run(self, task: dict) -> dict:
        inventory_alerts = task.get("inventory_alerts", [])
        forecasts = task.get("forecasts", [])
        self._log(f"Processing {len(inventory_alerts)} alerts and {len(forecasts)} forecasts...")
        self.save_state({"status": "running", "started_at": datetime.utcnow().isoformat()})

        prompt = f"""Make procurement decisions based on the following data:

Inventory alerts (SKUs needing reorder): {inventory_alerts}
Demand forecasts: {forecasts}

For each SKU that needs procurement:
1. Check current inventory levels
2. Get qualified suppliers for that SKU
3. Select the best supplier (balance reliability, lead time, price)
4. Calculate order quantity: target = 80% of max_stock - current_available
5. Ensure quantity meets supplier's min_order_qty

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

        response = self._call_gemini(prompt)
        self._log("Procurement decisions generated.")

        decisions = []
        try:
            import re, json
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                for d in parsed.get("procurement_decisions", []):
                    po_number = f"PO-{datetime.utcnow().strftime('%Y%m%d')}-{str(uuid.uuid4())[:8].upper()}"
                    d["po_number"] = po_number
                    self.sqlite.create_purchase_order(
                        po_number=po_number,
                        supplier_id=d.get("selected_supplier_id", "UNKNOWN"),
                        sku_id=d["sku_id"],
                        quantity=int(d.get("order_quantity", 0)),
                        unit_price=float(d.get("target_price", 0.0))
                    )
                    decisions.append(d)
                    self._log(f"Created PO {po_number} for {d['sku_id']} qty={d.get('order_quantity')}")
        except Exception as e:
            self._log(f"Warning: Could not parse procurement JSON: {e}")

        self.save_state({"status": "completed", "pos_created": len(decisions)})
        return {"agent": self.name, "decisions": decisions, "raw_response": response}
