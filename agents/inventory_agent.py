import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.inventory_tools import (get_all_inventory, get_inventory_by_sku,
                                    get_reorder_alerts, update_stock, simulate_iot_reading)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_FLASH_MODEL, INVENTORY_CACHE_TTL
from datetime import datetime

SYSTEM_PROMPT = """You are an Inventory Management Agent for an industrial supply chain.
Your role is to monitor stock levels across all warehouse locations, detect stockouts and
overstock situations, process IoT sensor readings, and generate reorder alerts.

You are fast and decisive — your goal is to ensure zero stockouts while minimizing
excess inventory. Always check all locations and aggregate totals before making decisions.
Return structured JSON summaries of your findings."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_all_inventory",
        description="Get a full snapshot of all SKU stock levels across all warehouse locations",
        parameters=glm.Schema(type=glm.Type.OBJECT, properties={})
    ),
    glm.FunctionDeclaration(
        name="get_inventory_by_sku",
        description="Get inventory details for a specific SKU",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_reorder_alerts",
        description="Get list of all SKUs that are at or below their reorder point",
        parameters=glm.Schema(type=glm.Type.OBJECT, properties={})
    ),
    glm.FunctionDeclaration(
        name="update_stock",
        description="Update stock level for a SKU at a specific location",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "sku_id": glm.Schema(type=glm.Type.STRING),
                "location": glm.Schema(type=glm.Type.STRING, description="Warehouse: WH-NORTH, WH-SOUTH, or WH-EAST"),
                "delta": glm.Schema(type=glm.Type.INTEGER, description="Change in stock (positive=increase, negative=decrease)"),
            },
            required=["sku_id", "location", "delta"]
        )
    ),
    glm.FunctionDeclaration(
        name="simulate_iot_reading",
        description="Get a simulated IoT sensor reading for warehouse/logistics monitoring",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sensor_id": glm.Schema(type=glm.Type.STRING, description="e.g. TEMP-WH-NORTH-01")},
            required=["sensor_id"]
        )
    ),
]


class InventoryAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
        super().__init__(
            name="inventory",
            model=GEMINI_FLASH_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_all_inventory": get_all_inventory,
                "get_inventory_by_sku": get_inventory_by_sku,
                "get_reorder_alerts": get_reorder_alerts,
                "update_stock": update_stock,
                "simulate_iot_reading": simulate_iot_reading,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
        )

    def _cache_inventory(self) -> None:
        from tools.inventory_tools import get_inventory_by_sku as get_inv
        sku_ids = [f"SKU-{str(i).zfill(3)}" for i in range(1, 11)]
        for sku_id in sku_ids:
            data = get_inv(sku_id)
            if "error" not in data:
                self.redis.set(RedisMemory.inventory_key(sku_id), data, ttl=INVENTORY_CACHE_TTL)

    def run(self, task: dict) -> dict:
        self._log("Starting inventory monitoring cycle...")
        self.save_state({"status": "running", "started_at": datetime.utcnow().isoformat()})

        self._cache_inventory()

        prompt = """Perform a comprehensive inventory health check:

1. Get the full inventory snapshot for all SKUs
2. Get all reorder alerts
3. Simulate IoT readings for 3 sensors: TEMP-WH-NORTH-01, STOCK-SKU-005, LOC-SHIPMENT-42
4. Analyze the data and identify:
   - SKUs in critical stockout risk (available < 50% of reorder point)
   - SKUs with overstock (available > 90% of max_stock)
   - Any anomalous IoT readings

Return a JSON object:
{
  "inventory_status": "healthy|warning|critical",
  "total_skus_monitored": <number>,
  "critical_alerts": [{"sku_id": "...", "issue": "...", "available": <n>, "reorder_point": <n>}],
  "overstock_alerts": [{"sku_id": "...", "available": <n>, "max_stock": <n>}],
  "reorder_needed": [{"sku_id": "...", "urgency": "...", "suggested_qty": <n>}],
  "iot_summary": {"sensors_checked": 3, "anomalies": []},
  "total_inventory_value_usd": <number>
}"""

        response = self._call_gemini(prompt)
        self._log("Inventory check complete.")

        result = {"agent": self.name, "raw_response": response, "alerts": []}
        try:
            import re, json
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                result.update(parsed)
                alerts = parsed.get("critical_alerts", []) + parsed.get("reorder_needed", [])
                result["alerts"] = alerts
        except Exception as e:
            self._log(f"Warning: Could not parse inventory JSON: {e}")

        self.save_state({"status": "completed", "alert_count": len(result.get("alerts", []))})
        return result
