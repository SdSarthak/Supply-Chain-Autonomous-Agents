import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from config import GEMINI_API_KEY, OFFLINE_MODE
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from orchestrator.message_bus import MessageBus
from orchestrator.task_router import TaskRouter

from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.inventory_agent import InventoryAgent
from agents.procurement_agent import ProcurementAgent
from agents.negotiation_agent import NegotiationAgent
from agents.logistics_agent import LogisticsAgent
from agents.supplier_performance_agent import SupplierPerformanceAgent
from agents.risk_agent import RiskAgent

from tools.inventory_tools import list_sku_ids
from tools.vendor_tools import list_supplier_ids

logger = logging.getLogger(__name__)

# Bus priorities: 1 = critical path, 5 = background.
TASK_PRIORITIES = {
    "check_inventory": 1,
    "forecast_demand": 2,
    "create_procurement": 1,
    "negotiate_po": 2,
    "assign_logistics": 2,
    "score_suppliers": 4,
    "assess_risk": 3,
}

AGENT_CLASSES = {
    "demand_forecasting": DemandForecastingAgent,
    "inventory": InventoryAgent,
    "procurement": ProcurementAgent,
    "negotiation": NegotiationAgent,
    "logistics": LogisticsAgent,
    "supplier_performance": SupplierPerformanceAgent,
    "risk": RiskAgent,
}


class Orchestrator:
    """Runs the seven-step procurement cycle.

    Every step is dispatched as a message on the priority bus and routed to an
    agent by task type, so the bus history is a complete audit trail of the run.
    """

    def __init__(self, offline: Optional[bool] = None,
                 allow_memory_fallback: bool = False, quiet: bool = False,
                 db_path: Optional[str] = None):
        self.quiet = quiet
        self.offline = OFFLINE_MODE or not GEMINI_API_KEY if offline is None else offline

        self._banner("SUPPLY CHAIN AUTONOMOUS INTELLIGENCE NETWORK", "Initializing...")

        self.redis = RedisMemory(allow_fallback=allow_memory_fallback)
        self.sqlite = SQLiteMemory(db_path) if db_path else SQLiteMemory()
        self.bus = MessageBus()
        self.router = TaskRouter()

        self.sku_ids = list_sku_ids()
        self.supplier_ids = list_supplier_ids()
        self._init_agents()

        self._print(f"  Mode: {self.mode} | memory: {self.redis.backend} | "
                    f"{len(self.sku_ids)} SKUs, {len(self.supplier_ids)} suppliers")
        self._print("  All agents initialized.\n")

    @property
    def mode(self) -> str:
        return "offline (deterministic engines)" if self.offline else "gemini"

    def _init_agents(self):
        self.agents = {
            name: cls(self.redis, self.sqlite, offline=self.offline)
            for name, cls in AGENT_CLASSES.items()
        }
        # In quiet mode stdout carries the summary JSON only, so agents must
        # not narrate onto it.
        for agent in self.agents.values():
            agent.quiet = self.quiet

    # ── output helpers ───────────────────────────────────────
    def _print(self, msg: str = "") -> None:
        if not self.quiet:
            print(msg)

    def _banner(self, *lines: str) -> None:
        self._print("\n" + "=" * 60)
        for line in lines:
            self._print(f"  {line}")
        self._print("=" * 60)

    def _step(self, index: int, title: str) -> None:
        self._print(f"\n[STEP {index}/7] {title}")
        self._print("-" * 40)

    # ── dispatch ─────────────────────────────────────────────
    async def _dispatch(self, task_type: str, payload: dict) -> dict:
        """Publish a task, pull it off the bus, route it, and run the agent."""
        message = self.router.build_message(
            task_type, payload, priority=TASK_PRIORITIES.get(task_type, 3)
        )
        await self.bus.publish(message)
        queued = await self.bus.consume(timeout=5.0)
        if queued is None:
            logger.error("Task %s was published but never delivered", task_type)
            return {"error": f"Task {task_type} was not delivered", "agent": message.to_agent}

        agent = self.agents.get(queued.to_agent)
        if agent is None:
            return {"error": f"Agent {queued.to_agent} not found", "agent": queued.to_agent}

        loop = asyncio.get_running_loop()
        try:
            # Agents are blocking (network + SQLite), so keep the loop free.
            result = await loop.run_in_executor(None, agent.run, queued.payload)
        except Exception as e:
            logger.exception("Agent %s failed", queued.to_agent)
            self._print(f"  ! {queued.to_agent} failed: {e}")
            result = {"error": str(e), "agent": queued.to_agent}

        await self.bus.publish(self.router.build_result(queued, result))
        await self.bus.consume(timeout=1.0)
        self._update_cycle_state(task_type, result)
        return result

    def _update_cycle_state(self, step: str, data: Any = None) -> None:
        self.redis.hset(RedisMemory.cycle_state_key(), step, {
            "status": "failed" if isinstance(data, dict) and data.get("error") else "completed",
            "timestamp": datetime.utcnow().isoformat(),
            "summary": str(data)[:200] if data else ""
        })

    def _resolve_scope(self, sku_ids: Optional[list]) -> Optional[list]:
        """Validate a requested SKU subset. None means "the whole catalogue".

        Unknown ids are reported and dropped. If the caller asked only for ids
        that do not exist, the request is honoured as-is so the cycle reports
        no work rather than silently widening to every SKU.
        """
        requested = [s for s in (sku_ids or []) if s]
        if not requested:
            return None
        known = set(self.sku_ids)
        scoped = [s for s in requested if s in known]
        unknown = [s for s in requested if s not in known]
        if unknown:
            logger.warning("Unknown SKU ids requested: %s", ", ".join(unknown))
            self._print(f"  ! Unknown SKU ids ignored: {', '.join(unknown)}")
        if not scoped:
            self._print("  ! None of the requested SKUs exist in the catalogue.")
            return requested
        return scoped

    # ── cycle ────────────────────────────────────────────────
    def run_cycle(self, sku_ids: Optional[list] = None) -> dict:
        """Synchronous entry point — runs the async cycle to completion."""
        return asyncio.run(self.run_cycle_async(sku_ids))

    async def run_cycle_async(self, sku_ids: Optional[list] = None) -> dict:
        # `scope` is set only when the caller asked for a subset, so agents can
        # tell "every SKU" apart from "these SKUs" and narrow their own steps.
        scope = self._resolve_scope(sku_ids)
        sku_ids = scope or self.sku_ids
        cycle_start = datetime.utcnow()
        # Millisecond precision: cycle_runs keys on cycle_id, so two cycles
        # started in the same second would otherwise overwrite one another.
        cycle_id = cycle_start.strftime("%Y%m%d%H%M%S%f")[:-3]
        self._banner(f"CYCLE START: {cycle_start.strftime('%Y-%m-%d %H:%M:%S UTC')}",
                     f"Cycle {cycle_id} | mode: {self.mode}")

        # cycle_state is a hash — clear the previous run before writing this one.
        self.redis.delete(RedisMemory.cycle_state_key())
        self.redis.set_hash(RedisMemory.cycle_state_key(),
                            {"status": "running", "cycle_id": cycle_id,
                             "started_at": cycle_start.isoformat()})

        # --- STEP 1: Inventory Check ---
        self._step(1, "INVENTORY MONITORING")
        inventory_result = await self._dispatch("check_inventory", {"sku_ids": scope})
        reorder_needed = inventory_result.get("reorder_needed") or inventory_result.get("alerts", [])
        self._print(f"  Status: {inventory_result.get('inventory_status', 'unknown')} | "
                    f"Reorder candidates: {len(reorder_needed)}")

        # --- STEP 2: Demand Forecasting ---
        self._step(2, "DEMAND FORECASTING")
        forecast_result = await self._dispatch("forecast_demand", {"sku_ids": sku_ids})
        forecasts = forecast_result.get("forecasts", [])
        self._print(f"  Forecasts generated: {len(forecasts)}")

        # --- STEP 3: Procurement Decisions ---
        self._step(3, "PROCUREMENT DECISIONS")
        procurement_result = await self._dispatch("create_procurement", {
            "inventory_alerts": reorder_needed,
            "forecasts": forecasts,
            "sku_ids": scope,
        })
        decisions = procurement_result.get("decisions", [])
        self._print(f"  Purchase orders created: {len(decisions)}")

        # --- STEP 4: Negotiation ---
        self._step(4, "PRICE NEGOTIATION")
        negotiations = {}
        for decision in decisions:
            po_number = decision.get("po_number")
            if not po_number:
                continue
            result = await self._dispatch("negotiate_po", {
                "po_number": po_number,
                "supplier_id": decision.get("selected_supplier_id"),
                "sku_id": decision.get("sku_id"),
                "quantity": decision.get("order_quantity", 0),
                "target_price": decision.get("target_price", 0.0),
            })
            negotiations[po_number] = result
            self._print(f"    {po_number}: {result.get('outcome', 'unknown')} | "
                        f"discount={float(result.get('discount_achieved_pct', 0) or 0):.1f}%")
        if not decisions:
            self._print("  No purchase orders to negotiate.")

        # --- STEP 5: Logistics Assignment ---
        self._step(5, "LOGISTICS ASSIGNMENT")
        shippable = []
        for decision in decisions:
            po_number = decision.get("po_number")
            negotiation = negotiations.get(po_number, {})
            if negotiation.get("outcome") == "walk_away":
                continue
            shippable.append({
                "po_number": po_number,
                "supplier_id": decision.get("selected_supplier_id"),
                "sku_id": decision.get("sku_id"),
                "quantity": decision.get("order_quantity"),
                "urgency": decision.get("urgency", "normal"),
            })

        logistics_result = await self._dispatch("assign_logistics", {"purchase_orders": shippable})
        assignments = logistics_result.get("assignments", [])
        self._print(f"  Routes assigned: {len(assignments)} | "
                    f"shipping cost: ${float(logistics_result.get('total_shipping_cost', 0) or 0):,.2f}")

        # --- STEP 6: Supplier Scoring ---
        self._step(6, "SUPPLIER PERFORMANCE SCORING")
        po_outcomes = [{
            "po_number": po_number,
            "supplier_id": n.get("supplier_id"),
            "outcome": n.get("outcome"),
            "discount_pct": n.get("discount_achieved_pct", 0),
        } for po_number, n in negotiations.items()]
        scoring_result = await self._dispatch("score_suppliers", {
            "supplier_ids": self.supplier_ids,
            "po_outcomes": po_outcomes,
        })
        scores = scoring_result.get("scores", [])
        self._print(f"  Suppliers scored: {len(scores)} | "
                    f"network avg: {float(scoring_result.get('avg_network_score', 0) or 0):.2f}")

        # --- STEP 7: Risk Assessment ---
        self._step(7, "RISK & RESILIENCE ASSESSMENT")
        open_pos = self.sqlite.get_open_purchase_orders()
        risk_result = await self._dispatch("assess_risk", {
            "active_pos": open_pos[:20],
            "supplier_scores": scores,
        })
        risks = risk_result.get("risks", [])
        self._print(f"  Risk level: {risk_result.get('overall_risk_level', 'unknown')} | "
                    f"{len(risks)} risks identified")

        summary = self._build_summary(
            cycle_id, cycle_start, inventory_result, forecasts, decisions,
            negotiations, logistics_result, scoring_result, risk_result
        )

        self.redis.set_hash(RedisMemory.cycle_state_key(),
                            {"status": "completed", "cycle_id": cycle_id, "summary": summary})
        self.sqlite.save_cycle_run(
            cycle_id=cycle_id,
            started_at=summary["started_at"],
            completed_at=summary["completed_at"],
            duration_seconds=summary["duration_seconds"],
            mode=summary["mode"],
            pos_created=len(decisions),
            risks_found=len(risks),
            summary=summary,
        )

        self._banner("CYCLE COMPLETE")
        self._print(json.dumps(summary, indent=2))
        self._print("=" * 60)
        return summary

    def _build_summary(self, cycle_id, cycle_start, inventory_result, forecasts,
                       decisions, negotiations, logistics_result, scoring_result,
                       risk_result) -> dict:
        cycle_end = datetime.utcnow()
        negotiation_results = list(negotiations.values())
        deals = [n for n in negotiation_results if n.get("outcome") == "deal_accepted"]
        walked = [n for n in negotiation_results if n.get("outcome") == "walk_away"]
        scores = scoring_result.get("scores", [])
        risks = risk_result.get("risks", [])
        status_counts = self.sqlite.get_po_status_counts()

        return {
            "cycle_id": cycle_id,
            "mode": "offline" if self.offline else "gemini",
            "started_at": cycle_start.isoformat(),
            "completed_at": cycle_end.isoformat(),
            "duration_seconds": round((cycle_end - cycle_start).total_seconds(), 1),
            "steps_completed": 7,
            "inventory": {
                "status": inventory_result.get("inventory_status", "unknown"),
                "alerts": len(inventory_result.get("alerts", [])),
                "skus_monitored": inventory_result.get("total_skus_monitored", len(self.sku_ids)),
                "stock_value_usd": inventory_result.get("total_inventory_value_usd", 0),
            },
            "forecasting": {"skus_forecasted": len(forecasts)},
            "procurement": {
                "pos_created": len(decisions),
                "total_value_usd": round(
                    sum(float(d.get("total_estimated_value", 0) or 0) for d in decisions), 2),
            },
            "negotiation": {
                "deals_completed": len(deals),
                "deals_walked": len(walked),
                "avg_discount_pct": round(
                    sum(float(n.get("discount_achieved_pct", 0) or 0) for n in negotiation_results)
                    / max(len(negotiation_results), 1), 2),
                "savings_vs_list_usd": round(
                    sum(float(n.get("savings_vs_list", 0) or 0) for n in deals), 2),
            },
            "logistics": {
                "routes_assigned": len(logistics_result.get("assignments", [])),
                "shipping_cost_usd": logistics_result.get("total_shipping_cost", 0.0),
                "avg_transit_days": logistics_result.get("avg_transit_days", 0.0),
            },
            "supplier_performance": {
                "suppliers_scored": len(scores),
                "preferred": len([s for s in scores if s.get("tier") == "preferred"]),
                "at_risk": len([s for s in scores if s.get("tier") == "at_risk"]),
                "avg_network_score": scoring_result.get("avg_network_score", 0.0),
            },
            "risk": {
                "total_risks": len(risks),
                "critical": len([r for r in risks if r.get("severity", 0) >= 4]),
                "high": len([r for r in risks if r.get("severity", 0) == 3]),
                "overall_level": risk_result.get("overall_risk_level", "unknown"),
                "risk_score": risk_result.get("risk_score", 0.0),
            },
            "database": {
                "po_status_counts": status_counts,
                "total_pos": sum(s["count"] for s in status_counts.values()),
                "total_value_usd": round(
                    sum(s["value"] for status, s in status_counts.items()
                        if status != "cancelled"), 2),
                "supplier_scores_saved": len(self.sqlite.get_all_supplier_scores()),
            },
            "messages_on_bus": len(self.bus.get_history()),
        }
