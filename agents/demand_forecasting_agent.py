import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.inventory_tools import get_demand_history, get_seasonal_factors, get_inventory_by_sku
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_PRO_MODEL
from datetime import datetime, timedelta

SYSTEM_PROMPT = """You are a Demand Forecasting Agent for an industrial supply chain company.
Your role is to analyze historical demand data, identify patterns, seasonality, and trends,
then produce accurate 30, 60, and 90-day demand forecasts for specific SKUs.

You have access to tools to retrieve demand history, seasonal factors, and current inventory levels.
When forecasting:
- Analyze at least 90 days of historical data
- Account for seasonality using seasonal factors
- Consider current inventory levels to flag urgency
- Provide confidence scores (0.0-1.0)
- Always return structured JSON results

Be analytical, precise, and data-driven. Flag any unusual patterns or demand spikes."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_demand_history",
        description="Retrieve historical daily demand records for a SKU",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "sku_id": glm.Schema(type=glm.Type.STRING, description="SKU identifier e.g. SKU-001"),
                "days": glm.Schema(type=glm.Type.INTEGER, description="Number of past days to retrieve, default 90"),
            },
            required=["sku_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_seasonal_factors",
        description="Get monthly seasonal demand multipliers for a SKU",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_inventory_by_sku",
        description="Get current inventory levels for a SKU across all warehouse locations",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
]


class DemandForecastingAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
        super().__init__(
            name="demand_forecasting",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_demand_history": get_demand_history,
                "get_seasonal_factors": get_seasonal_factors,
                "get_inventory_by_sku": get_inventory_by_sku,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
        )

    def run(self, task: dict) -> dict:
        sku_ids = task.get("sku_ids", [])
        self._log(f"Forecasting demand for {len(sku_ids)} SKUs: {sku_ids}")
        self.save_state({"status": "running", "sku_ids": sku_ids, "started_at": datetime.utcnow().isoformat()})

        prompt = f"""Analyze and forecast demand for the following SKUs: {sku_ids}

For each SKU:
1. Retrieve 180 days of demand history
2. Get seasonal factors
3. Check current inventory level
4. Calculate average daily demand and growth trend
5. Produce forecasts for 30, 60, and 90 day horizons
6. Assign a confidence score based on data consistency

Return a JSON object with this structure:
{{
  "forecasts": [
    {{
      "sku_id": "...",
      "avg_daily_demand": <number>,
      "growth_trend_pct": <number>,
      "forecast_30d": <units>,
      "forecast_60d": <units>,
      "forecast_90d": <units>,
      "confidence": <0.0-1.0>,
      "current_stock": <units>,
      "days_of_stock": <number>,
      "reorder_urgency": "low|medium|high|critical"
    }}
  ]
}}"""

        response = self._call_gemini(prompt)
        self._log("Forecast generation complete.")

        forecasts = []
        try:
            import re, json
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                for fc in parsed.get("forecasts", []):
                    today = datetime.utcnow().date()
                    for horizon, days in [(fc.get("forecast_30d"), 30),
                                          (fc.get("forecast_60d"), 60),
                                          (fc.get("forecast_90d"), 90)]:
                        if horizon is not None:
                            forecast_date = (today + timedelta(days=days)).isoformat()
                            self.sqlite.save_forecast(
                                sku_id=fc["sku_id"],
                                forecast_date=forecast_date,
                                predicted_units=float(horizon),
                                confidence=float(fc.get("confidence", 0.75)),
                                horizon_days=days
                            )
                    forecasts.append(fc)
        except Exception as e:
            self._log(f"Warning: Could not parse forecast JSON: {e}")

        self.save_state({"status": "completed", "forecasts_saved": len(forecasts)})
        return {"agent": self.name, "forecasts": forecasts, "raw_response": response}
