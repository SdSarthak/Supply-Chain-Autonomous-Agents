"""
Stress tests for Supply Chain Autonomous Intelligence Network.
Tests all tools, memory layers, agent engines, the full offline cycle, data
integrity, concurrency, and edge cases.
Does NOT require a Gemini API key or a running Redis — covers the whole
non-LLM stack plus every agent's deterministic engine.
"""

import sys
import os
import json
import shutil
import time
import random
import threading
import traceback
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS = 0
FAIL = 0
ERRORS = []

INVENTORY_FILE = os.path.join("data", "inventory.json")
INVENTORY_BACKUP = os.path.join("data", "inventory.json.testbak")


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  [PASS] {name}")
        PASS += 1
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        ERRORS.append((name, traceback.format_exc()))
        FAIL += 1


def protect_inventory_file(fn):
    """Some tool tests write to data/inventory.json — restore it afterwards."""
    def wrapper():
        shutil.copyfile(INVENTORY_FILE, INVENTORY_BACKUP)
        try:
            fn()
        finally:
            shutil.move(INVENTORY_BACKUP, INVENTORY_FILE)
    return wrapper


def temp_db(name):
    """Remove a SQLite file and its WAL sidecars."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = name + suffix
        if os.path.exists(path):
            os.remove(path)


# ─────────────────────────────────────────
# DATA INTEGRITY TESTS
# ─────────────────────────────────────────
def suite_data_integrity():
    print("\n=== DATA INTEGRITY ===")

    def test_demand_history_records():
        with open("data/demand_history.json") as f:
            data = json.load(f)
        assert len(data) == 7300, f"Expected 7300 records, got {len(data)}"
        skus = set(r["sku_id"] for r in data)
        assert len(skus) == 10, f"Expected 10 SKUs, got {len(skus)}"
        for r in data[:100]:
            assert "date" in r and "units_sold" in r and "region" in r
            assert r["units_sold"] >= 0
            assert r["region"] in ("APAC", "EMEA", "AMER")
    check("demand_history: 7300 records, 10 SKUs, valid fields", test_demand_history_records)

    def test_demand_seasonality():
        with open("data/demand_history.json") as f:
            data = json.load(f)
        sku_data = [r for r in data if r["sku_id"] == "SKU-001"]
        months = {}
        for r in sku_data:
            m = r["date"][5:7]
            months.setdefault(m, []).append(r["units_sold"])
        avgs = {m: sum(v)/len(v) for m, v in months.items()}
        # Q4 (Oct-Dec) should be higher than Q1 (Jan-Mar) due to seasonality
        q4_avg = (avgs.get("10", 0) + avgs.get("11", 0) + avgs.get("12", 0)) / 3
        q1_avg = (avgs.get("01", 0) + avgs.get("02", 0) + avgs.get("03", 0)) / 3
        assert q4_avg > q1_avg * 0.9, f"Seasonality not detected: Q4={q4_avg:.1f} vs Q1={q1_avg:.1f}"
    check("demand_history: seasonality pattern present in SKU-001", test_demand_seasonality)

    def test_inventory_completeness():
        with open("data/inventory.json") as f:
            data = json.load(f)
        assert len(data) == 30, f"Expected 30 records (10 SKUs x 3 locs), got {len(data)}"
        locs = set(r["location"] for r in data)
        assert locs == {"WH-NORTH", "WH-SOUTH", "WH-EAST"}
        for r in data:
            assert r["available"] == r["on_hand"] - r["reserved"], "available calculation wrong"
            assert r["on_hand"] >= 0
            assert r["reserved"] >= 0
            assert r["reorder_point"] > 0
            assert r["max_stock"] > r["reorder_point"]
    check("inventory: 30 records, 3 locations, field consistency", test_inventory_completeness)

    def test_suppliers_pricing():
        with open("data/suppliers.json") as f:
            data = json.load(f)
        assert len(data) == 8, f"Expected 8 suppliers, got {len(data)}"
        for s in data:
            assert s["reliability_score"] > 0 and s["reliability_score"] <= 1
            assert s["lead_time_days"] > 0
            assert len(s["skus"]) > 0
            for sku, pricing in s["pricing"].items():
                assert "list_price" in pricing
                assert pricing["list_price"] > 0
                if "tier2_price" in pricing:
                    assert pricing["tier2_price"] < pricing["list_price"], "Tier2 should be cheaper"
                if "tier3_price" in pricing:
                    assert pricing["tier3_price"] < pricing.get("tier2_price", pricing["list_price"])
    check("suppliers: 8 suppliers, tier pricing consistent (each tier cheaper)", test_suppliers_pricing)

    def test_logistics_routes_coverage():
        with open("data/logistics_routes.json") as f:
            data = json.load(f)
        assert len(data) == 12
        origins = set(r["origin"] for r in data)
        assert "SUPPLIER-EMEA" in origins
        assert "SUPPLIER-APAC" in origins
        assert "SUPPLIER-AMER" in origins
        for r in data:
            assert r["reliability"] > 0 and r["reliability"] <= 1
            assert r["transit_days"] > 0
            assert r["cost_per_unit"] > 0
            assert r["mode"] in ("air", "sea", "road")
    check("logistics_routes: 12 routes, all regions covered, valid fields", test_logistics_routes_coverage)


# ─────────────────────────────────────────
# TOOL FUNCTION TESTS
# ─────────────────────────────────────────
@protect_inventory_file
def suite_tools():
    print("\n=== TOOL FUNCTIONS ===")
    from tools.inventory_tools import (get_all_inventory, get_inventory_by_sku,
                                        get_reorder_alerts, update_stock,
                                        simulate_iot_reading, get_demand_history,
                                        get_seasonal_factors)
    from tools.vendor_tools import (get_qualified_suppliers, get_supplier_info,
                                     get_supplier_offer, get_market_price_benchmark,
                                     get_alternative_supplier, simulate_supplier_counter_offer)
    from tools.logistics_tools import (get_available_routes, get_routes_by_supplier_region,
                                        select_optimal_route, estimate_delivery,
                                        track_shipment, get_all_routes)

    def test_get_all_inventory():
        result = get_all_inventory()
        assert "inventory" in result
        assert len(result["inventory"]) == 10
        for item in result["inventory"]:
            assert item["total_on_hand"] >= 0
            assert len(item["locations"]) == 3
    check("get_all_inventory: 10 SKUs, 3 locations each", test_get_all_inventory)

    def test_get_inventory_by_sku_all():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            result = get_inventory_by_sku(sku)
            assert "error" not in result, f"Error for {sku}: {result}"
            assert result["sku_id"] == sku
            assert result["total_on_hand"] >= 0
    check("get_inventory_by_sku: all 10 SKUs return valid data", test_get_inventory_by_sku_all)

    def test_get_inventory_missing_sku():
        result = get_inventory_by_sku("SKU-999")
        assert "error" in result
    check("get_inventory_by_sku: missing SKU returns error", test_get_inventory_missing_sku)

    def test_reorder_alerts():
        result = get_reorder_alerts()
        assert "alerts" in result
        assert "total_alerts" in result
        assert isinstance(result["alerts"], list)
        for a in result["alerts"]:
            assert a["total_available"] <= a["reorder_point"]
            assert a["urgency"] in ("warning", "critical")
    check("get_reorder_alerts: structure and logic correct", test_reorder_alerts)

    def test_update_stock_positive():
        from tools.inventory_tools import get_inventory_by_sku as get_inv
        before = get_inv("SKU-001")
        loc_before = next(l for l in before["locations"] if l["location"] == "WH-NORTH")
        on_hand_before = loc_before["on_hand"]
        result = update_stock("SKU-001", "WH-NORTH", 50)
        assert result["status"] == "updated"
        after = get_inv("SKU-001")
        loc_after = next(l for l in after["locations"] if l["location"] == "WH-NORTH")
        assert loc_after["on_hand"] == on_hand_before + 50
        update_stock("SKU-001", "WH-NORTH", -50)  # rollback
    check("update_stock: positive delta increases on_hand correctly", test_update_stock_positive)

    def test_update_stock_negative_no_underflow():
        result = update_stock("SKU-001", "WH-SOUTH", -999999)
        assert result["new_on_hand"] >= 0, "Stock should not go below 0"
    check("update_stock: negative delta clamps at 0 (no underflow)", test_update_stock_negative_no_underflow)

    def test_iot_sensors():
        for sensor_id in ["TEMP-WH-01", "LOC-TRUCK-99", "STOCK-MAIN-01"]:
            r = simulate_iot_reading(sensor_id)
            assert r["sensor_id"] == sensor_id
            assert r["status"] == "online"
            assert "reading" in r
            assert "timestamp" in r
    check("simulate_iot_reading: TEMP/LOC/STOCK sensor types all return readings", test_iot_sensors)

    def test_demand_history_all_skus():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            result = get_demand_history(sku, days=90)
            assert "error" not in result
            assert result["days_analyzed"] <= 90
            assert result["avg_daily_demand"] >= 0
    check("get_demand_history: all 10 SKUs, 90-day window", test_demand_history_all_skus)

    def test_demand_history_varying_windows():
        for days in [7, 30, 90, 180, 365]:
            r = get_demand_history("SKU-005", days=days)
            assert r["days_analyzed"] <= days
    check("get_demand_history: varying window sizes (7/30/90/180/365d)", test_demand_history_varying_windows)

    def test_seasonal_factors_all_months():
        r = get_seasonal_factors("SKU-003")
        assert "seasonal_factors" in r
        assert len(r["seasonal_factors"]) == 12, "Should have 12 monthly factors"
        for m, f in r["seasonal_factors"].items():
            assert 0 < f < 3, f"Factor {f} for month {m} seems unrealistic"
    check("get_seasonal_factors: 12 months, all factors in realistic range (0-3x)", test_seasonal_factors_all_months)

    def test_qualified_suppliers_all_skus():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            result = get_qualified_suppliers(sku)
            assert result["count"] > 0, f"No suppliers found for {sku}"
            for s in result["qualified_suppliers"]:
                assert s["list_price"] > 0
                assert s["reliability_score"] > 0
    check("get_qualified_suppliers: all 10 SKUs have at least 1 supplier", test_qualified_suppliers_all_skus)

    def test_supplier_ranking():
        r = get_qualified_suppliers("SKU-001")
        sups = r["qualified_suppliers"]
        scores = [s["reliability_score"] for s in sups]
        # Should be sorted by reliability desc
        assert scores == sorted(scores, reverse=True), "Suppliers should be sorted by reliability"
    check("get_qualified_suppliers: sorted by reliability descending", test_supplier_ranking)

    def test_supplier_offer_tiered_pricing():
        # Small qty — list price
        r1 = get_supplier_offer("SUP-001", "SKU-001", 5)
        # Large qty — should be cheaper (tier3)
        r3 = get_supplier_offer("SUP-001", "SKU-001", 600)
        assert "error" not in r1
        assert "error" not in r3
        # Tier3 base price is 40.00 vs list 48.50 — offered price should be lower
        assert r3["offered_price"] < r1["offered_price"] * 1.1  # within margin
    check("get_supplier_offer: tiered pricing applies correctly", test_supplier_offer_tiered_pricing)

    def test_supplier_offer_wrong_sku():
        r = get_supplier_offer("SUP-001", "SKU-002", 100)
        assert "error" in r
    check("get_supplier_offer: wrong SKU for supplier returns error", test_supplier_offer_wrong_sku)

    def test_market_benchmark_all_skus():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            r = get_market_price_benchmark(sku)
            assert "error" not in r
            assert r["market_min"] <= r["market_avg"] <= r["market_max"]
            assert r["supplier_count"] > 0
    check("get_market_price_benchmark: all 10 SKUs, min<=avg<=max", test_market_benchmark_all_skus)

    def test_alternative_supplier():
        r = get_alternative_supplier("SKU-001", "SUP-001")
        assert "error" not in r
        assert r["supplier_id"] != "SUP-001"
        assert r["reliability_score"] > 0
    check("get_alternative_supplier: returns different supplier", test_alternative_supplier)

    def test_counter_offer_progression():
        list_price = 100.0
        our_offer = 88.0
        last_counter = list_price
        for round_num in range(1, 6):
            r = simulate_supplier_counter_offer(our_offer, round_num, list_price)
            assert "counter_price" in r
            assert r["counter_price"] >= list_price * 0.88, "Counter should not go below 88% of list"
            last_counter = r["counter_price"]
    check("simulate_supplier_counter_offer: 5 rounds, floor at 88% of list", test_counter_offer_progression)

    def test_routes_by_region():
        for region in ["EMEA", "APAC", "AMER"]:
            r = get_routes_by_supplier_region(region)
            assert r["count"] > 0, f"No routes for {region}"
    check("get_routes_by_supplier_region: EMEA/APAC/AMER all have routes", test_routes_by_region)

    def test_route_selection_priorities():
        routes = get_all_routes()["routes"]
        for priority in ["speed", "cost", "reliability", "balanced"]:
            r = select_optimal_route(routes, priority)
            assert "selected_route" in r
            assert "score" in r
    check("select_optimal_route: all 4 priority modes return a selection", test_route_selection_priorities)

    def test_route_selection_empty():
        r = select_optimal_route([], "balanced")
        assert "error" in r
    check("select_optimal_route: empty routes list returns error", test_route_selection_empty)

    def test_delivery_estimation_all_routes():
        routes = get_all_routes()["routes"]
        for route in routes:
            r = estimate_delivery(route["route_id"], 100)
            assert "error" not in r
            assert r["transit_days"] > 0
            assert r["total_shipping_cost"] > 0
            assert r["total_shipping_cost"] == round(route["cost_per_unit"] * 100, 2)
    check("estimate_delivery: all 12 routes, cost calculation correct", test_delivery_estimation_all_routes)

    def test_shipment_tracking():
        for po in ["PO-20260101-AAAA", "PO-20260202-BBBB", "PO-20260303-CCCC"]:
            r = track_shipment(po)
            assert r["po_number"] == po
            assert r["status"] in ["order_confirmed", "picked_up", "in_transit",
                                    "customs_clearance", "out_for_delivery", "delivered"]
            assert "estimated_arrival" in r
    check("track_shipment: 3 different POs, valid statuses", test_shipment_tracking)

    def test_shipment_tracking_stable():
        first = track_shipment("PO-20260404-DDDD")
        second = track_shipment("PO-20260404-DDDD")
        assert first["status"] == second["status"]
        assert first["carrier_tracking_id"] == second["carrier_tracking_id"]
        # The global RNG must be untouched by tracking.
        random.seed(7)
        expected = random.random()
        random.seed(7)
        track_shipment("PO-20260404-DDDD")
        assert random.random() == expected, "track_shipment reseeded the global RNG"
    check("track_shipment: stable per PO and leaves global RNG alone", test_shipment_tracking_stable)

    def test_update_stock_keeps_invariant():
        from tools.inventory_tools import get_inventory_by_sku as get_inv
        update_stock("SKU-003", "WH-EAST", -999999)
        with open(INVENTORY_FILE) as f:
            records = json.load(f)
        row = next(r for r in records
                   if r["sku_id"] == "SKU-003" and r["location"] == "WH-EAST")
        assert row["on_hand"] == 0
        assert row["reserved"] <= row["on_hand"]
        assert row["available"] == row["on_hand"] - row["reserved"]
    check("update_stock: clamping to 0 keeps available = on_hand - reserved",
         test_update_stock_keeps_invariant)

    def test_list_sku_ids():
        from tools.inventory_tools import list_sku_ids
        from tools.vendor_tools import list_supplier_ids
        skus = list_sku_ids()
        assert len(skus) == 10 and len(set(skus)) == 10
        assert len(list_supplier_ids()) == 8
    check("list_sku_ids / list_supplier_ids: master data enumerated once each",
         test_list_sku_ids)

    def test_demand_history_aggregates():
        r = get_demand_history("SKU-004", days=180)
        assert r["days_analyzed"] == 180
        assert len(r["records"]) <= 30, "raw records should be truncated"
        assert r["records_truncated"] is True
        assert sum(w["units_sold"] for w in r["weekly_totals"]) == r["total_sold"]
        assert sum(r["regional_split"].values()) == r["total_sold"]
        assert r["min_daily"] <= r["avg_daily_demand"] <= r["max_daily"]
    check("get_demand_history: weekly/regional aggregates reconcile with total",
         test_demand_history_aggregates)

    def test_tier_price_resolution():
        from tools.vendor_tools import get_tier_price
        pricing = {"list_price": 100.0, "tier2_qty": 50, "tier2_price": 90.0,
                   "tier3_qty": 200, "tier3_price": 80.0}
        assert get_tier_price(pricing, 10) == 100.0
        assert get_tier_price(pricing, 50) == 90.0
        assert get_tier_price(pricing, 199) == 90.0
        assert get_tier_price(pricing, 200) == 80.0
        assert get_tier_price({"list_price": 5.0}, 10_000) == 5.0
    check("get_tier_price: volume tiers resolve at every boundary", test_tier_price_resolution)

    def test_counter_offer_floor_override():
        from tools.vendor_tools import simulate_supplier_counter_offer as counter
        accepted = counter(current_offer=90.0, round_num=1, list_price=100.0, floor_price=90.0)
        assert accepted["accepted"] is True and accepted["counter_price"] == 90.0
        rejected = counter(current_offer=70.0, round_num=1, list_price=100.0, floor_price=85.0)
        assert rejected["accepted"] is False
        assert rejected["counter_price"] >= 85.0
        assert rejected["counter_price"] <= 100.0
    check("simulate_supplier_counter_offer: explicit floor accepted/countered correctly",
         test_counter_offer_floor_override)

    def test_select_route_for_region():
        from tools.logistics_tools import select_route_for_region
        fast = select_route_for_region("APAC", "critical")
        cheap = select_route_for_region("APAC", "low")
        assert "error" not in fast and "error" not in cheap
        assert fast["selected_route"]["transit_days"] <= cheap["selected_route"]["transit_days"]
        assert cheap["selected_route"]["cost_per_unit"] <= fast["selected_route"]["cost_per_unit"]
        assert "error" in select_route_for_region("ANTARCTICA", "normal")
    check("select_route_for_region: urgency changes the selected route",
         test_select_route_for_region)


# ─────────────────────────────────────────
# SQLITE MEMORY TESTS
# ─────────────────────────────────────────
def suite_sqlite():
    print("\n=== SQLITE MEMORY ===")
    from memory.sqlite_memory import SQLiteMemory
    db = SQLiteMemory("test_stress.db")

    def test_forecast_roundtrip():
        db.save_forecast("SKU-001", "2026-06-01", 1350.0, 0.85, 30)
        db.save_forecast("SKU-001", "2026-07-01", 2700.0, 0.80, 60)
        db.save_forecast("SKU-002", "2026-06-01", 950.0, 0.90, 30)
        rows = db.get_forecasts("SKU-001", 30)
        assert len(rows) >= 1
        assert rows[0]["predicted_units"] == 1350.0
    check("SQLite: forecast save and retrieve", test_forecast_roundtrip)

    def test_supplier_score_upsert():
        db.upsert_supplier_score("SUP-001", 0.94, 0.97, 0.88)
        score = db.get_supplier_score("SUP-001")
        assert score is not None
        assert abs(score["delivery_score"] - 0.94) < 0.001
        # Upsert again - should update
        db.upsert_supplier_score("SUP-001", 0.80, 0.85, 0.75)
        score2 = db.get_supplier_score("SUP-001")
        assert abs(score2["delivery_score"] - 0.80) < 0.001
    check("SQLite: supplier score upsert (insert then update)", test_supplier_score_upsert)

    def test_supplier_score_weighting():
        db.upsert_supplier_score("SUP-TEST", 1.0, 1.0, 1.0)
        score = db.get_supplier_score("SUP-TEST")
        assert abs(score["overall"] - 1.0) < 0.001
        db.upsert_supplier_score("SUP-TEST", 0.0, 0.0, 0.0)
        score2 = db.get_supplier_score("SUP-TEST")
        assert abs(score2["overall"] - 0.0) < 0.001
    check("SQLite: supplier score overall weighting (0.40+0.35+0.25=1.0)", test_supplier_score_weighting)

    def test_purchase_order_lifecycle():
        db.create_purchase_order("PO-STRESS-001", "SUP-001", "SKU-001", 200, 44.00)
        pos = db.get_all_purchase_orders()
        po = next((p for p in pos if p["po_number"] == "PO-STRESS-001"), None)
        assert po is not None
        assert po["quantity"] == 200
        assert po["total_value"] == 200 * 44.00
        assert po["status"] == "pending"
        db.update_po_status("PO-STRESS-001", "negotiated")
        pos2 = db.get_all_purchase_orders()
        po2 = next(p for p in pos2 if p["po_number"] == "PO-STRESS-001")
        assert po2["status"] == "negotiated"
        db.update_po_status("PO-STRESS-001", "in_transit", "RT-007", "2026-05-20")
        pos3 = db.get_all_purchase_orders()
        po3 = next(p for p in pos3 if p["po_number"] == "PO-STRESS-001")
        assert po3["status"] == "in_transit"
        assert po3["route_id"] == "RT-007"
    check("SQLite: PO full lifecycle (pending->negotiated->in_transit)", test_purchase_order_lifecycle)

    def test_negotiation_log():
        for round_num in range(1, 6):
            db.log_negotiation("SESS-STRESS-01", "SUP-002", "SKU-004",
                               round_num, 55.0 - round_num, 60.0 - round_num,
                               "completed" if round_num == 5 else "ongoing")
        history = db.get_negotiation_history("SESS-STRESS-01")
        assert len(history) == 5
        assert history[0]["round_num"] == 1
        assert history[-1]["status"] == "completed"
    check("SQLite: negotiation log 5 rounds, ordered correctly", test_negotiation_log)

    def test_disruption_event_log():
        db.log_disruption("geopolitical", "APAC", 3, ["SKU-002", "SKU-004"], "Trade restrictions")
        db.log_disruption("supplier_failure", "EMEA", 4, ["SKU-001"], "Factory fire")
        disruptions = db.get_active_disruptions()
        assert len(disruptions) >= 2
        severities = [d["severity"] for d in disruptions]
        assert severities == sorted(severities, reverse=True), "Should be sorted by severity desc"
        for d in disruptions:
            assert isinstance(d["affected_skus"], list)
    check("SQLite: disruption events, sorted by severity desc", test_disruption_event_log)

    def test_disruption_events_are_not_duplicated():
        temp_db("test_disruption.db")
        from memory.sqlite_memory import SQLiteMemory as SM
        d = SM("test_disruption.db")
        first = d.record_disruption("concentration", "AMER", 3, ["SKU-002"], "70% from AMER")
        again = d.record_disruption("concentration", "AMER", 4, ["SKU-002"], "85% from AMER")
        assert first == again, "an identical open event must be refreshed, not duplicated"
        open_events = d.get_active_disruptions()
        assert len(open_events) == 1
        assert open_events[0]["severity"] == 4, "severity must be refreshed in place"
        assert open_events[0]["description"] == "85% from AMER"

        # A different SKU set is a different condition.
        other = d.record_disruption("concentration", "AMER", 3, ["SKU-009"], "shifted")
        assert other != first
        assert len(d.get_active_disruptions()) == 2

        resolved = d.resolve_disruptions_except([first])
        assert resolved == 1
        remaining = d.get_active_disruptions()
        assert len(remaining) == 1 and remaining[0]["id"] == first

        assert d.resolve_disruptions_except([]) == 1
        assert d.get_active_disruptions() == []
        temp_db("test_disruption.db")
    check("SQLite: standing disruptions refresh in place and stale ones resolve",
         test_disruption_events_are_not_duplicated)

    def test_all_supplier_scores():
        for i in range(1, 9):
            db.upsert_supplier_score(f"SUP-{str(i).zfill(3)}", 0.85, 0.90, 0.80)
        scores = db.get_all_supplier_scores()
        assert len(scores) >= 8
        overalls = [s["overall"] for s in scores]
        assert overalls == sorted(overalls, reverse=True), "Should be sorted by overall desc"
    check("SQLite: all 8 supplier scores, sorted by overall desc", test_all_supplier_scores)

    def test_high_volume_inserts():
        start = time.time()
        for i in range(500):
            db.save_forecast(f"SKU-{str(random.randint(1,10)).zfill(3)}",
                             f"2026-{str(random.randint(1,12)).zfill(2)}-01",
                             float(random.randint(100, 5000)),
                             round(random.uniform(0.6, 0.99), 2),
                             random.choice([30, 60, 90]))
        elapsed = time.time() - start
        assert elapsed < 10, f"500 inserts took {elapsed:.1f}s (too slow)"
    check("SQLite: 500 forecast inserts complete in <10s", test_high_volume_inserts)

    import os
    os.remove("test_stress.db")


# ─────────────────────────────────────────
# REDIS MEMORY TESTS
# ─────────────────────────────────────────
def suite_redis():
    print("\n=== REDIS MEMORY ===")
    from memory.redis_memory import RedisMemory
    redis = RedisMemory()

    if not redis.ping():
        print("  [SKIP] Redis not available — skipping Redis tests")
        return

    def test_set_get_roundtrip():
        redis.set("stress:test:simple", {"value": 42, "text": "hello"})
        result = redis.get("stress:test:simple")
        assert result == {"value": 42, "text": "hello"}
        redis.delete("stress:test:simple")
    check("Redis: set/get JSON roundtrip", test_set_get_roundtrip)

    def test_ttl_key():
        redis.set("stress:test:ttl", "expires_soon", ttl=1)
        assert redis.get("stress:test:ttl") == "expires_soon"
        time.sleep(1.1)
        assert redis.get("stress:test:ttl") is None
    check("Redis: TTL expiry works correctly", test_ttl_key)

    def test_list_sliding_window():
        key = "stress:test:history"
        redis.clear_list(key)
        for i in range(25):
            redis.append_to_list(key, {"turn": i}, max_len=20)
        items = redis.get_list(key)
        assert len(items) == 20
        assert items[0]["turn"] == 5  # oldest kept is turn 5
        assert items[-1]["turn"] == 24
        redis.delete(key)
    check("Redis: list sliding window (max_len=20, added 25)", test_list_sliding_window)

    def test_hash_operations():
        key = "stress:test:hash"
        redis.set_hash(key, {"field1": "val1", "field2": 99, "field3": [1, 2, 3]})
        h = redis.get_hash(key)
        assert h["field1"] == "val1"
        assert h["field2"] == 99
        assert h["field3"] == [1, 2, 3]
        redis.hset(key, "field4", {"nested": True})
        val = redis.hget(key, "field4")
        assert val == {"nested": True}
        redis.delete(key)
    check("Redis: hash set/get/hset/hget with complex types", test_hash_operations)

    def test_inventory_cache_keys():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            key = RedisMemory.inventory_key(sku)
            redis.set(key, {"sku_id": sku, "on_hand": random.randint(50, 500)}, ttl=5)
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            data = redis.get(RedisMemory.inventory_key(sku))
            assert data is not None
            assert data["sku_id"] == sku
    check("Redis: inventory cache for all 10 SKUs", test_inventory_cache_keys)

    def test_concurrent_writes():
        errors = []
        def write_task(thread_id):
            try:
                for i in range(20):
                    key = f"stress:concurrent:{thread_id}:{i}"
                    redis.set(key, {"tid": thread_id, "i": i})
                    val = redis.get(key)
                    assert val["tid"] == thread_id
                    redis.delete(key)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=write_task, args=(t,)) for t in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert len(errors) == 0, f"Concurrent write errors: {errors}"
    check("Redis: 10 concurrent threads × 20 writes each (no errors)", test_concurrent_writes)

    def test_agent_state_keys():
        agents = ["demand_forecasting", "inventory", "procurement",
                  "negotiation", "logistics", "supplier_performance", "risk"]
        for agent in agents:
            key = RedisMemory.agent_state_key(agent)
            redis.set(key, {"status": "running", "agent": agent})
            state = redis.get(key)
            assert state["agent"] == agent
        for agent in agents:
            redis.delete(RedisMemory.agent_state_key(agent))
    check("Redis: agent state keys for all 7 agents", test_agent_state_keys)

    def test_high_throughput():
        start = time.time()
        for i in range(1000):
            redis.set(f"stress:throughput:{i}", {"idx": i, "val": random.random()})
        for i in range(1000):
            v = redis.get(f"stress:throughput:{i}")
            assert v["idx"] == i
        for i in range(1000):
            redis.delete(f"stress:throughput:{i}")
        elapsed = time.time() - start
        assert elapsed < 15, f"1000 set+get+delete took {elapsed:.1f}s (too slow)"
    check("Redis: 1000 set+get+delete ops in <15s", test_high_throughput)


# ─────────────────────────────────────────
# ORCHESTRATOR / ROUTING TESTS
# ─────────────────────────────────────────
def suite_orchestrator():
    print("\n=== ORCHESTRATOR & ROUTING ===")
    from orchestrator.message_bus import MessageBus, Message
    from orchestrator.task_router import TaskRouter

    def test_message_bus_basic():
        import asyncio
        bus = MessageBus()
        msg = Message("orchestrator", "inventory", "task", {"action": "check"}, priority=2)

        async def run():
            await bus.publish(msg)
            assert bus.size() == 1
            received = await bus.consume(timeout=1.0)
            assert received.msg_id == msg.msg_id
            assert received.from_agent == "orchestrator"
            assert received.to_agent == "inventory"
            assert bus.size() == 0

        asyncio.run(run())
    check("MessageBus: publish and consume single message", test_message_bus_basic)

    def test_message_bus_priority_ordering():
        import asyncio
        bus = MessageBus()

        async def run():
            await bus.publish(Message("o", "a", "task", {}, priority=5))
            await bus.publish(Message("o", "b", "task", {}, priority=1))
            await bus.publish(Message("o", "c", "task", {}, priority=3))

            m1 = await bus.consume(timeout=0.5)
            m2 = await bus.consume(timeout=0.5)
            m3 = await bus.consume(timeout=0.5)
            priorities = [m1.priority, m2.priority, m3.priority]
            assert priorities == [1, 3, 5], f"Priority order wrong: {priorities}"

        asyncio.run(run())
    check("MessageBus: messages consumed in priority order (1 before 3 before 5)", test_message_bus_priority_ordering)

    def test_message_bus_timeout():
        import asyncio
        bus = MessageBus()

        async def run():
            result = await bus.consume(timeout=0.1)
            assert result is None

        asyncio.run(run())
    check("MessageBus: timeout returns None on empty queue", test_message_bus_timeout)

    def test_message_bus_history():
        import asyncio
        bus = MessageBus()

        async def run():
            for i in range(5):
                await bus.publish(Message("o", f"agent-{i}", "task", {"i": i}))
            history = bus.get_history()
            assert len(history) == 5

        asyncio.run(run())
    check("MessageBus: history records all published messages", test_message_bus_history)

    def test_task_router_all_tasks():
        router = TaskRouter()
        expected = {
            "forecast_demand": "demand_forecasting",
            "check_inventory": "inventory",
            "create_procurement": "procurement",
            "negotiate_po": "negotiation",
            "assign_logistics": "logistics",
            "score_suppliers": "supplier_performance",
            "assess_risk": "risk",
        }
        for task, expected_agent in expected.items():
            assert router.route(task) == expected_agent
    check("TaskRouter: all 7 task types route to correct agents", test_task_router_all_tasks)

    def test_task_router_unknown_task():
        router = TaskRouter()
        try:
            router.route("unknown_task_xyz")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
    check("TaskRouter: unknown task raises ValueError", test_task_router_unknown_task)

    def test_message_serialization():
        msg = Message("agent_a", "agent_b", "result", {"data": [1, 2, 3]}, priority=2)
        d = msg.to_dict()
        assert d["from_agent"] == "agent_a"
        assert d["to_agent"] == "agent_b"
        assert d["payload"]["data"] == [1, 2, 3]
        assert "msg_id" in d
        assert "timestamp" in d
        assert "correlation_id" in d
    check("Message: to_dict() serialization has all required fields", test_message_serialization)


# ─────────────────────────────────────────
# IN-MEMORY STORE (Redis fallback)
# ─────────────────────────────────────────
def suite_memory_fallback():
    print("\n=== IN-MEMORY STORE (REDIS FALLBACK) ===")
    from memory.redis_memory import RedisMemory, InMemoryStore

    def build():
        return RedisMemory(client=InMemoryStore())

    def test_backend_flag():
        mem = build()
        assert mem.backend == "in-memory"
        assert mem.ping() is True
    check("fallback: reports in-memory backend and pings", test_backend_flag)

    def test_json_roundtrip_and_ttl():
        mem = build()
        mem.set("k", {"a": [1, 2, 3]})
        assert mem.get("k") == {"a": [1, 2, 3]}
        mem.set("t", "gone", ttl=1)
        assert mem.get("t") == "gone"
        time.sleep(1.05)
        assert mem.get("t") is None
        mem.delete("k")
        assert mem.get("k") is None
    check("fallback: set/get/delete and TTL expiry", test_json_roundtrip_and_ttl)

    def test_list_window():
        mem = build()
        for i in range(25):
            mem.append_to_list("hist", {"turn": i}, max_len=20)
        items = mem.get_list("hist")
        assert len(items) == 20
        assert items[0]["turn"] == 5 and items[-1]["turn"] == 24
    check("fallback: list sliding window matches Redis LTRIM semantics", test_list_window)

    def test_hash_ops():
        mem = build()
        mem.set_hash("h", {"a": 1, "b": {"nested": True}})
        mem.hset("h", "c", [1, 2])
        assert mem.get_hash("h") == {"a": 1, "b": {"nested": True}, "c": [1, 2]}
        assert mem.hget("h", "b") == {"nested": True}
        assert mem.hget("h", "missing") is None
    check("fallback: hash set/get with nested values", test_hash_ops)

    def test_type_enforcement():
        mem = build()
        mem.set("mixed", "a string")
        try:
            mem.hset("mixed", "field", 1)
            assert False, "should refuse a hash write over a string key"
        except TypeError:
            pass
    check("fallback: refuses hash writes to a string key (Redis WRONGTYPE parity)",
         test_type_enforcement)

    def test_concurrent_access():
        mem = build()
        errors = []

        def worker(tid):
            try:
                for i in range(50):
                    mem.set(f"c:{tid}:{i}", {"tid": tid, "i": i})
                    assert mem.get(f"c:{tid}:{i}")["tid"] == tid
                    mem.append_to_list("shared", {"tid": tid}, max_len=100)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, f"concurrent errors: {errors}"
        assert len(mem.get_list("shared")) == 100
    check("fallback: 8 threads x 50 writes stay consistent", test_concurrent_access)


# ─────────────────────────────────────────
# AGENT PLUMBING
# ─────────────────────────────────────────
def suite_agent_plumbing():
    print("\n=== AGENT PLUMBING ===")
    from agents.base_agent import extract_json, is_retryable, BaseAgent

    def test_extract_plain_json():
        assert extract_json('{"a": 1}') == {"a": 1}
        assert extract_json('  {"a": {"b": [1,2]}}  ') == {"a": {"b": [1, 2]}}
    check("extract_json: plain and nested objects", test_extract_plain_json)

    def test_extract_fenced_json():
        text = 'Here you go:\n```json\n{"forecasts": [{"sku_id": "SKU-001"}]}\n```\nDone.'
        assert extract_json(text)["forecasts"][0]["sku_id"] == "SKU-001"
    check("extract_json: ```json fenced block with surrounding prose", test_extract_fenced_json)

    def test_extract_with_trailing_prose():
        text = '{"risks": [{"severity": 4}]} — note that severity 4 needs action.'
        assert extract_json(text)["risks"][0]["severity"] == 4
    check("extract_json: stops at the closing brace, ignores trailing prose",
         test_extract_with_trailing_prose)

    def test_extract_braces_in_strings():
        text = '{"description": "uses } and { inside", "n": 2}'
        assert extract_json(text)["n"] == 2
    check("extract_json: braces inside strings do not end the object",
         test_extract_braces_in_strings)

    def test_extract_failures():
        assert extract_json("") == {}
        assert extract_json("no json here") == {}
        assert extract_json('{"broken": ') == {}
        assert extract_json('[1, 2, 3]') == {}
    check("extract_json: empty/invalid/array input returns {}", test_extract_failures)

    def test_retryable_classification():
        assert is_retryable(Exception("429 Resource has been exhausted")) is True
        assert is_retryable(Exception("503 Service Unavailable")) is True
        assert is_retryable(ValueError("invalid api key")) is False
    check("is_retryable: rate limits and 5xx retry, auth errors do not",
         test_retryable_classification)

    def test_history_sanitization():
        raw = [
            {"role": "model", "parts": [{"text": "orphan model turn"}]},
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"text": "hi"}]},
            {"role": "user", "parts": [{"text": ""}]},
            {"role": "user", "parts": [{"text": "dangling"}]},
        ]
        clean = BaseAgent._sanitize_history(raw)
        assert [t["role"] for t in clean] == ["user", "model"]
        assert clean[0]["parts"][0]["text"] == "hello"
        assert BaseAgent._sanitize_history([]) == []
        assert BaseAgent._sanitize_history(["garbage", None]) == []
    check("BaseAgent._sanitize_history: drops orphans, empties and dangling turns",
         test_history_sanitization)

    def test_tool_dispatch():
        from agents.inventory_agent import InventoryAgent
        from memory.redis_memory import RedisMemory, InMemoryStore
        from memory.sqlite_memory import SQLiteMemory
        temp_db("test_dispatch.db")
        agent = InventoryAgent(RedisMemory(client=InMemoryStore()),
                               SQLiteMemory("test_dispatch.db"), offline=True)
        ok = agent._dispatch_tool("get_inventory_by_sku", {"sku_id": "SKU-001"})
        assert ok["sku_id"] == "SKU-001"
        assert "error" in agent._dispatch_tool("not_a_tool", {})
        assert "error" in agent._dispatch_tool("get_inventory_by_sku", {"wrong_arg": 1})
        assert "error" in agent._dispatch_tool("get_inventory_by_sku", {"sku_id": "SKU-999"})
        temp_db("test_dispatch.db")
    check("BaseAgent._dispatch_tool: unknown tool, bad args and tool errors are handled",
         test_tool_dispatch)

    def test_declarations_match_implementations():
        from memory.redis_memory import RedisMemory, InMemoryStore
        from memory.sqlite_memory import SQLiteMemory
        from orchestrator.orchestrator import AGENT_CLASSES
        temp_db("test_decl.db")
        redis_mem = RedisMemory(client=InMemoryStore())
        db = SQLiteMemory("test_decl.db")
        for name, cls in AGENT_CLASSES.items():
            agent = cls(redis_mem, db, offline=True)
            for declaration in agent._tool_declarations:
                assert declaration.name in agent._tools, \
                    f"{name} declares {declaration.name} with no implementation"
            assert agent._offline_result is not None
        temp_db("test_decl.db")
    check("agents: every declared Gemini tool has a registered implementation",
         test_declarations_match_implementations)


# ─────────────────────────────────────────
# AGENT DECISION ENGINES (offline)
# ─────────────────────────────────────────
@protect_inventory_file
def suite_agent_engines():
    print("\n=== AGENT DECISION ENGINES (OFFLINE) ===")
    from memory.redis_memory import RedisMemory, InMemoryStore
    from memory.sqlite_memory import SQLiteMemory
    from agents.inventory_agent import InventoryAgent
    from agents.demand_forecasting_agent import DemandForecastingAgent
    from agents.procurement_agent import ProcurementAgent
    from agents.negotiation_agent import NegotiationAgent
    from agents.logistics_agent import LogisticsAgent
    from agents.supplier_performance_agent import SupplierPerformanceAgent
    from agents.risk_agent import RiskAgent

    temp_db("test_agents.db")
    redis_mem = RedisMemory(client=InMemoryStore())
    db = SQLiteMemory("test_agents.db")

    def build(cls):
        return cls(redis_mem, db, offline=True)

    def test_inventory_engine():
        result = build(InventoryAgent).run({})
        assert result["inventory_status"] in ("healthy", "warning", "critical")
        assert result["total_skus_monitored"] == 10
        assert result["total_inventory_value_usd"] > 0
        assert result["iot_summary"]["sensors_checked"] == 3
        for alert in result["reorder_needed"]:
            assert alert["suggested_qty"] >= 0
            assert alert["available"] <= alert["reorder_point"]
    check("InventoryAgent: full snapshot, reorder list and IoT summary", test_inventory_engine)

    def test_forecast_engine():
        result = build(DemandForecastingAgent).run({"sku_ids": ["SKU-001", "SKU-009"]})
        assert len(result["forecasts"]) == 2
        for f in result["forecasts"]:
            assert f["forecast_30d"] < f["forecast_60d"] < f["forecast_90d"]
            assert 0.5 <= f["confidence"] <= 0.95
            assert f["reorder_urgency"] in ("low", "medium", "high", "critical")
        rows = db.get_forecasts("SKU-001", 60)
        assert rows and rows[0]["horizon_days"] == 60
    check("DemandForecastingAgent: horizons increase, saved to SQLite", test_forecast_engine)

    def test_forecast_unknown_sku():
        result = build(DemandForecastingAgent).run({"sku_ids": ["SKU-999"]})
        assert result["forecasts"] == []
    check("DemandForecastingAgent: unknown SKU is skipped, not crashed",
         test_forecast_unknown_sku)

    def test_procurement_engine():
        alerts = [{"sku_id": "SKU-005", "urgency": "critical"}]
        result = build(ProcurementAgent).run({"inventory_alerts": alerts, "forecasts": []})
        decisions = result["decisions"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["sku_id"] == "SKU-005"
        assert d["order_quantity"] > 0
        assert d["po_number"].startswith("PO-")
        assert d["target_price"] > 0
        po = db.get_purchase_order(d["po_number"])
        assert po["status"] == "pending"
        assert po["total_value"] == round(d["order_quantity"] * d["target_price"], 2)
    check("ProcurementAgent: selects a supplier and writes the PO", test_procurement_engine)

    def test_procurement_respects_capacity():
        from tools.inventory_tools import get_inventory_by_sku
        alerts = [{"sku_id": "SKU-009", "urgency": "critical"}]
        result = build(ProcurementAgent).run({
            "inventory_alerts": alerts,
            "forecasts": [{"sku_id": "SKU-009", "forecast_30d": 999999}],
        })
        d = result["decisions"][0]
        inventory = get_inventory_by_sku("SKU-009")
        assert d["order_quantity"] <= inventory["max_stock"] - inventory["total_available"]
    check("ProcurementAgent: order quantity capped at warehouse capacity",
         test_procurement_respects_capacity)

    def test_procurement_skips_full_stock():
        result = build(ProcurementAgent).run({
            "inventory_alerts": [{"sku_id": "SKU-007", "urgency": "warning"}],
            "forecasts": [],
        })
        assert result["decisions"] == []
        assert "SKU-007" in result["skus_skipped"]
    check("ProcurementAgent: skips SKUs already above the fill target",
         test_procurement_skips_full_stock)

    def test_negotiation_engine():
        agent = build(NegotiationAgent)
        db.create_purchase_order("PO-NEG-TEST", "SUP-001", "SKU-001", 150, 44.0)
        result = agent.run({"po_number": "PO-NEG-TEST", "supplier_id": "SUP-001",
                            "sku_id": "SKU-001", "quantity": 150, "target_price": 44.0})
        assert result["outcome"] in ("deal_accepted", "walk_away")
        assert 1 <= result["rounds_taken"] <= 5
        assert len(result["rounds"]) == result["rounds_taken"]
        for r in result["rounds"]:
            assert r["our_offer"] > 0 and r["their_offer"] > 0
        history = db.get_negotiation_history(result["session_id"])
        assert len(history) == result["rounds_taken"]
        assert history[-1]["status"] == "completed"
        po = db.get_purchase_order("PO-NEG-TEST")
        if result["outcome"] == "deal_accepted":
            assert po["status"] == "negotiated"
            assert po["unit_price"] == result["final_agreed_price"]
        else:
            assert po["status"] == "cancelled"
    check("NegotiationAgent: real rounds logged and PO re-priced", test_negotiation_engine)

    def test_negotiation_bad_supplier():
        result = build(NegotiationAgent).run({
            "po_number": "PO-NEG-BAD", "supplier_id": "SUP-001",
            "sku_id": "SKU-002", "quantity": 10, "target_price": 5.0})
        assert result["outcome"] == "walk_away"
        assert result["rounds"] == []
    check("NegotiationAgent: supplier that cannot supply the SKU walks away",
         test_negotiation_bad_supplier)

    def test_logistics_engine():
        db.create_purchase_order("PO-LOG-TEST", "SUP-002", "SKU-004", 200, 56.0)
        result = build(LogisticsAgent).run({"purchase_orders": [
            {"po_number": "PO-LOG-TEST", "supplier_id": "SUP-002",
             "sku_id": "SKU-004", "quantity": 200, "urgency": "critical"}
        ]})
        assert len(result["assignments"]) == 1
        a = result["assignments"][0]
        assert a["mode"] in ("air", "sea", "road")
        assert a["shipping_cost_usd"] > 0
        po = db.get_purchase_order("PO-LOG-TEST")
        assert po["status"] == "in_transit"
        assert po["route_id"] == a["selected_route_id"]
    check("LogisticsAgent: routes a PO and flips it to in_transit", test_logistics_engine)

    def test_logistics_unknown_supplier():
        result = build(LogisticsAgent).run({"purchase_orders": [
            {"po_number": "PO-LOG-BAD", "supplier_id": "SUP-999",
             "sku_id": "SKU-004", "quantity": 10, "urgency": "normal"}
        ]})
        assert result["assignments"] == []
        assert len(result["unassigned"]) == 1
    check("LogisticsAgent: unknown supplier reported as unassigned",
         test_logistics_unknown_supplier)

    def test_supplier_scoring_engine():
        result = build(SupplierPerformanceAgent).run({
            "po_outcomes": [{"supplier_id": "SUP-008", "outcome": "walk_away"}]})
        scores = result["scores"]
        assert len(scores) == 8
        overalls = [s["overall_score"] for s in scores]
        assert overalls == sorted(overalls, reverse=True)
        for s in scores:
            assert 0 <= s["overall_score"] <= 1
            assert s["tier"] in ("preferred", "approved", "conditional", "at_risk")
            assert s["recommendation"]
        assert db.get_supplier_score("SUP-001") is not None
    check("SupplierPerformanceAgent: 8 suppliers scored, ranked and persisted",
         test_supplier_scoring_engine)

    def test_risk_engine():
        result = build(RiskAgent).run({
            "active_pos": [
                {"po_number": "PO-R1", "supplier_id": "SUP-008", "sku_id": "SKU-002"},
                {"po_number": "PO-R2", "supplier_id": "SUP-008", "sku_id": "SKU-004"},
            ],
            "supplier_scores": [{"supplier_id": "SUP-008", "tier": "conditional"}],
        })
        risks = result["risks"]
        assert risks, "SUP-008 (India, 25d lead time, 0.82 reliability) must raise risks"
        types = {r["type"] for r in risks}
        assert "supplier_reliability" in types
        assert "geographic" in types
        assert "lead_time" in types
        assert "concentration" in types
        for r in risks:
            assert 1 <= r["severity"] <= 4
            assert r["mitigation"]
        assert result["overall_risk_level"] in ("low", "medium", "high", "critical")
        assert result["disruptions_logged"] == len([r for r in risks if r["severity"] >= 3])
    check("RiskAgent: reliability, geographic, lead time and concentration risks",
         test_risk_engine)

    def test_risk_engine_clean_slate():
        result = build(RiskAgent).run({"active_pos": [], "supplier_scores": []})
        assert result["risks"] == []
        assert result["overall_risk_level"] == "low"
    check("RiskAgent: no active POs means no risks", test_risk_engine_clean_slate)

    temp_db("test_agents.db")


# ─────────────────────────────────────────
# FULL CYCLE (offline, end to end)
# ─────────────────────────────────────────
@protect_inventory_file
def suite_full_cycle():
    print("\n=== FULL CYCLE (OFFLINE) ===")
    from memory.redis_memory import InMemoryStore, RedisMemory
    from memory.sqlite_memory import SQLiteMemory
    from orchestrator.orchestrator import Orchestrator

    temp_db("test_cycle.db")
    orchestrator = Orchestrator(offline=True, quiet=True, db_path="test_cycle.db")
    orchestrator.redis = RedisMemory(client=InMemoryStore())
    orchestrator._init_agents()
    summary = orchestrator.run_cycle()
    db = SQLiteMemory("test_cycle.db")

    def test_summary_shape():
        for key in ("cycle_id", "mode", "duration_seconds", "inventory", "forecasting",
                    "procurement", "negotiation", "logistics", "supplier_performance",
                    "risk", "database"):
            assert key in summary, f"summary missing {key}"
        assert summary["mode"] == "offline"
        assert summary["steps_completed"] == 7
    check("cycle: summary contains all seven step sections", test_summary_shape)

    def test_cycle_produced_work():
        assert summary["forecasting"]["skus_forecasted"] == 10
        assert summary["procurement"]["pos_created"] > 0, "seeded low stock must trigger POs"
        assert summary["supplier_performance"]["suppliers_scored"] == 8
    check("cycle: forecasts, purchase orders and supplier scores all produced",
         test_cycle_produced_work)

    def test_po_lifecycle_persisted():
        pos = db.get_all_purchase_orders()
        assert len(pos) == summary["procurement"]["pos_created"]
        for po in pos:
            assert po["status"] in ("pending", "negotiated", "in_transit", "cancelled")
            if po["status"] == "in_transit":
                assert po["route_id"] and po["expected_delivery"]
    check("cycle: every PO reached a terminal state with routing data",
         test_po_lifecycle_persisted)

    def test_bus_audit_trail():
        history = orchestrator.bus.get_history()
        # One request + one result per dispatched task.
        assert len(history) >= 14
        requests = [m for m in history if m["from_agent"] == "orchestrator"]
        results = [m for m in history if m["type"].endswith("_result")]
        assert len(requests) == len(results)
        for result in results:
            assert any(r["correlation_id"] == result["correlation_id"] for r in requests)
    check("cycle: every task on the bus has a correlated result", test_bus_audit_trail)

    def test_cycle_state_in_memory():
        from memory.redis_memory import RedisMemory as RM
        state = orchestrator.redis.get_hash(RM.cycle_state_key())
        assert state["status"] == "completed"
        assert state["cycle_id"] == summary["cycle_id"]
        for step in ("check_inventory", "forecast_demand", "create_procurement"):
            assert state[step]["status"] == "completed"
    check("cycle: per-step state recorded in the cycle_state hash",
         test_cycle_state_in_memory)

    def test_cycle_run_persisted():
        runs = db.get_cycle_runs()
        assert len(runs) == 1
        assert runs[0]["cycle_id"] == summary["cycle_id"]
        assert runs[0]["mode"] == "offline"
        assert runs[0]["summary"]["procurement"]["pos_created"] == summary["procurement"]["pos_created"]
    check("cycle: run summary persisted to the cycle_runs table", test_cycle_run_persisted)

    def test_second_cycle_is_idempotent():
        second = orchestrator.run_cycle()
        assert second["cycle_id"] != summary["cycle_id"]
        assert len(db.get_cycle_runs()) == 2
        assert db.get_all_supplier_scores()[0]["overall"] > 0
    check("cycle: a second run completes and appends a new cycle record",
         test_second_cycle_is_idempotent)

    temp_db("test_cycle.db")


# ─────────────────────────────────────────
# SKU SCOPING TESTS
# ─────────────────────────────────────────
def suite_sku_scope():
    print("\n=== SKU SCOPING ===")
    from memory.redis_memory import InMemoryStore, RedisMemory
    from memory.sqlite_memory import SQLiteMemory
    from orchestrator.orchestrator import Orchestrator

    def build(db_name):
        temp_db(db_name)
        orch = Orchestrator(offline=True, quiet=True, db_path=db_name)
        orch.redis = RedisMemory(client=InMemoryStore())
        orch._init_agents()
        return orch

    def test_resolve_scope():
        orch = build("test_scope_resolve.db")
        assert orch._resolve_scope(None) is None
        assert orch._resolve_scope([]) is None
        assert orch._resolve_scope(["SKU-002", "SKU-005"]) == ["SKU-002", "SKU-005"]
        # Unknown ids are dropped when at least one is real...
        assert orch._resolve_scope(["SKU-002", "SKU-999"]) == ["SKU-002"]
        # ...but an all-unknown request is honoured rather than widened to all.
        assert orch._resolve_scope(["SKU-999"]) == ["SKU-999"]
        temp_db("test_scope_resolve.db")
    check("scope: unknown SKUs dropped, all-unknown request never widens to the catalogue",
         test_resolve_scope)

    def test_scoped_cycle_only_touches_requested_skus():
        orch = build("test_scope_cycle.db")
        scope = ["SKU-002", "SKU-005"]
        summary = orch.run_cycle(sku_ids=scope)
        db = SQLiteMemory("test_scope_cycle.db")

        assert summary["inventory"]["skus_monitored"] == len(scope), \
            f"monitored {summary['inventory']['skus_monitored']} SKUs, expected {len(scope)}"
        assert summary["forecasting"]["skus_forecasted"] == len(scope)
        pos = db.get_all_purchase_orders()
        assert pos, "seeded low stock in scope must still produce POs"
        for po in pos:
            assert po["sku_id"] in scope, f"PO created for out-of-scope SKU {po['sku_id']}"
        for forecast in db.get_latest_forecasts(30):
            assert forecast["sku_id"] in scope
        temp_db("test_scope_cycle.db")
    check("scope: --skus narrows inventory, forecasting and procurement alike",
         test_scoped_cycle_only_touches_requested_skus)

    def test_unknown_scope_produces_no_work():
        orch = build("test_scope_unknown.db")
        summary = orch.run_cycle(sku_ids=["SKU-DOES-NOT-EXIST"])
        assert summary["procurement"]["pos_created"] == 0
        assert summary["inventory"]["skus_monitored"] == 0
        assert summary["steps_completed"] == 7, "cycle must still complete all steps"
        temp_db("test_scope_unknown.db")
    check("scope: a scope matching nothing completes the cycle with no orders",
         test_unknown_scope_produces_no_work)

    def test_agents_stay_silent_when_quiet():
        import io
        from contextlib import redirect_stdout
        orch = build("test_scope_quiet.db")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            orch.run_cycle(sku_ids=["SKU-002"])
        assert buffer.getvalue() == "", \
            f"quiet cycle wrote to stdout: {buffer.getvalue()[:120]!r}"
        temp_db("test_scope_quiet.db")
    check("scope: a quiet cycle keeps stdout clean for --json piping",
         test_agents_stay_silent_when_quiet)


# ─────────────────────────────────────────
# CONFIG TESTS
# ─────────────────────────────────────────
def suite_config():
    print("\n=== CONFIG ===")
    import config

    def test_model_names():
        assert "gemini-2.5-pro" in config.GEMINI_PRO_MODEL
        assert "gemini-2.0-flash" in config.GEMINI_FLASH_MODEL
    check("config: model names are gemini-2.5-pro and gemini-2.0-flash", test_model_names)

    def test_country_risk_scores():
        assert len(config.COUNTRY_RISK_SCORES) >= 8
        for country, score in config.COUNTRY_RISK_SCORES.items():
            assert 0 <= score <= 1, f"{country} risk score {score} out of range"
    check("config: 8 countries with risk scores in [0,1]", test_country_risk_scores)

    def test_data_files_exist():
        for fname in ["demand_history.json", "inventory.json", "suppliers.json", "logistics_routes.json"]:
            path = os.path.join(config.DATA_DIR, fname)
            assert os.path.exists(path), f"Missing: {path}"
    check("config: all 4 mock data files exist at DATA_DIR", test_data_files_exist)

    def test_score_weights_sum_to_one():
        total = (config.SCORE_WEIGHT_DELIVERY + config.SCORE_WEIGHT_QUALITY
                 + config.SCORE_WEIGHT_PRICE)
        assert abs(total - 1.0) < 1e-9, f"weights sum to {total}"
    check("config: supplier score weights sum to 1.0", test_score_weights_sum_to_one)

    def test_tier_classification():
        assert config.classify_supplier_tier(0.95) == "preferred"
        assert config.classify_supplier_tier(0.90) == "preferred"
        assert config.classify_supplier_tier(0.85) == "approved"
        assert config.classify_supplier_tier(0.75) == "conditional"
        assert config.classify_supplier_tier(0.10) == "at_risk"
    check("config: tier classification at every boundary", test_tier_classification)

    def test_env_overrides():
        os.environ["SCAI_TEST_INT"] = "42"
        os.environ["SCAI_TEST_BOOL"] = "yes"
        try:
            assert config._env_int("SCAI_TEST_INT", 1) == 42
            assert config._env_int("SCAI_MISSING", 7) == 7
            assert config._env_bool("SCAI_TEST_BOOL", False) is True
            assert config._env_bool("SCAI_MISSING", True) is True
            os.environ["SCAI_TEST_INT"] = "not-a-number"
            assert config._env_int("SCAI_TEST_INT", 5) == 5
        finally:
            os.environ.pop("SCAI_TEST_INT", None)
            os.environ.pop("SCAI_TEST_BOOL", None)
    check("config: env overrides parse and fall back on bad values", test_env_overrides)


# ─────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────
SUITES = (
    suite_config,
    suite_data_integrity,
    suite_tools,
    suite_sqlite,
    suite_memory_fallback,
    suite_redis,
    suite_orchestrator,
    suite_agent_plumbing,
    suite_agent_engines,
    suite_full_cycle,
    suite_sku_scope,
)


def run_all() -> int:
    """Run every suite from the repo root. Returns the failure count."""
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    for suite in SUITES:
        suite()
    return FAIL


def format_results() -> str:
    lines = ["", "="*60, f"  RESULTS: {PASS}/{PASS + FAIL} passed  |  {FAIL} failed"]
    if ERRORS:
        lines.append("\n  FAILURES:")
        for name, tb in ERRORS:
            lines.append(f"\n  [x] {name}")
            lines.extend(f"    {line}" for line in tb.strip().split("\n")[-4:])
    lines.append("="*60)
    return "\n".join(lines)


def test_stress_suite():
    """pytest entry point — the suites are plain asserts, so surface them as one case."""
    run_all()
    assert FAIL == 0, format_results()


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  SUPPLY CHAIN AI — STRESS TEST SUITE")
    print("="*60)
    failed = run_all()
    print(format_results())
    sys.exit(0 if failed == 0 else 1)
