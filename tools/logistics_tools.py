import json
import random
from datetime import datetime, timedelta
from config import LOGISTICS_ROUTES_FILE

REGION_TO_ORIGIN = {
    "EMEA": "SUPPLIER-EMEA",
    "APAC": "SUPPLIER-APAC",
    "AMER": "SUPPLIER-AMER",
}

SHIPMENT_STATUSES = [
    "order_confirmed", "picked_up", "in_transit", "customs_clearance",
    "out_for_delivery", "delivered"
]


def _load_routes() -> list[dict]:
    with open(LOGISTICS_ROUTES_FILE) as f:
        return json.load(f)


def get_available_routes(origin: str, destination: str) -> dict:
    routes = _load_routes()
    matching = [r for r in routes if r["origin"] == origin and r["destination"] == destination]
    if not matching:
        # Return all inbound supplier routes if exact match not found
        matching = [r for r in routes if origin in r["origin"] or destination in r["destination"]]
    return {
        "origin": origin,
        "destination": destination,
        "routes": matching,
        "count": len(matching)
    }


def get_routes_by_supplier_region(supplier_region: str, warehouse: str = None) -> dict:
    origin = REGION_TO_ORIGIN.get(supplier_region, supplier_region)
    routes = _load_routes()
    matching = [r for r in routes if r["origin"] == origin]
    if warehouse:
        matching = [r for r in matching if r["destination"] == warehouse]
    return {"supplier_region": supplier_region, "routes": matching, "count": len(matching)}


def select_optimal_route(routes: list[dict], priority: str = "balanced") -> dict:
    if not routes:
        return {"error": "No routes provided"}

    def score(r):
        if priority == "speed":
            return -r["transit_days"]
        elif priority == "cost":
            return -r["cost_per_unit"]
        elif priority == "reliability":
            return r["reliability"]
        else:  # balanced
            norm_cost = 1 - (r["cost_per_unit"] / 10)
            norm_speed = 1 - (r["transit_days"] / 30)
            return r["reliability"] * 0.4 + norm_cost * 0.35 + norm_speed * 0.25

    best = max(routes, key=score)
    return {
        "selected_route": best,
        "selection_criteria": priority,
        "score": round(score(best), 4)
    }


def estimate_delivery(route_id: str, quantity: int) -> dict:
    routes = _load_routes()
    route = next((r for r in routes if r["route_id"] == route_id), None)
    if not route:
        return {"error": f"Route {route_id} not found"}
    total_cost = round(route["cost_per_unit"] * quantity, 2)
    eta = (datetime.utcnow() + timedelta(days=route["transit_days"])).date().isoformat()
    return {
        "route_id": route_id,
        "carrier": route["carrier"],
        "mode": route["mode"],
        "transit_days": route["transit_days"],
        "estimated_arrival": eta,
        "total_shipping_cost": total_cost,
        "cost_per_unit": route["cost_per_unit"],
        "co2_kg": round(route["co2_kg_per_unit"] * quantity, 2)
    }


def track_shipment(po_number: str) -> dict:
    random.seed(hash(po_number) % 1000)
    status_idx = random.randint(1, len(SHIPMENT_STATUSES) - 1)
    status = SHIPMENT_STATUSES[status_idx]
    last_update = datetime.utcnow() - timedelta(hours=random.randint(1, 48))
    eta = datetime.utcnow() + timedelta(days=random.randint(0, 10))
    return {
        "po_number": po_number,
        "status": status,
        "last_update": last_update.isoformat(),
        "estimated_arrival": eta.date().isoformat(),
        "carrier_tracking_id": f"TRK-{po_number}-{random.randint(10000, 99999)}",
        "current_location": random.choice(["Frankfurt Hub", "Singapore Port", "Dallas Warehouse",
                                           "Rotterdam Port", "Hong Kong Airport", "Chicago Hub"])
    }


def get_all_routes() -> dict:
    routes = _load_routes()
    return {"routes": routes, "count": len(routes)}
