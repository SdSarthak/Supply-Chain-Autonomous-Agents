import json
import random
import math
from datetime import date, timedelta
import os

random.seed(42)

SKUS = [
    {"sku_id": "SKU-001", "name": "Industrial Pump Valve", "category": "Mechanical", "unit": "piece"},
    {"sku_id": "SKU-002", "name": "Copper Wire Spool 500m", "category": "Electrical", "unit": "spool"},
    {"sku_id": "SKU-003", "name": "Hydraulic Cylinder 50mm", "category": "Mechanical", "unit": "piece"},
    {"sku_id": "SKU-004", "name": "Safety Relay Module", "category": "Electrical", "unit": "piece"},
    {"sku_id": "SKU-005", "name": "Stainless Steel Fastener Kit", "category": "Hardware", "unit": "kit"},
    {"sku_id": "SKU-006", "name": "Pneumatic Hose 10m", "category": "Mechanical", "unit": "roll"},
    {"sku_id": "SKU-007", "name": "Control Panel Board", "category": "Electrical", "unit": "piece"},
    {"sku_id": "SKU-008", "name": "Bearing Assembly 6204", "category": "Mechanical", "unit": "piece"},
    {"sku_id": "SKU-009", "name": "Industrial Filter Cartridge", "category": "Consumable", "unit": "pack"},
    {"sku_id": "SKU-010", "name": "Sensor Proximity NPN", "category": "Electrical", "unit": "piece"},
]

base_demand = {
    "SKU-001": 45, "SKU-002": 30, "SKU-003": 22, "SKU-004": 60,
    "SKU-005": 120, "SKU-006": 38, "SKU-007": 15, "SKU-008": 85,
    "SKU-009": 200, "SKU-010": 70
}
reorder_points = {
    "SKU-001": 100, "SKU-002": 60, "SKU-003": 40, "SKU-004": 120,
    "SKU-005": 300, "SKU-006": 80, "SKU-007": 30, "SKU-008": 200,
    "SKU-009": 500, "SKU-010": 150
}
max_stocks = {
    "SKU-001": 600, "SKU-002": 400, "SKU-003": 250, "SKU-004": 700,
    "SKU-005": 1800, "SKU-006": 480, "SKU-007": 180, "SKU-008": 1200,
    "SKU-009": 3000, "SKU-010": 900
}

out_dir = os.path.dirname(os.path.abspath(__file__))

# demand_history.json
demand_records = []
start_date = date(2023, 1, 1)
regions = ["APAC", "EMEA", "AMER"]

for sku in SKUS:
    sid = sku["sku_id"]
    base = base_demand[sid]
    for d in range(730):
        dt = start_date + timedelta(days=d)
        season = 1 + 0.25 * math.sin(2 * math.pi * (dt.timetuple().tm_yday / 365) - math.pi / 2)
        trend = 1 + 0.05 * (d / 365)
        noise = random.gauss(0, 0.12)
        units = max(0, round(base * season * trend * (1 + noise)))
        demand_records.append({
            "sku_id": sid,
            "date": dt.isoformat(),
            "units_sold": units,
            "region": random.choice(regions)
        })

with open(os.path.join(out_dir, "demand_history.json"), "w") as f:
    json.dump(demand_records, f, indent=2)
print(f"demand_history.json: {len(demand_records)} records")

# inventory.json
locations = ["WH-NORTH", "WH-SOUTH", "WH-EAST"]
inventory_records = []
unit_costs = {
    "SKU-001": 48.50, "SKU-002": 95.00, "SKU-003": 185.00, "SKU-004": 62.00,
    "SKU-005": 18.50, "SKU-006": 22.00, "SKU-007": 340.00, "SKU-008": 12.80,
    "SKU-009": 8.20, "SKU-010": 28.50
}

for sku in SKUS:
    sid = sku["sku_id"]
    for loc in locations:
        on_hand = random.randint(int(reorder_points[sid] * 0.5), max_stocks[sid])
        reserved = random.randint(0, int(on_hand * 0.2))
        inventory_records.append({
            "sku_id": sid,
            "sku_name": sku["name"],
            "location": loc,
            "on_hand": on_hand,
            "reserved": reserved,
            "available": on_hand - reserved,
            "reorder_point": reorder_points[sid],
            "max_stock": max_stocks[sid],
            "last_updated": date.today().isoformat(),
            "unit_cost": unit_costs[sid]
        })

with open(os.path.join(out_dir, "inventory.json"), "w") as f:
    json.dump(inventory_records, f, indent=2)
print(f"inventory.json: {len(inventory_records)} records")

# suppliers.json
suppliers = [
    {
        "supplier_id": "SUP-001", "name": "TechParts GmbH", "country": "Germany", "region": "EMEA",
        "contact": "hans.mueller@techparts.de", "payment_terms": "Net30",
        "skus": ["SKU-001", "SKU-003", "SKU-006", "SKU-008"],
        "lead_time_days": 14, "reliability_score": 0.92, "on_time_delivery_rate": 0.94,
        "quality_rejection_rate": 0.02, "min_order_qty": 10,
        "pricing": {
            "SKU-001": {"list_price": 48.50, "tier2_qty": 100, "tier2_price": 44.00, "tier3_qty": 500, "tier3_price": 40.00},
            "SKU-003": {"list_price": 185.00, "tier2_qty": 20, "tier2_price": 170.00, "tier3_qty": 100, "tier3_price": 158.00},
            "SKU-006": {"list_price": 22.00, "tier2_qty": 50, "tier2_price": 19.50, "tier3_qty": 200, "tier3_price": 17.00},
            "SKU-008": {"list_price": 12.80, "tier2_qty": 100, "tier2_price": 11.50, "tier3_qty": 500, "tier3_price": 10.20}
        },
        "past_deliveries": [
            {"po": "PO-2024-001", "sku": "SKU-001", "qty": 200, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-008", "sku": "SKU-008", "qty": 500, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-015", "sku": "SKU-003", "qty": 50, "on_time": False, "quality_ok": True}
        ]
    },
    {
        "supplier_id": "SUP-002", "name": "ElectroPro Asia", "country": "Taiwan", "region": "APAC",
        "contact": "sales@electropro.tw", "payment_terms": "Net45",
        "skus": ["SKU-002", "SKU-004", "SKU-007", "SKU-010"],
        "lead_time_days": 21, "reliability_score": 0.87, "on_time_delivery_rate": 0.89,
        "quality_rejection_rate": 0.03, "min_order_qty": 20,
        "pricing": {
            "SKU-002": {"list_price": 95.00, "tier2_qty": 30, "tier2_price": 88.00, "tier3_qty": 100, "tier3_price": 80.00},
            "SKU-004": {"list_price": 62.00, "tier2_qty": 50, "tier2_price": 56.00, "tier3_qty": 200, "tier3_price": 50.00},
            "SKU-007": {"list_price": 340.00, "tier2_qty": 10, "tier2_price": 310.00, "tier3_qty": 50, "tier3_price": 285.00},
            "SKU-010": {"list_price": 28.50, "tier2_qty": 100, "tier2_price": 25.00, "tier3_qty": 500, "tier3_price": 21.50}
        },
        "past_deliveries": [
            {"po": "PO-2024-003", "sku": "SKU-004", "qty": 100, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-011", "sku": "SKU-010", "qty": 300, "on_time": False, "quality_ok": False},
            {"po": "PO-2024-019", "sku": "SKU-002", "qty": 60, "on_time": True, "quality_ok": True}
        ]
    },
    {
        "supplier_id": "SUP-003", "name": "FastFix Hardware Co", "country": "USA", "region": "AMER",
        "contact": "orders@fastfix.com", "payment_terms": "Net15",
        "skus": ["SKU-005", "SKU-009"],
        "lead_time_days": 7, "reliability_score": 0.95, "on_time_delivery_rate": 0.97,
        "quality_rejection_rate": 0.01, "min_order_qty": 50,
        "pricing": {
            "SKU-005": {"list_price": 18.50, "tier2_qty": 200, "tier2_price": 16.00, "tier3_qty": 1000, "tier3_price": 13.50},
            "SKU-009": {"list_price": 8.20, "tier2_qty": 500, "tier2_price": 7.00, "tier3_qty": 2000, "tier3_price": 5.80}
        },
        "past_deliveries": [
            {"po": "PO-2024-005", "sku": "SKU-005", "qty": 500, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-012", "sku": "SKU-009", "qty": 1000, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-021", "sku": "SKU-005", "qty": 800, "on_time": True, "quality_ok": True}
        ]
    },
    {
        "supplier_id": "SUP-004", "name": "PneumaTech Italia", "country": "Italy", "region": "EMEA",
        "contact": "vendite@pneumatech.it", "payment_terms": "Net30",
        "skus": ["SKU-001", "SKU-003", "SKU-006"],
        "lead_time_days": 18, "reliability_score": 0.88, "on_time_delivery_rate": 0.90,
        "quality_rejection_rate": 0.025, "min_order_qty": 5,
        "pricing": {
            "SKU-001": {"list_price": 51.00, "tier2_qty": 50, "tier2_price": 46.00, "tier3_qty": 200, "tier3_price": 41.50},
            "SKU-003": {"list_price": 178.00, "tier2_qty": 20, "tier2_price": 162.00, "tier3_qty": 100, "tier3_price": 150.00},
            "SKU-006": {"list_price": 24.50, "tier2_qty": 50, "tier2_price": 21.50, "tier3_qty": 200, "tier3_price": 18.80}
        },
        "past_deliveries": [
            {"po": "PO-2024-007", "sku": "SKU-006", "qty": 100, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-016", "sku": "SKU-001", "qty": 80, "on_time": False, "quality_ok": True}
        ]
    },
    {
        "supplier_id": "SUP-005", "name": "SensorWorks Korea", "country": "South Korea", "region": "APAC",
        "contact": "biz@sensorworks.kr", "payment_terms": "Net30",
        "skus": ["SKU-004", "SKU-007", "SKU-010"],
        "lead_time_days": 16, "reliability_score": 0.91, "on_time_delivery_rate": 0.93,
        "quality_rejection_rate": 0.015, "min_order_qty": 25,
        "pricing": {
            "SKU-004": {"list_price": 58.00, "tier2_qty": 100, "tier2_price": 52.00, "tier3_qty": 500, "tier3_price": 46.00},
            "SKU-007": {"list_price": 320.00, "tier2_qty": 15, "tier2_price": 295.00, "tier3_qty": 60, "tier3_price": 270.00},
            "SKU-010": {"list_price": 26.00, "tier2_qty": 200, "tier2_price": 22.50, "tier3_qty": 1000, "tier3_price": 19.00}
        },
        "past_deliveries": [
            {"po": "PO-2024-009", "sku": "SKU-010", "qty": 400, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-017", "sku": "SKU-004", "qty": 150, "on_time": True, "quality_ok": True}
        ]
    },
    {
        "supplier_id": "SUP-006", "name": "BearingMaster Poland", "country": "Poland", "region": "EMEA",
        "contact": "export@bearingmaster.pl", "payment_terms": "Net45",
        "skus": ["SKU-008", "SKU-001"],
        "lead_time_days": 12, "reliability_score": 0.93, "on_time_delivery_rate": 0.95,
        "quality_rejection_rate": 0.012, "min_order_qty": 50,
        "pricing": {
            "SKU-008": {"list_price": 11.90, "tier2_qty": 200, "tier2_price": 10.50, "tier3_qty": 1000, "tier3_price": 9.20},
            "SKU-001": {"list_price": 46.00, "tier2_qty": 100, "tier2_price": 41.50, "tier3_qty": 500, "tier3_price": 37.00}
        },
        "past_deliveries": [
            {"po": "PO-2024-006", "sku": "SKU-008", "qty": 600, "on_time": True, "quality_ok": True},
            {"po": "PO-2024-014", "sku": "SKU-008", "qty": 800, "on_time": True, "quality_ok": True}
        ]
    },
    {
        "supplier_id": "SUP-007", "name": "ConsumaParts Mexico", "country": "Mexico", "region": "AMER",
        "contact": "ventas@consumaparts.mx", "payment_terms": "Net20",
        "skus": ["SKU-005", "SKU-009", "SKU-002"],
        "lead_time_days": 10, "reliability_score": 0.84, "on_time_delivery_rate": 0.86,
        "quality_rejection_rate": 0.04, "min_order_qty": 100,
        "pricing": {
            "SKU-005": {"list_price": 17.00, "tier2_qty": 300, "tier2_price": 15.00, "tier3_qty": 1500, "tier3_price": 12.50},
            "SKU-009": {"list_price": 7.80, "tier2_qty": 1000, "tier2_price": 6.50, "tier3_qty": 5000, "tier3_price": 5.20},
            "SKU-002": {"list_price": 88.00, "tier2_qty": 50, "tier2_price": 80.00, "tier3_qty": 200, "tier3_price": 72.00}
        },
        "past_deliveries": [
            {"po": "PO-2024-010", "sku": "SKU-009", "qty": 2000, "on_time": False, "quality_ok": True},
            {"po": "PO-2024-018", "sku": "SKU-005", "qty": 400, "on_time": True, "quality_ok": False}
        ]
    },
    {
        "supplier_id": "SUP-008", "name": "WireTech India", "country": "India", "region": "APAC",
        "contact": "supply@wiretech.in", "payment_terms": "Net30",
        "skus": ["SKU-002", "SKU-004"],
        "lead_time_days": 25, "reliability_score": 0.82, "on_time_delivery_rate": 0.85,
        "quality_rejection_rate": 0.05, "min_order_qty": 30,
        "pricing": {
            "SKU-002": {"list_price": 78.00, "tier2_qty": 60, "tier2_price": 70.00, "tier3_qty": 250, "tier3_price": 62.00},
            "SKU-004": {"list_price": 54.00, "tier2_qty": 100, "tier2_price": 48.00, "tier3_qty": 400, "tier3_price": 42.00}
        },
        "past_deliveries": [
            {"po": "PO-2024-013", "sku": "SKU-002", "qty": 80, "on_time": False, "quality_ok": True},
            {"po": "PO-2024-020", "sku": "SKU-004", "qty": 200, "on_time": True, "quality_ok": True}
        ]
    }
]

with open(os.path.join(out_dir, "suppliers.json"), "w") as f:
    json.dump(suppliers, f, indent=2)
print(f"suppliers.json: {len(suppliers)} suppliers")

# logistics_routes.json
routes = [
    {"route_id": "RT-001", "origin": "WH-NORTH", "destination": "CUSTOMER-ZONE-A", "carrier": "FastFreight",
     "mode": "road", "transit_days": 2, "cost_per_unit": 1.20, "max_weight_kg": 10000, "reliability": 0.96, "co2_kg_per_unit": 0.08},
    {"route_id": "RT-002", "origin": "WH-NORTH", "destination": "CUSTOMER-ZONE-B", "carrier": "AirCargo Express",
     "mode": "air", "transit_days": 1, "cost_per_unit": 4.50, "max_weight_kg": 5000, "reliability": 0.99, "co2_kg_per_unit": 0.45},
    {"route_id": "RT-003", "origin": "WH-SOUTH", "destination": "CUSTOMER-ZONE-A", "carrier": "RoadMaster",
     "mode": "road", "transit_days": 3, "cost_per_unit": 0.95, "max_weight_kg": 20000, "reliability": 0.93, "co2_kg_per_unit": 0.10},
    {"route_id": "RT-004", "origin": "WH-SOUTH", "destination": "CUSTOMER-ZONE-C", "carrier": "SeaLink",
     "mode": "sea", "transit_days": 12, "cost_per_unit": 0.45, "max_weight_kg": 50000, "reliability": 0.91, "co2_kg_per_unit": 0.03},
    {"route_id": "RT-005", "origin": "WH-EAST", "destination": "CUSTOMER-ZONE-B", "carrier": "FastFreight",
     "mode": "road", "transit_days": 2, "cost_per_unit": 1.10, "max_weight_kg": 15000, "reliability": 0.95, "co2_kg_per_unit": 0.09},
    {"route_id": "RT-006", "origin": "WH-EAST", "destination": "CUSTOMER-ZONE-D", "carrier": "AirCargo Express",
     "mode": "air", "transit_days": 1, "cost_per_unit": 5.20, "max_weight_kg": 3000, "reliability": 0.98, "co2_kg_per_unit": 0.48},
    {"route_id": "RT-007", "origin": "SUPPLIER-EMEA", "destination": "WH-NORTH", "carrier": "EuroFreight",
     "mode": "road", "transit_days": 4, "cost_per_unit": 1.80, "max_weight_kg": 25000, "reliability": 0.94, "co2_kg_per_unit": 0.12},
    {"route_id": "RT-008", "origin": "SUPPLIER-APAC", "destination": "WH-EAST", "carrier": "PacificShip",
     "mode": "sea", "transit_days": 18, "cost_per_unit": 0.60, "max_weight_kg": 80000, "reliability": 0.89, "co2_kg_per_unit": 0.04},
    {"route_id": "RT-009", "origin": "SUPPLIER-AMER", "destination": "WH-SOUTH", "carrier": "AmeriTruck",
     "mode": "road", "transit_days": 5, "cost_per_unit": 1.40, "max_weight_kg": 20000, "reliability": 0.96, "co2_kg_per_unit": 0.11},
    {"route_id": "RT-010", "origin": "SUPPLIER-APAC", "destination": "WH-NORTH", "carrier": "AirCargo Express",
     "mode": "air", "transit_days": 2, "cost_per_unit": 6.80, "max_weight_kg": 8000, "reliability": 0.97, "co2_kg_per_unit": 0.52},
    {"route_id": "RT-011", "origin": "SUPPLIER-EMEA", "destination": "WH-SOUTH", "carrier": "MedSea Logistics",
     "mode": "sea", "transit_days": 14, "cost_per_unit": 0.55, "max_weight_kg": 60000, "reliability": 0.88, "co2_kg_per_unit": 0.035},
    {"route_id": "RT-012", "origin": "SUPPLIER-AMER", "destination": "WH-EAST", "carrier": "FastFreight",
     "mode": "road", "transit_days": 6, "cost_per_unit": 1.60, "max_weight_kg": 18000, "reliability": 0.94, "co2_kg_per_unit": 0.13},
]

with open(os.path.join(out_dir, "logistics_routes.json"), "w") as f:
    json.dump(routes, f, indent=2)
print(f"logistics_routes.json: {len(routes)} routes")
print("All mock data generated successfully.")
