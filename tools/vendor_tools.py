import json
import random
import uuid
from datetime import datetime, timedelta
from typing import Optional
from config import SUPPLIERS_FILE


def _load_suppliers() -> list[dict]:
    with open(SUPPLIERS_FILE) as f:
        return json.load(f)


def get_qualified_suppliers(sku_id: str) -> dict:
    suppliers = _load_suppliers()
    qualified = [s for s in suppliers if sku_id in s.get("skus", [])]
    result = []
    for s in qualified:
        pricing = s["pricing"].get(sku_id, {})
        result.append({
            "supplier_id": s["supplier_id"],
            "name": s["name"],
            "country": s["country"],
            "region": s["region"],
            "lead_time_days": s["lead_time_days"],
            "reliability_score": s["reliability_score"],
            "on_time_delivery_rate": s["on_time_delivery_rate"],
            "quality_rejection_rate": s["quality_rejection_rate"],
            "payment_terms": s["payment_terms"],
            "min_order_qty": s["min_order_qty"],
            "list_price": pricing.get("list_price", 0.0),
            "tier2_qty": pricing.get("tier2_qty"),
            "tier2_price": pricing.get("tier2_price"),
            "tier3_qty": pricing.get("tier3_qty"),
            "tier3_price": pricing.get("tier3_price"),
        })
    result.sort(key=lambda x: (-x["reliability_score"], x["lead_time_days"]))
    return {"sku_id": sku_id, "qualified_suppliers": result, "count": len(result)}


def get_supplier_info(supplier_id: str) -> dict:
    suppliers = _load_suppliers()
    for s in suppliers:
        if s["supplier_id"] == supplier_id:
            return s
    return {"error": f"Supplier {supplier_id} not found"}


def get_supplier_offer(supplier_id: str, sku_id: str, quantity: int) -> dict:
    suppliers = _load_suppliers()
    supplier = next((s for s in suppliers if s["supplier_id"] == supplier_id), None)
    if not supplier:
        return {"error": f"Supplier {supplier_id} not found"}
    if sku_id not in supplier.get("skus", []):
        return {"error": f"Supplier {supplier_id} does not supply {sku_id}"}

    pricing = supplier["pricing"].get(sku_id, {})
    list_price = pricing.get("list_price", 0.0)

    if quantity >= pricing.get("tier3_qty", float("inf")):
        base_price = pricing.get("tier3_price", list_price)
    elif quantity >= pricing.get("tier2_qty", float("inf")):
        base_price = pricing.get("tier2_price", list_price)
    else:
        base_price = list_price

    # Simulate supplier counter-offer with slight randomness
    counter_multiplier = random.uniform(0.97, 1.03)
    counter_price = round(base_price * counter_multiplier, 2)

    return {
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"],
        "sku_id": sku_id,
        "quantity": quantity,
        "list_price": list_price,
        "offered_price": counter_price,
        "lead_time_days": supplier["lead_time_days"],
        "payment_terms": supplier["payment_terms"],
        "min_order_qty": supplier["min_order_qty"],
        "offer_valid_until": (datetime.utcnow() + timedelta(hours=48)).isoformat()
    }


def get_market_price_benchmark(sku_id: str) -> dict:
    suppliers = _load_suppliers()
    prices = []
    for s in suppliers:
        if sku_id in s.get("skus", []) and sku_id in s.get("pricing", {}):
            prices.append(s["pricing"][sku_id]["list_price"])
    if not prices:
        return {"error": f"No pricing data found for {sku_id}"}
    return {
        "sku_id": sku_id,
        "market_min": min(prices),
        "market_max": max(prices),
        "market_avg": round(sum(prices) / len(prices), 2),
        "supplier_count": len(prices)
    }


def get_alternative_supplier(sku_id: str, exclude_supplier_id: str) -> dict:
    suppliers = _load_suppliers()
    alternatives = [
        s for s in suppliers
        if sku_id in s.get("skus", []) and s["supplier_id"] != exclude_supplier_id
    ]
    if not alternatives:
        return {"error": f"No alternative suppliers for {sku_id}"}
    best = max(alternatives, key=lambda x: x["reliability_score"])
    pricing = best["pricing"].get(sku_id, {})
    return {
        "supplier_id": best["supplier_id"],
        "name": best["name"],
        "country": best["country"],
        "reliability_score": best["reliability_score"],
        "lead_time_days": best["lead_time_days"],
        "list_price": pricing.get("list_price", 0.0)
    }


def simulate_supplier_counter_offer(current_offer: float, round_num: int,
                                     list_price: float) -> dict:
    # Supplier concedes ~2-3% per round but never goes below 88% of list price
    floor = list_price * 0.88
    concession = current_offer * (1 - random.uniform(0.015, 0.03))
    counter = max(floor, round(concession, 2))
    accepted = counter <= current_offer
    return {
        "counter_price": counter,
        "accepted": accepted,
        "round": round_num,
        "message": "We can offer this price given the volume." if not accepted else "We accept your offer."
    }
