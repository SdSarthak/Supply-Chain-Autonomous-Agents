import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from orchestrator.message_bus import MessageBus, Message
from orchestrator.task_router import TaskRouter

from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.inventory_agent import InventoryAgent
from agents.procurement_agent import ProcurementAgent
from agents.negotiation_agent import NegotiationAgent
from agents.logistics_agent import LogisticsAgent
from agents.supplier_performance_agent import SupplierPerformanceAgent
from agents.risk_agent import RiskAgent

logger = logging.getLogger(__name__)

ALL_SKUS = [f"SKU-{str(i).zfill(3)}" for i in range(1, 11)]
ALL_SUPPLIERS = [f"SUP-{str(i).zfill(3)}" for i in range(1, 9)]


class Orchestrator:
    def __init__(self):
        print("\n" + "="*60)
        print("  SUPPLY CHAIN AUTONOMOUS INTELLIGENCE NETWORK")
        print("  Initializing...")
        print("="*60)

        self.redis = RedisMemory()
        self.sqlite = SQLiteMemory()
        self.bus = MessageBus()
        self.router = TaskRouter()

        self._init_agents()
        print("  All agents initialized.\n")

    def _init_agents(self):
        self.agents = {
            "demand_forecasting": DemandForecastingAgent(self.redis, self.sqlite),
            "inventory": InventoryAgent(self.redis, self.sqlite),
            "procurement": ProcurementAgent(self.redis, self.sqlite),
            "negotiation": NegotiationAgent(self.redis, self.sqlite),
            "logistics": LogisticsAgent(self.redis, self.sqlite),
            "supplier_performance": SupplierPerformanceAgent(self.redis, self.sqlite),
            "risk": RiskAgent(self.redis, self.sqlite),
        }

    def _run_agent(self, agent_name: str, task: dict) -> dict:
        agent = self.agents.get(agent_name)
        if not agent:
            return {"error": f"Agent {agent_name} not found"}
        try:
            return agent.run(task)
        except Exception as e:
            logger.error(f"Agent {agent_name} failed: {e}")
            return {"error": str(e), "agent": agent_name}

    def _update_cycle_state(self, step: str, data: Any = None):
        self.redis.hset(RedisMemory.cycle_state_key(), step, {
            "status": "completed",
            "timestamp": datetime.utcnow().isoformat(),
            "summary": str(data)[:200] if data else ""
        })

    def run_cycle(self) -> dict:
        cycle_start = datetime.utcnow()
        print(f"\n{'='*60}")
        print(f"  CYCLE START: {cycle_start.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"{'='*60}\n")

        self.redis.set(RedisMemory.cycle_state_key(), {"status": "running", "started_at": cycle_start.isoformat()})

        # --- STEP 1: Inventory Check ---
        print("\n[STEP 1/7] INVENTORY MONITORING")
        print("-" * 40)
        inventory_result = self._run_agent("inventory", {})
        alerts = inventory_result.get("alerts", [])
        reorder_needed = inventory_result.get("reorder_needed", alerts)
        self._update_cycle_state("inventory", inventory_result.get("inventory_status"))
        print(f"  Status: {inventory_result.get('inventory_status', 'unknown')} | Alerts: {len(alerts)}")

        # --- STEP 2: Demand Forecasting ---
        print("\n[STEP 2/7] DEMAND FORECASTING")
        print("-" * 40)
        forecast_result = self._run_agent("demand_forecasting", {"sku_ids": ALL_SKUS})
        forecasts = forecast_result.get("forecasts", [])
        self._update_cycle_state("forecasting", f"{len(forecasts)} forecasts")
        print(f"  Forecasts generated: {len(forecasts)}")

        # --- STEP 3: Procurement Decisions ---
        print("\n[STEP 3/7] PROCUREMENT DECISIONS")
        print("-" * 40)
        procurement_result = self._run_agent("procurement", {
            "inventory_alerts": reorder_needed,
            "forecasts": forecasts
        })
        decisions = procurement_result.get("decisions", [])
        self._update_cycle_state("procurement", f"{len(decisions)} POs")
        print(f"  Purchase orders created: {len(decisions)}")
        for d in decisions:
            print(f"    PO {d.get('po_number','?')}: {d.get('sku_id')} x{d.get('order_quantity')} from {d.get('selected_supplier_name','?')}")

        # --- STEP 4: Negotiation ---
        print("\n[STEP 4/7] PRICE NEGOTIATION")
        print("-" * 40)
        negotiation_results = []
        for decision in decisions:
            if decision.get("action") == "create_po" or decision.get("po_number"):
                neg_result = self._run_agent("negotiation", {
                    "po_number": decision.get("po_number", "UNKNOWN"),
                    "supplier_id": decision.get("selected_supplier_id"),
                    "sku_id": decision.get("sku_id"),
                    "quantity": decision.get("order_quantity", 0),
                    "target_price": decision.get("target_price", 0.0)
                })
                negotiation_results.append(neg_result)
                outcome = neg_result.get("outcome", "unknown")
                discount = neg_result.get("discount_achieved_pct", 0)
                print(f"    {decision.get('po_number','?')}: {outcome} | discount={discount:.1f}%")
        self._update_cycle_state("negotiation", f"{len(negotiation_results)} negotiations")

        # --- STEP 5: Logistics Assignment ---
        print("\n[STEP 5/7] LOGISTICS ASSIGNMENT")
        print("-" * 40)
        negotiated_pos = []
        for d, n in zip(decisions, negotiation_results):
            if n.get("outcome") != "walk_away":
                negotiated_pos.append({
                    "po_number": d.get("po_number"),
                    "supplier_id": d.get("selected_supplier_id"),
                    "sku_id": d.get("sku_id"),
                    "quantity": d.get("order_quantity"),
                    "urgency": d.get("urgency", "normal")
                })

        logistics_result = self._run_agent("logistics", {"purchase_orders": negotiated_pos})
        assignments = logistics_result.get("assignments", [])
        self._update_cycle_state("logistics", f"{len(assignments)} routes assigned")
        print(f"  Routes assigned: {len(assignments)}")

        # --- STEP 6: Supplier Scoring ---
        print("\n[STEP 6/7] SUPPLIER PERFORMANCE SCORING")
        print("-" * 40)
        po_outcomes = []
        for n in negotiation_results:
            po_outcomes.append({
                "po_number": n.get("po_number"),
                "supplier_id": n.get("supplier_id"),
                "outcome": n.get("outcome"),
                "discount_pct": n.get("discount_achieved_pct", 0)
            })
        scoring_result = self._run_agent("supplier_performance", {
            "supplier_ids": ALL_SUPPLIERS,
            "po_outcomes": po_outcomes
        })
        scores = scoring_result.get("scores", [])
        self._update_cycle_state("supplier_scoring", f"{len(scores)} suppliers scored")
        print(f"  Suppliers scored: {len(scores)}")

        # --- STEP 7: Risk Assessment ---
        print("\n[STEP 7/7] RISK & RESILIENCE ASSESSMENT")
        print("-" * 40)
        all_pos = self.sqlite.get_all_purchase_orders()
        active_pos = [p for p in all_pos if p["status"] in ("pending", "negotiated", "in_transit")]
        risk_result = self._run_agent("risk", {
            "active_pos": active_pos[:10],
            "supplier_scores": scores
        })
        risks = risk_result.get("risks", [])
        self._update_cycle_state("risk", f"{len(risks)} risks identified")

        # --- FINAL SUMMARY ---
        cycle_end = datetime.utcnow()
        duration = (cycle_end - cycle_start).total_seconds()

        all_scores = self.sqlite.get_all_supplier_scores()
        total_po_value = sum(p.get("total_value", 0) for p in self.sqlite.get_all_purchase_orders()
                             if p["status"] != "cancelled")

        summary = {
            "cycle_id": cycle_start.strftime("%Y%m%d%H%M%S"),
            "started_at": cycle_start.isoformat(),
            "completed_at": cycle_end.isoformat(),
            "duration_seconds": round(duration, 1),
            "steps_completed": 7,
            "inventory": {
                "status": inventory_result.get("inventory_status", "unknown"),
                "alerts": len(alerts),
                "skus_monitored": inventory_result.get("total_skus_monitored", 10)
            },
            "forecasting": {
                "skus_forecasted": len(forecasts)
            },
            "procurement": {
                "pos_created": len(decisions),
                "total_value_usd": sum(d.get("total_estimated_value", 0) for d in decisions)
            },
            "negotiation": {
                "deals_completed": len([n for n in negotiation_results if n.get("outcome") == "deal_accepted"]),
                "deals_walked": len([n for n in negotiation_results if n.get("outcome") == "walk_away"]),
                "avg_discount_pct": round(
                    sum(n.get("discount_achieved_pct", 0) for n in negotiation_results) /
                    max(len(negotiation_results), 1), 2
                )
            },
            "logistics": {
                "routes_assigned": len(assignments)
            },
            "supplier_performance": {
                "suppliers_scored": len(scores),
                "preferred": len([s for s in scores if s.get("tier") == "preferred"]),
                "at_risk": len([s for s in scores if s.get("tier") == "at_risk"])
            },
            "risk": {
                "total_risks": len(risks),
                "critical": len([r for r in risks if r.get("severity", 0) >= 4]),
                "high": len([r for r in risks if r.get("severity", 0) == 3]),
                "overall_level": risk_result.get("overall_risk_level", "unknown")
            },
            "database": {
                "total_pos": len(self.sqlite.get_all_purchase_orders()),
                "total_value_usd": round(total_po_value, 2),
                "supplier_scores_saved": len(all_scores)
            }
        }

        print(f"\n{'='*60}")
        print("  CYCLE COMPLETE")
        print(f"{'='*60}")
        print(json.dumps(summary, indent=2))
        print("="*60)

        return summary
