from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.inventory_tools import (get_all_inventory, get_inventory_by_sku,
                                    get_reorder_alerts, update_stock, simulate_iot_reading,
                                    list_sku_ids)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_FLASH_MODEL, INVENTORY_CACHE_TTL, PROCUREMENT_TARGET_FILL
from datetime import datetime

# Sensors polled every cycle as a warehouse/logistics health spot check.
MONITORED_SENSORS = ["TEMP-WH-NORTH-01", "STOCK-SKU-005", "LOC-SHIPMENT-42"]
# Warehouse ambient limits — readings outside these are flagged as anomalies.
TEMP_RANGE_C = (15.0, 28.0)
HUMIDITY_MAX_PCT = 70.0
# Stock is critical below this fraction of its reorder point.
CRITICAL_STOCK_RATIO = 0.5
# Stock is overstocked above this fraction of max_stock.
OVERSTOCK_RATIO = 0.9

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


PROMPT_TEMPLATE = """Perform a comprehensive inventory health check:

1. Get the full inventory snapshot for all SKUs
2. Get all reorder alerts
3. Simulate IoT readings for these sensors: {sensors}
4. Analyze the data and identify:
   - SKUs in critical stockout risk (available < {critical_pct:.0f}% of reorder point)
   - SKUs with overstock (available > {overstock_pct:.0f}% of max_stock)
   - Any anomalous IoT readings (warehouse temperature outside {temp_min}-{temp_max}C
     or humidity above {humidity_max}%)

Return a JSON object:
{{
  "inventory_status": "healthy|warning|critical",
  "total_skus_monitored": <number>,
  "critical_alerts": [{{"sku_id": "...", "issue": "...", "available": <n>, "reorder_point": <n>}}],
  "overstock_alerts": [{{"sku_id": "...", "available": <n>, "max_stock": <n>}}],
  "reorder_needed": [{{"sku_id": "...", "urgency": "...", "suggested_qty": <n>}}],
  "iot_summary": {{"sensors_checked": {sensor_count}, "anomalies": []}},
  "total_inventory_value_usd": <number>
}}"""


class InventoryAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
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
            offline=offline,
        )

    def _cache_inventory(self) -> None:
        for sku_id in list_sku_ids():
            data = get_inventory_by_sku(sku_id)
            if "error" not in data:
                self.redis.set(RedisMemory.inventory_key(sku_id), data, ttl=INVENTORY_CACHE_TTL)

    @staticmethod
    def _iot_anomaly(reading: dict) -> Optional[str]:
        values = reading.get("reading", {})
        if reading.get("status") != "online":
            return "sensor offline"
        temp = values.get("temperature_c")
        if temp is not None and not (TEMP_RANGE_C[0] <= temp <= TEMP_RANGE_C[1]):
            return f"temperature {temp}C outside {TEMP_RANGE_C[0]}-{TEMP_RANGE_C[1]}C"
        humidity = values.get("humidity_pct")
        if humidity is not None and humidity > HUMIDITY_MAX_PCT:
            return f"humidity {humidity}% above {HUMIDITY_MAX_PCT}%"
        return None

    def _offline_result(self, task: dict) -> dict:
        snapshot = get_all_inventory()
        items = snapshot["inventory"]
        alert_urgency = {a["sku_id"]: a["urgency"] for a in get_reorder_alerts()["alerts"]}

        critical_alerts, overstock_alerts, reorder_needed = [], [], []
        for item in items:
            available = item["total_available"]
            reorder_point = item["reorder_point"]
            max_stock = item["max_stock"]

            if available < reorder_point * CRITICAL_STOCK_RATIO:
                critical_alerts.append({
                    "sku_id": item["sku_id"],
                    "issue": f"available below {CRITICAL_STOCK_RATIO:.0%} of reorder point",
                    "available": available,
                    "reorder_point": reorder_point,
                })
            if available > max_stock * OVERSTOCK_RATIO:
                overstock_alerts.append({
                    "sku_id": item["sku_id"],
                    "available": available,
                    "max_stock": max_stock,
                })
            if item["sku_id"] in alert_urgency:
                target = int(max_stock * PROCUREMENT_TARGET_FILL)
                reorder_needed.append({
                    "sku_id": item["sku_id"],
                    "sku_name": item["sku_name"],
                    "urgency": alert_urgency[item["sku_id"]],
                    "available": available,
                    "reorder_point": reorder_point,
                    "max_stock": max_stock,
                    "suggested_qty": max(0, target - available),
                })

        anomalies = []
        for sensor_id in MONITORED_SENSORS:
            reading = simulate_iot_reading(sensor_id)
            issue = self._iot_anomaly(reading)
            if issue:
                anomalies.append({"sensor_id": sensor_id, "issue": issue,
                                  "reading": reading["reading"]})

        if critical_alerts:
            status = "critical"
        elif reorder_needed or overstock_alerts or anomalies:
            status = "warning"
        else:
            status = "healthy"

        return {
            "inventory_status": status,
            "total_skus_monitored": len(items),
            "critical_alerts": critical_alerts,
            "overstock_alerts": overstock_alerts,
            "reorder_needed": reorder_needed,
            "iot_summary": {"sensors_checked": len(MONITORED_SENSORS), "anomalies": anomalies},
            "total_inventory_value_usd": snapshot["total_inventory_value_usd"],
        }

    def run(self, task: dict) -> dict:
        self._log("Starting inventory monitoring cycle...")
        self.save_state({"status": "running", "started_at": datetime.utcnow().isoformat()})

        self._cache_inventory()

        prompt = PROMPT_TEMPLATE.format(
            sensors=", ".join(MONITORED_SENSORS),
            sensor_count=len(MONITORED_SENSORS),
            critical_pct=CRITICAL_STOCK_RATIO * 100,
            overstock_pct=OVERSTOCK_RATIO * 100,
            temp_min=TEMP_RANGE_C[0], temp_max=TEMP_RANGE_C[1],
            humidity_max=HUMIDITY_MAX_PCT,
        )
        parsed, raw = self._reason(prompt, task)
        self._log("Inventory check complete.")

        result = {"agent": self.name, "raw_response": raw}
        result.update(parsed)
        result["alerts"] = parsed.get("critical_alerts", []) + parsed.get("reorder_needed", [])

        self._log(f"Status: {result.get('inventory_status', 'unknown')} | "
                  f"{len(result['alerts'])} alerts | "
                  f"{len(result.get('overstock_alerts', []))} overstocked")
        self.save_state({"status": "completed", "alert_count": len(result["alerts"])})
        return result
