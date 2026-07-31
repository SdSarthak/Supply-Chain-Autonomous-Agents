import statistics
from datetime import datetime, timedelta
from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.inventory_tools import get_demand_history, get_seasonal_factors, get_inventory_by_sku
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_PRO_MODEL, CRITICAL_DAYS_OF_STOCK, URGENT_DAYS_OF_STOCK

# History window each forecast is built from.
FORECAST_LOOKBACK_DAYS = 180
FORECAST_HORIZONS = (30, 60, 90)

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

PROMPT_TEMPLATE = """Analyze and forecast demand for the following SKUs: {sku_ids}

For each SKU:
1. Retrieve {lookback} days of demand history
2. Get seasonal factors
3. Check current inventory level
4. Calculate average daily demand and growth trend
5. Produce forecasts for {horizons} day horizons, applying the seasonal factor of
   each month the horizon covers
6. Assign a confidence score based on data consistency (steadier demand = higher confidence)
7. Set reorder_urgency from days of stock cover: critical below {critical_days} days,
   high below {urgent_days} days, medium below twice that, otherwise low

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


class DemandForecastingAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
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
            offline=offline,
        )

    # ── deterministic engine ─────────────────────────────────
    @staticmethod
    def _seasonal_horizon_units(avg_daily: float, factors: dict, horizon_days: int,
                                start: datetime) -> float:
        """Sum daily demand across a horizon, weighting each day by its month factor."""
        total = 0.0
        for offset in range(horizon_days):
            day = start + timedelta(days=offset)
            total += avg_daily * factors.get(f"{day.month:02d}", 1.0)
        return total

    @staticmethod
    def _confidence(weekly_totals: list) -> float:
        """Higher when weekly demand is stable; clamped to a sane 0.5-0.95 band."""
        values = [w["units_sold"] for w in weekly_totals if w["days"] == 7]
        if len(values) < 3:
            return 0.6
        mean = statistics.fmean(values)
        if mean <= 0:
            return 0.5
        cv = statistics.pstdev(values) / mean
        return round(max(0.5, min(0.95, 1.0 - cv)), 2)

    @staticmethod
    def _urgency(days_of_stock: float) -> str:
        if days_of_stock < CRITICAL_DAYS_OF_STOCK:
            return "critical"
        if days_of_stock < URGENT_DAYS_OF_STOCK:
            return "high"
        if days_of_stock < URGENT_DAYS_OF_STOCK * 2:
            return "medium"
        return "low"

    def _offline_result(self, task: dict) -> dict:
        forecasts = []
        today = datetime.utcnow()
        for sku_id in task.get("sku_ids", []):
            history = get_demand_history(sku_id, days=FORECAST_LOOKBACK_DAYS)
            if "error" in history:
                continue
            factors = get_seasonal_factors(sku_id).get("seasonal_factors", {})
            inventory = get_inventory_by_sku(sku_id)

            avg_daily = history["avg_daily_demand"]
            growth = history["growth_trend_pct"]
            # Carry half the observed half-over-half growth forward — full
            # extrapolation over-reacts to a single noisy window.
            trend_multiplier = 1 + (growth / 100.0) * 0.5

            entry = {
                "sku_id": sku_id,
                "sku_name": inventory.get("sku_name", sku_id),
                "avg_daily_demand": round(avg_daily, 2),
                "growth_trend_pct": growth,
                "confidence": self._confidence(history["weekly_totals"]),
            }
            for horizon in FORECAST_HORIZONS:
                units = self._seasonal_horizon_units(avg_daily, factors, horizon, today)
                entry[f"forecast_{horizon}d"] = int(round(units * trend_multiplier))

            available = inventory.get("total_available", 0)
            days_of_stock = round(available / avg_daily, 1) if avg_daily > 0 else 999.0
            entry["current_stock"] = available
            entry["days_of_stock"] = days_of_stock
            entry["reorder_urgency"] = self._urgency(days_of_stock)
            forecasts.append(entry)

        return {"forecasts": forecasts}

    # ── cycle step ───────────────────────────────────────────
    def _persist(self, forecasts: list) -> list:
        saved = []
        today = datetime.utcnow().date()
        for fc in forecasts:
            # Model output: an entry that is not an object must not crash the step.
            if not isinstance(fc, dict):
                continue
            sku_id = fc.get("sku_id")
            if not sku_id:
                continue
            for horizon in FORECAST_HORIZONS:
                units = fc.get(f"forecast_{horizon}d")
                if units is None:
                    continue
                try:
                    predicted = float(units)
                    confidence = float(fc.get("confidence", 0.75))
                except (TypeError, ValueError):
                    continue
                self.sqlite.save_forecast(
                    sku_id=sku_id,
                    forecast_date=(today + timedelta(days=horizon)).isoformat(),
                    predicted_units=predicted,
                    confidence=confidence,
                    horizon_days=horizon,
                )
            saved.append(fc)
        return saved

    def run(self, task: dict) -> dict:
        sku_ids = task.get("sku_ids", [])
        self._log(f"Forecasting demand for {len(sku_ids)} SKUs: {sku_ids}")
        self.save_state({"status": "running", "sku_ids": sku_ids,
                         "started_at": datetime.utcnow().isoformat()})

        prompt = PROMPT_TEMPLATE.format(
            sku_ids=sku_ids,
            lookback=FORECAST_LOOKBACK_DAYS,
            horizons="/".join(str(h) for h in FORECAST_HORIZONS),
            critical_days=CRITICAL_DAYS_OF_STOCK,
            urgent_days=URGENT_DAYS_OF_STOCK,
        )
        parsed, raw = self._reason(prompt, task)
        forecasts = self._persist(parsed.get("forecasts", []))

        self._log(f"Forecast generation complete — {len(forecasts)} SKUs, "
                  f"{len(forecasts) * len(FORECAST_HORIZONS)} horizon rows saved.")
        self.save_state({"status": "completed", "forecasts_saved": len(forecasts)})
        return {"agent": self.name, "forecasts": forecasts, "raw_response": raw}
