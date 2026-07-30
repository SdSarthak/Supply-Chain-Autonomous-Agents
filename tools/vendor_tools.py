import json
import random
from datetime import datetime, timedelta
from config import SUPPLIERS_FILE


def _load_suppliers() -> list[dict]:
    with open(SUPPLIERS_FILE) as f:
        return json.load(f)


def list_supplier_ids() -> list[str]:
    """Every supplier id in the vendor master data, in file order."""
    return [s["supplier_id"] for s in _load_suppliers()]


def get_tier_price(pricing: dict, quantity: int) -> float:
    """Resolve the volume-tier unit price for a quantity from a pricing block."""
    list_price = pricing.get("list_price", 0.0)
    tier3_qty = pricing.get("tier3_qty")
    tier2_qty = pricing.get("tier2_qty")
    if tier3_qty is not None and quantity >= tier3_qty:
        return pricing.get("tier3_price", list_price)
    if tier2_qty is not None and quantity >= tier2_qty:
        return pricing.get("tier2_price", list_price)
    return list_price


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
    base_price = get_tier_price(pricing, quantity)

    # Simulate supplier counter-offer with slight randomness
    counter_multiplier = random.uniform(0.97, 1.03)
    counter_price = round(base_price * counter_multiplier, 2)

    return {
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"],
        "sku_id": sku_id,
        "quantity": quantity,
        "list_price": list_price,
        "tier_price": base_price,
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


SUPPLIER_PRICE_FLOOR_PCT = 0.88
SUPPLIER_CONCESSION_PER_ROUND = 0.02


def simulate_supplier_counter_offer(current_offer: float, round_num: int,
                                     list_price: float,
                                     floor_price: float = None) -> dict:
    """Simulate how a supplier responds to our price proposal.

    `list_price` is the price the supplier is anchored on for this deal — their
    opening quote. They hold a floor at `floor_price`, defaulting to 88% of that
    anchor. Anything at or above the floor is accepted outright; below it they
    counter, conceding ~2-3% of the anchor per round without crossing the floor.
    """
    current_offer = float(current_offer)
    list_price = float(list_price)
    round_num = max(1, int(round_num))
    floor = round(float(floor_price) if floor_price else list_price * SUPPLIER_PRICE_FLOOR_PCT, 2)

    if current_offer >= floor:
        return {
            "counter_price": round(current_offer, 2),
            "accepted": True,
            "round": round_num,
            "floor_price": floor,
            "message": "We accept your offer."
        }

    concession = SUPPLIER_CONCESSION_PER_ROUND * round_num + random.uniform(0.005, 0.015)
    counter = max(floor, round(list_price * (1 - concession), 2))
    return {
        "counter_price": counter,
        "accepted": False,
        "round": round_num,
        "floor_price": floor,
        "message": "That is below our cost base — this is the best we can do at this volume."
    }
