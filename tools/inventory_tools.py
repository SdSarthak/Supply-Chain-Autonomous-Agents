import json
import random
from datetime import datetime
from typing import Optional
from config import INVENTORY_FILE, DEMAND_HISTORY_FILE


def _load_inventory() -> list[dict]:
    with open(INVENTORY_FILE) as f:
        return json.load(f)


def _save_inventory(records: list[dict]) -> None:
    with open(INVENTORY_FILE, "w") as f:
        json.dump(records, f, indent=2)


def get_all_inventory() -> dict:
    records = _load_inventory()
    summary = {}
    for r in records:
        sid = r["sku_id"]
        if sid not in summary:
            summary[sid] = {"sku_id": sid, "sku_name": r["sku_name"],
                            "total_on_hand": 0, "total_available": 0,
                            "reorder_point": r["reorder_point"],
                            "max_stock": r["max_stock"], "locations": []}
        summary[sid]["total_on_hand"] += r["on_hand"]
        summary[sid]["total_available"] += r["available"]
        summary[sid]["locations"].append({
            "location": r["location"], "on_hand": r["on_hand"],
            "available": r["available"], "reserved": r["reserved"]
        })
    return {"inventory": list(summary.values()), "snapshot_time": datetime.utcnow().isoformat()}


def get_inventory_by_sku(sku_id: str) -> dict:
    records = _load_inventory()
    sku_records = [r for r in records if r["sku_id"] == sku_id]
    if not sku_records:
        return {"error": f"SKU {sku_id} not found"}
    total_on_hand = sum(r["on_hand"] for r in sku_records)
    total_available = sum(r["available"] for r in sku_records)
    return {
        "sku_id": sku_id,
        "sku_name": sku_records[0]["sku_name"],
        "total_on_hand": total_on_hand,
        "total_available": total_available,
        "reorder_point": sku_records[0]["reorder_point"],
        "max_stock": sku_records[0]["max_stock"],
        "unit_cost": sku_records[0]["unit_cost"],
        "locations": [{"location": r["location"], "on_hand": r["on_hand"],
                       "available": r["available"]} for r in sku_records]
    }


def get_reorder_alerts() -> dict:
    records = _load_inventory()
    alerts = []
    seen = set()
    for r in records:
        sid = r["sku_id"]
        if sid in seen:
            continue
        sku_records = [x for x in records if x["sku_id"] == sid]
        total_available = sum(x["available"] for x in sku_records)
        reorder_point = r["reorder_point"]
        if total_available <= reorder_point:
            urgency = "critical" if total_available <= reorder_point * 0.5 else "warning"
            alerts.append({
                "sku_id": sid,
                "sku_name": r["sku_name"],
                "total_available": total_available,
                "reorder_point": reorder_point,
                "shortage": reorder_point - total_available,
                "urgency": urgency
            })
            seen.add(sid)
    return {"alerts": alerts, "total_alerts": len(alerts)}


def update_stock(sku_id: str, location: str, delta: int) -> dict:
    records = _load_inventory()
    for r in records:
        if r["sku_id"] == sku_id and r["location"] == location:
            r["on_hand"] = max(0, r["on_hand"] + delta)
            r["available"] = max(0, r["on_hand"] - r["reserved"])
            r["last_updated"] = datetime.utcnow().isoformat()
            _save_inventory(records)
            return {"status": "updated", "sku_id": sku_id, "location": location,
                    "new_on_hand": r["on_hand"], "new_available": r["available"]}
    return {"error": f"SKU {sku_id} at {location} not found"}


def simulate_iot_reading(sensor_id: str) -> dict:
    sensor_types = {
        "TEMP": lambda: {"temperature_c": round(random.uniform(18.0, 25.0), 1),
                         "humidity_pct": round(random.uniform(40.0, 65.0), 1)},
        "LOC": lambda: {"latitude": round(random.uniform(48.0, 52.0), 6),
                        "longitude": round(random.uniform(8.0, 14.0), 6),
                        "speed_kmh": round(random.uniform(0, 95.0), 1)},
        "STOCK": lambda: {"count": random.randint(50, 500),
                          "weight_kg": round(random.uniform(10.0, 2000.0), 2)},
    }
    sensor_type = sensor_id.split("-")[0] if "-" in sensor_id else "STOCK"
    reading_fn = sensor_types.get(sensor_type, sensor_types["STOCK"])
    return {
        "sensor_id": sensor_id,
        "timestamp": datetime.utcnow().isoformat(),
        "status": "online",
        "reading": reading_fn()
    }


def get_demand_history(sku_id: str, days: int = 90) -> dict:
    with open(DEMAND_HISTORY_FILE) as f:
        all_records = json.load(f)
    sku_records = [r for r in all_records if r["sku_id"] == sku_id]
    recent = sku_records[-days:] if len(sku_records) >= days else sku_records
    if not recent:
        return {"error": f"No demand history for {sku_id}"}
    total = sum(r["units_sold"] for r in recent)
    avg_daily = round(total / len(recent), 2)
    return {
        "sku_id": sku_id,
        "days_analyzed": len(recent),
        "total_sold": total,
        "avg_daily_demand": avg_daily,
        "records": recent
    }


def get_seasonal_factors(sku_id: str) -> dict:
    with open(DEMAND_HISTORY_FILE) as f:
        all_records = json.load(f)
    sku_records = [r for r in all_records if r["sku_id"] == sku_id]
    if not sku_records:
        return {"error": f"No data for {sku_id}"}
    monthly = {}
    for r in sku_records:
        month = r["date"][5:7]
        monthly.setdefault(month, []).append(r["units_sold"])
    avg_monthly = {m: round(sum(v) / len(v), 2) for m, v in monthly.items()}
    overall_avg = sum(avg_monthly.values()) / len(avg_monthly)
    factors = {m: round(v / overall_avg, 3) for m, v in avg_monthly.items()}
    return {"sku_id": sku_id, "seasonal_factors": factors, "overall_avg_daily": round(overall_avg, 2)}
