import random
import zlib
from datetime import datetime, timedelta
from config import LOGISTICS_ROUTES_FILE
from tools.data_files import load_json_records

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
    return load_json_records(LOGISTICS_ROUTES_FILE, "logistics routes")


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


URGENCY_TO_PRIORITY = {
    "critical": "speed",
    "urgent": "speed",
    "normal": "balanced",
    "low": "cost",
}


def select_optimal_route(routes: list[dict], priority: str = "balanced") -> dict:
    if not routes:
        return {"error": "No routes provided"}

    costs = [r["cost_per_unit"] for r in routes]
    times = [r["transit_days"] for r in routes]
    cost_span = (max(costs) - min(costs)) or 1.0
    time_span = (max(times) - min(times)) or 1.0
    min_cost, min_time = min(costs), min(times)

    def score(r):
        if priority == "speed":
            return -r["transit_days"]
        if priority == "cost":
            return -r["cost_per_unit"]
        if priority == "reliability":
            return r["reliability"]
        # balanced — min-max normalised within the candidate set so the
        # weighting means the same thing whatever routes were passed in.
        norm_cost = 1 - (r["cost_per_unit"] - min_cost) / cost_span
        norm_speed = 1 - (r["transit_days"] - min_time) / time_span
        return r["reliability"] * 0.40 + norm_cost * 0.35 + norm_speed * 0.25

    best = max(routes, key=score)
    return {
        "selected_route": best,
        "selection_criteria": priority,
        "candidates_considered": len(routes),
        "score": round(score(best), 4)
    }


def select_route_for_region(supplier_region: str, urgency: str = "normal",
                            warehouse: str = None) -> dict:
    """Pick the best inbound route from a supplier region for a given urgency.

    Urgency maps onto a selection priority: critical/urgent optimise for
    transit time, normal balances cost against reliability, low optimises cost.
    """
    candidates = get_routes_by_supplier_region(supplier_region, warehouse)["routes"]
    if not candidates and warehouse:
        # Warehouse constraint too tight — fall back to any route from the region.
        candidates = get_routes_by_supplier_region(supplier_region)["routes"]
    if not candidates:
        return {"error": f"No inbound routes found for region {supplier_region}"}

    priority = URGENCY_TO_PRIORITY.get(str(urgency).lower(), "balanced")
    selection = select_optimal_route(candidates, priority)
    selection["supplier_region"] = supplier_region
    selection["urgency"] = urgency
    return selection


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
    # Seeded from the PO number so tracking a shipment twice tells the same
    # story, using a private RNG so the global random stream stays untouched.
    # (crc32 rather than hash() — hash() of a str is salted per process.)
    rng = random.Random(zlib.crc32(po_number.encode("utf-8")))
    status_idx = rng.randint(1, len(SHIPMENT_STATUSES) - 1)
    status = SHIPMENT_STATUSES[status_idx]
    last_update = datetime.utcnow() - timedelta(hours=rng.randint(1, 48))
    eta = datetime.utcnow() + timedelta(days=rng.randint(0, 10))
    return {
        "po_number": po_number,
        "status": status,
        "last_update": last_update.isoformat(),
        "estimated_arrival": eta.date().isoformat(),
        "carrier_tracking_id": f"TRK-{po_number}-{rng.randint(10000, 99999)}",
        "current_location": rng.choice(["Frankfurt Hub", "Singapore Port", "Dallas Warehouse",
                                        "Rotterdam Port", "Hong Kong Airport", "Chicago Hub"])
    }


def get_all_routes() -> dict:
    routes = _load_routes()
    return {"routes": routes, "count": len(routes)}
