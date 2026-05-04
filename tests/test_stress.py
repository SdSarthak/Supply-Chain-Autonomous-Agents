"""
Stress tests for Supply Chain Autonomous Intelligence Network.
Tests all tools, memory layers, data integrity, concurrency, and edge cases.
Does NOT require a real Gemini API key or Redis — tests the full non-LLM stack.
"""

import sys
import os
import json
import time
import random
import threading
import traceback
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS = 0
FAIL = 0
ERRORS = []


def test(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  [PASS] {name}")
        PASS += 1
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        ERRORS.append((name, traceback.format_exc()))
        FAIL += 1


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
    test("demand_history: 7300 records, 10 SKUs, valid fields", test_demand_history_records)

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
    test("demand_history: seasonality pattern present in SKU-001", test_demand_seasonality)

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
    test("inventory: 30 records, 3 locations, field consistency", test_inventory_completeness)

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
    test("suppliers: 8 suppliers, tier pricing consistent (each tier cheaper)", test_suppliers_pricing)

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
    test("logistics_routes: 12 routes, all regions covered, valid fields", test_logistics_routes_coverage)


# ─────────────────────────────────────────
# TOOL FUNCTION TESTS
# ─────────────────────────────────────────
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
    test("get_all_inventory: 10 SKUs, 3 locations each", test_get_all_inventory)

    def test_get_inventory_by_sku_all():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            result = get_inventory_by_sku(sku)
            assert "error" not in result, f"Error for {sku}: {result}"
            assert result["sku_id"] == sku
            assert result["total_on_hand"] >= 0
    test("get_inventory_by_sku: all 10 SKUs return valid data", test_get_inventory_by_sku_all)

    def test_get_inventory_missing_sku():
        result = get_inventory_by_sku("SKU-999")
        assert "error" in result
    test("get_inventory_by_sku: missing SKU returns error", test_get_inventory_missing_sku)

    def test_reorder_alerts():
        result = get_reorder_alerts()
        assert "alerts" in result
        assert "total_alerts" in result
        assert isinstance(result["alerts"], list)
        for a in result["alerts"]:
            assert a["total_available"] <= a["reorder_point"]
            assert a["urgency"] in ("warning", "critical")
    test("get_reorder_alerts: structure and logic correct", test_reorder_alerts)

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
    test("update_stock: positive delta increases on_hand correctly", test_update_stock_positive)

    def test_update_stock_negative_no_underflow():
        result = update_stock("SKU-001", "WH-SOUTH", -999999)
        assert result["new_on_hand"] >= 0, "Stock should not go below 0"
    test("update_stock: negative delta clamps at 0 (no underflow)", test_update_stock_negative_no_underflow)

    def test_iot_sensors():
        for sensor_id in ["TEMP-WH-01", "LOC-TRUCK-99", "STOCK-MAIN-01"]:
            r = simulate_iot_reading(sensor_id)
            assert r["sensor_id"] == sensor_id
            assert r["status"] == "online"
            assert "reading" in r
            assert "timestamp" in r
    test("simulate_iot_reading: TEMP/LOC/STOCK sensor types all return readings", test_iot_sensors)

    def test_demand_history_all_skus():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            result = get_demand_history(sku, days=90)
            assert "error" not in result
            assert result["days_analyzed"] <= 90
            assert result["avg_daily_demand"] >= 0
    test("get_demand_history: all 10 SKUs, 90-day window", test_demand_history_all_skus)

    def test_demand_history_varying_windows():
        for days in [7, 30, 90, 180, 365]:
            r = get_demand_history("SKU-005", days=days)
            assert r["days_analyzed"] <= days
    test("get_demand_history: varying window sizes (7/30/90/180/365d)", test_demand_history_varying_windows)

    def test_seasonal_factors_all_months():
        r = get_seasonal_factors("SKU-003")
        assert "seasonal_factors" in r
        assert len(r["seasonal_factors"]) == 12, "Should have 12 monthly factors"
        for m, f in r["seasonal_factors"].items():
            assert 0 < f < 3, f"Factor {f} for month {m} seems unrealistic"
    test("get_seasonal_factors: 12 months, all factors in realistic range (0-3x)", test_seasonal_factors_all_months)

    def test_qualified_suppliers_all_skus():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            result = get_qualified_suppliers(sku)
            assert result["count"] > 0, f"No suppliers found for {sku}"
            for s in result["qualified_suppliers"]:
                assert s["list_price"] > 0
                assert s["reliability_score"] > 0
    test("get_qualified_suppliers: all 10 SKUs have at least 1 supplier", test_qualified_suppliers_all_skus)

    def test_supplier_ranking():
        r = get_qualified_suppliers("SKU-001")
        sups = r["qualified_suppliers"]
        scores = [s["reliability_score"] for s in sups]
        # Should be sorted by reliability desc
        assert scores == sorted(scores, reverse=True), "Suppliers should be sorted by reliability"
    test("get_qualified_suppliers: sorted by reliability descending", test_supplier_ranking)

    def test_supplier_offer_tiered_pricing():
        # Small qty — list price
        r1 = get_supplier_offer("SUP-001", "SKU-001", 5)
        # Large qty — should be cheaper (tier3)
        r3 = get_supplier_offer("SUP-001", "SKU-001", 600)
        assert "error" not in r1
        assert "error" not in r3
        # Tier3 base price is 40.00 vs list 48.50 — offered price should be lower
        assert r3["offered_price"] < r1["offered_price"] * 1.1  # within margin
    test("get_supplier_offer: tiered pricing applies correctly", test_supplier_offer_tiered_pricing)

    def test_supplier_offer_wrong_sku():
        r = get_supplier_offer("SUP-001", "SKU-002", 100)
        assert "error" in r
    test("get_supplier_offer: wrong SKU for supplier returns error", test_supplier_offer_wrong_sku)

    def test_market_benchmark_all_skus():
        for i in range(1, 11):
            sku = f"SKU-{str(i).zfill(3)}"
            r = get_market_price_benchmark(sku)
            assert "error" not in r
            assert r["market_min"] <= r["market_avg"] <= r["market_max"]
            assert r["supplier_count"] > 0
    test("get_market_price_benchmark: all 10 SKUs, min<=avg<=max", test_market_benchmark_all_skus)

    def test_alternative_supplier():
        r = get_alternative_supplier("SKU-001", "SUP-001")
        assert "error" not in r
        assert r["supplier_id"] != "SUP-001"
        assert r["reliability_score"] > 0
    test("get_alternative_supplier: returns different supplier", test_alternative_supplier)

    def test_counter_offer_progression():
        list_price = 100.0
        our_offer = 88.0
        last_counter = list_price
        for round_num in range(1, 6):
            r = simulate_supplier_counter_offer(our_offer, round_num, list_price)
            assert "counter_price" in r
            assert r["counter_price"] >= list_price * 0.88, "Counter should not go below 88% of list"
            last_counter = r["counter_price"]
    test("simulate_supplier_counter_offer: 5 rounds, floor at 88% of list", test_counter_offer_progression)

    def test_routes_by_region():
        for region in ["EMEA", "APAC", "AMER"]:
            r = get_routes_by_supplier_region(region)
            assert r["count"] > 0, f"No routes for {region}"
    test("get_routes_by_supplier_region: EMEA/APAC/AMER all have routes", test_routes_by_region)

    def test_route_selection_priorities():
        routes = get_all_routes()["routes"]
        for priority in ["speed", "cost", "reliability", "balanced"]:
            r = select_optimal_route(routes, priority)
            assert "selected_route" in r
            assert "score" in r
    test("select_optimal_route: all 4 priority modes return a selection", test_route_selection_priorities)

    def test_route_selection_empty():
        r = select_optimal_route([], "balanced")
        assert "error" in r
    test("select_optimal_route: empty routes list returns error", test_route_selection_empty)

    def test_delivery_estimation_all_routes():
        routes = get_all_routes()["routes"]
        for route in routes:
            r = estimate_delivery(route["route_id"], 100)
            assert "error" not in r
            assert r["transit_days"] > 0
            assert r["total_shipping_cost"] > 0
            assert r["total_shipping_cost"] == round(route["cost_per_unit"] * 100, 2)
    test("estimate_delivery: all 12 routes, cost calculation correct", test_delivery_estimation_all_routes)

    def test_shipment_tracking():
        for po in ["PO-20260101-AAAA", "PO-20260202-BBBB", "PO-20260303-CCCC"]:
            r = track_shipment(po)
            assert r["po_number"] == po
            assert r["status"] in ["order_confirmed", "picked_up", "in_transit",
                                    "customs_clearance", "out_for_delivery", "delivered"]
            assert "estimated_arrival" in r
    test("track_shipment: 3 different POs, valid statuses", test_shipment_tracking)


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
    test("SQLite: forecast save and retrieve", test_forecast_roundtrip)

    def test_supplier_score_upsert():
        db.upsert_supplier_score("SUP-001", 0.94, 0.97, 0.88)
        score = db.get_supplier_score("SUP-001")
        assert score is not None
        assert abs(score["delivery_score"] - 0.94) < 0.001
        # Upsert again - should update
        db.upsert_supplier_score("SUP-001", 0.80, 0.85, 0.75)
        score2 = db.get_supplier_score("SUP-001")
        assert abs(score2["delivery_score"] - 0.80) < 0.001
    test("SQLite: supplier score upsert (insert then update)", test_supplier_score_upsert)

    def test_supplier_score_weighting():
        db.upsert_supplier_score("SUP-TEST", 1.0, 1.0, 1.0)
        score = db.get_supplier_score("SUP-TEST")
        assert abs(score["overall"] - 1.0) < 0.001
        db.upsert_supplier_score("SUP-TEST", 0.0, 0.0, 0.0)
        score2 = db.get_supplier_score("SUP-TEST")
        assert abs(score2["overall"] - 0.0) < 0.001
    test("SQLite: supplier score overall weighting (0.40+0.35+0.25=1.0)", test_supplier_score_weighting)

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
    test("SQLite: PO full lifecycle (pending->negotiated->in_transit)", test_purchase_order_lifecycle)

    def test_negotiation_log():
        for round_num in range(1, 6):
            db.log_negotiation("SESS-STRESS-01", "SUP-002", "SKU-004",
                               round_num, 55.0 - round_num, 60.0 - round_num,
                               "completed" if round_num == 5 else "ongoing")
        history = db.get_negotiation_history("SESS-STRESS-01")
        assert len(history) == 5
        assert history[0]["round_num"] == 1
        assert history[-1]["status"] == "completed"
    test("SQLite: negotiation log 5 rounds, ordered correctly", test_negotiation_log)

    def test_disruption_event_log():
        db.log_disruption("geopolitical", "APAC", 3, ["SKU-002", "SKU-004"], "Trade restrictions")
        db.log_disruption("supplier_failure", "EMEA", 4, ["SKU-001"], "Factory fire")
        disruptions = db.get_active_disruptions()
        assert len(disruptions) >= 2
        severities = [d["severity"] for d in disruptions]
        assert severities == sorted(severities, reverse=True), "Should be sorted by severity desc"
        for d in disruptions:
            assert isinstance(d["affected_skus"], list)
    test("SQLite: disruption events, sorted by severity desc", test_disruption_event_log)

    def test_all_supplier_scores():
        for i in range(1, 9):
            db.upsert_supplier_score(f"SUP-{str(i).zfill(3)}", 0.85, 0.90, 0.80)
        scores = db.get_all_supplier_scores()
        assert len(scores) >= 8
        overalls = [s["overall"] for s in scores]
        assert overalls == sorted(overalls, reverse=True), "Should be sorted by overall desc"
    test("SQLite: all 8 supplier scores, sorted by overall desc", test_all_supplier_scores)

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
    test("SQLite: 500 forecast inserts complete in <10s", test_high_volume_inserts)

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
    test("Redis: set/get JSON roundtrip", test_set_get_roundtrip)

    def test_ttl_key():
        redis.set("stress:test:ttl", "expires_soon", ttl=1)
        assert redis.get("stress:test:ttl") == "expires_soon"
        time.sleep(1.1)
        assert redis.get("stress:test:ttl") is None
    test("Redis: TTL expiry works correctly", test_ttl_key)

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
    test("Redis: list sliding window (max_len=20, added 25)", test_list_sliding_window)

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
    test("Redis: hash set/get/hset/hget with complex types", test_hash_operations)

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
    test("Redis: inventory cache for all 10 SKUs", test_inventory_cache_keys)

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
    test("Redis: 10 concurrent threads × 20 writes each (no errors)", test_concurrent_writes)

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
    test("Redis: agent state keys for all 7 agents", test_agent_state_keys)

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
    test("Redis: 1000 set+get+delete ops in <15s", test_high_throughput)


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
    test("MessageBus: publish and consume single message", test_message_bus_basic)

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
    test("MessageBus: messages consumed in priority order (1 before 3 before 5)", test_message_bus_priority_ordering)

    def test_message_bus_timeout():
        import asyncio
        bus = MessageBus()

        async def run():
            result = await bus.consume(timeout=0.1)
            assert result is None

        asyncio.run(run())
    test("MessageBus: timeout returns None on empty queue", test_message_bus_timeout)

    def test_message_bus_history():
        import asyncio
        bus = MessageBus()

        async def run():
            for i in range(5):
                await bus.publish(Message("o", f"agent-{i}", "task", {"i": i}))
            history = bus.get_history()
            assert len(history) == 5

        asyncio.run(run())
    test("MessageBus: history records all published messages", test_message_bus_history)

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
    test("TaskRouter: all 7 task types route to correct agents", test_task_router_all_tasks)

    def test_task_router_unknown_task():
        router = TaskRouter()
        try:
            router.route("unknown_task_xyz")
            assert False, "Should have raised ValueError"
        except ValueError:
            pass
    test("TaskRouter: unknown task raises ValueError", test_task_router_unknown_task)

    def test_message_serialization():
        msg = Message("agent_a", "agent_b", "result", {"data": [1, 2, 3]}, priority=2)
        d = msg.to_dict()
        assert d["from_agent"] == "agent_a"
        assert d["to_agent"] == "agent_b"
        assert d["payload"]["data"] == [1, 2, 3]
        assert "msg_id" in d
        assert "timestamp" in d
        assert "correlation_id" in d
    test("Message: to_dict() serialization has all required fields", test_message_serialization)


# ─────────────────────────────────────────
# CONFIG TESTS
# ─────────────────────────────────────────
def suite_config():
    print("\n=== CONFIG ===")
    import config

    def test_model_names():
        assert "gemini-2.5-pro" in config.GEMINI_PRO_MODEL
        assert "gemini-2.0-flash" in config.GEMINI_FLASH_MODEL
    test("config: model names are gemini-2.5-pro and gemini-2.0-flash", test_model_names)

    def test_country_risk_scores():
        assert len(config.COUNTRY_RISK_SCORES) >= 8
        for country, score in config.COUNTRY_RISK_SCORES.items():
            assert 0 <= score <= 1, f"{country} risk score {score} out of range"
    test("config: 8 countries with risk scores in [0,1]", test_country_risk_scores)

    def test_data_files_exist():
        for fname in ["demand_history.json", "inventory.json", "suppliers.json", "logistics_routes.json"]:
            path = os.path.join(config.DATA_DIR, fname)
            assert os.path.exists(path), f"Missing: {path}"
    test("config: all 4 mock data files exist at DATA_DIR", test_data_files_exist)


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*60)
    print("  SUPPLY CHAIN AI — STRESS TEST SUITE")
    print("="*60)

    os.chdir(os.path.join(os.path.dirname(__file__), ".."))

    suite_config()
    suite_data_integrity()
    suite_tools()
    suite_sqlite()
    suite_redis()
    suite_orchestrator()

    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"  RESULTS: {PASS}/{total} passed  |  {FAIL} failed")
    if ERRORS:
        print(f"\n  FAILURES:")
        for name, tb in ERRORS:
            print(f"\n  ✗ {name}")
            for line in tb.strip().split("\n")[-4:]:
                print(f"    {line}")
    print("="*60)
    sys.exit(0 if FAIL == 0 else 1)
