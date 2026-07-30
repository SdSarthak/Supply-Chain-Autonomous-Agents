import uuid
from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.vendor_tools import (get_supplier_offer, get_market_price_benchmark,
                                 simulate_supplier_counter_offer,
                                 SUPPLIER_PRICE_FLOOR_PCT)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import (GEMINI_PRO_MODEL, NEGOTIATION_SESSION_TTL, NEGOTIATION_MAX_ROUNDS,
                    NEGOTIATION_TARGET_DISCOUNT, NEGOTIATION_OPENING_DISCOUNT,
                    NEGOTIATION_WALKAWAY_DISCOUNT)

# How far below their own opening quote a supplier will move before the
# 88%-of-list cost line takes over as the binding constraint.
SUPPLIER_ROOM_BELOW_QUOTE = 0.06

SYSTEM_PROMPT = f"""You are a Negotiation Agent for an industrial supply chain company.
Your role is to negotiate the best possible price and terms with suppliers for purchase orders.

Negotiation strategy:
- Target at least {NEGOTIATION_TARGET_DISCOUNT:.0%} discount from the supplier's initial offer
- Never accept the first offer — always counter-negotiate
- Use market benchmarks to justify your position
- Maximum {NEGOTIATION_MAX_ROUNDS} negotiation rounds per deal
- Walk away if the supplier will not go below {NEGOTIATION_WALKAWAY_DISCOUNT:.0%} off
  their opening offer (procurement will find alternatives)
- Be professional but firm. Use data to support your position.

Return structured JSON at each negotiation step."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_supplier_offer",
        description="Get the supplier's current price offer for a SKU and quantity",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "supplier_id": glm.Schema(type=glm.Type.STRING),
                "sku_id": glm.Schema(type=glm.Type.STRING),
                "quantity": glm.Schema(type=glm.Type.INTEGER, description="Order quantity"),
            },
            required=["supplier_id", "sku_id", "quantity"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_market_price_benchmark",
        description="Get market price range for a SKU across all suppliers",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="simulate_supplier_counter_offer",
        description="Send our price proposal to the supplier and get their response",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "current_offer": glm.Schema(type=glm.Type.NUMBER, description="Our current offer price per unit"),
                "round_num": glm.Schema(type=glm.Type.INTEGER, description="Current negotiation round number"),
                "list_price": glm.Schema(type=glm.Type.NUMBER, description="Supplier's original list price"),
            },
            required=["current_offer", "round_num", "list_price"]
        )
    ),
]

PROMPT_TEMPLATE = """Negotiate a purchase order with the following details:
- PO Number: {po_number}
- Supplier ID: {supplier_id}
- SKU ID: {sku_id}
- Quantity: {quantity} units
- Our target price: ${target_price:.2f}/unit (aim for at most this price)

Steps:
1. Get the supplier's initial offer for this quantity
2. Get market benchmark prices to understand fair value
3. Conduct negotiation rounds (max {max_rounds}):
   - Open at {opening_discount:.0%} below their initial offer
   - Each round, call simulate_supplier_counter_offer with your price to get their response
   - Concede gradually towards their counter, never above your target
4. Accept once the price is at or below target, or at the final round take their best offer.
   Walk away only if the best achievable discount is under {walkaway_discount:.0%} AND the
   price is still above the market average — a deep volume tier can already be the best
   price available even when the supplier will not move further.

Record every round you ran. Return final JSON:
{{
  "session_id": "{session_id}",
  "po_number": "{po_number}",
  "supplier_id": "{supplier_id}",
  "sku_id": "{sku_id}",
  "quantity": {quantity},
  "initial_list_price": <number>,
  "initial_offer_price": <number>,
  "final_agreed_price": <number>,
  "discount_achieved_pct": <number>,
  "rounds_taken": <number>,
  "rounds": [{{"round": 1, "our_offer": <number>, "their_offer": <number>}}],
  "outcome": "deal_accepted|walk_away",
  "total_value": <number>,
  "savings_vs_list": <number>
}}"""


class NegotiationAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
        super().__init__(
            name="negotiation",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_supplier_offer": get_supplier_offer,
                "get_market_price_benchmark": get_market_price_benchmark,
                "simulate_supplier_counter_offer": simulate_supplier_counter_offer,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
            offline=offline,
        )

    # ── deterministic engine ─────────────────────────────────
    def _offline_result(self, task: dict) -> dict:
        supplier_id = task.get("supplier_id")
        sku_id = task.get("sku_id")
        quantity = int(task.get("quantity", 0) or 0)
        target_price = float(task.get("target_price", 0.0) or 0.0)
        session_id = task.get("session_id", uuid.uuid4().hex[:12])

        offer = get_supplier_offer(supplier_id, sku_id, quantity)
        if "error" in offer:
            return {
                "session_id": session_id,
                "po_number": task.get("po_number"),
                "supplier_id": supplier_id,
                "sku_id": sku_id,
                "quantity": quantity,
                "outcome": "walk_away",
                "rounds": [],
                "rounds_taken": 0,
                "error": offer["error"],
            }

        list_price = float(offer["list_price"])
        opening_offer = float(offer["offered_price"])
        benchmark = get_market_price_benchmark(sku_id)
        market_avg = float(benchmark.get("market_avg", opening_offer))

        our_offer = opening_offer * (1 - NEGOTIATION_OPENING_DISCOUNT)
        target = min(target_price or opening_offer, opening_offer * (1 - NEGOTIATION_TARGET_DISCOUNT))
        # Their walk-away point: a little below the quote they opened with, but
        # never under their cost line at 88% of list. On a bulk order the tier
        # price is already close to that line, so there is less room to move.
        floor_price = min(opening_offer * (1 - SUPPLIER_ROOM_BELOW_QUOTE),
                          list_price * SUPPLIER_PRICE_FLOOR_PCT)

        rounds = []
        agreed_price = None
        for round_num in range(1, NEGOTIATION_MAX_ROUNDS + 1):
            counter = simulate_supplier_counter_offer(
                current_offer=round(our_offer, 2), round_num=round_num,
                list_price=opening_offer, floor_price=round(floor_price, 2)
            )
            their_offer = float(counter["counter_price"])
            rounds.append({
                "round": round_num,
                "our_offer": round(our_offer, 2),
                "their_offer": round(their_offer, 2),
            })

            if counter["accepted"]:
                agreed_price = round(our_offer, 2)
                break
            if their_offer <= target:
                agreed_price = round(their_offer, 2)
                break
            # Concede a quarter of the remaining gap each round.
            our_offer = our_offer + (their_offer - our_offer) * 0.25

        if agreed_price is None and rounds:
            agreed_price = rounds[-1]["their_offer"]

        discount_pct = ((opening_offer - agreed_price) / opening_offer * 100) if opening_offer else 0.0
        # Take the deal when we moved them far enough, or when the price already
        # beats the market average — a deep volume tier leaves little room to
        # negotiate but is still the best price available.
        beats_market = agreed_price is not None and agreed_price <= market_avg
        outcome = ("deal_accepted"
                   if discount_pct >= NEGOTIATION_WALKAWAY_DISCOUNT * 100 or beats_market
                   else "walk_away")

        return {
            "session_id": session_id,
            "po_number": task.get("po_number"),
            "supplier_id": supplier_id,
            "sku_id": sku_id,
            "quantity": quantity,
            "initial_list_price": list_price,
            "initial_offer_price": opening_offer,
            "final_agreed_price": agreed_price,
            "discount_achieved_pct": round(discount_pct, 2),
            "rounds_taken": len(rounds),
            "rounds": rounds,
            "outcome": outcome,
            "total_value": round(agreed_price * quantity, 2),
            "savings_vs_list": round((list_price - agreed_price) * quantity, 2),
        }

    # ── cycle step ───────────────────────────────────────────
    def _persist(self, session_id: str, parsed: dict, task: dict) -> None:
        supplier_id = parsed.get("supplier_id") or task.get("supplier_id") or "UNKNOWN"
        sku_id = parsed.get("sku_id") or task.get("sku_id") or "UNKNOWN"
        rounds = parsed.get("rounds") or []
        total_rounds = len(rounds)

        for entry in rounds:
            try:
                round_num = int(entry.get("round", 0))
                our_offer = float(entry.get("our_offer", 0.0))
                their_offer = float(entry.get("their_offer", 0.0))
            except (TypeError, ValueError, AttributeError):
                continue
            self.sqlite.log_negotiation(
                session_id=session_id, supplier_id=supplier_id, sku_id=sku_id,
                round_num=round_num, our_offer=round(our_offer, 2),
                their_offer=round(their_offer, 2),
                status="completed" if round_num == total_rounds else "ongoing",
            )

        po_number = parsed.get("po_number") or task.get("po_number")
        final_price = parsed.get("final_agreed_price")
        if parsed.get("outcome") == "deal_accepted" and po_number and final_price:
            self.sqlite.update_po_price(po_number, round(float(final_price), 2))
            self.sqlite.update_po_status(po_number, "negotiated")
            self._log(f"Deal accepted at ${float(final_price):.2f}/unit "
                      f"({float(parsed.get('discount_achieved_pct', 0)):.1f}% off opening offer)")
        elif po_number:
            self.sqlite.update_po_status(po_number, "cancelled")
            self._log(f"Walked away from {po_number} — no acceptable price")

    def run(self, task: dict) -> dict:
        po_number = task.get("po_number", "UNKNOWN")
        supplier_id = task.get("supplier_id")
        sku_id = task.get("sku_id")
        quantity = int(task.get("quantity", 0) or 0)
        target_price = float(task.get("target_price", 0.0) or 0.0)

        self._log(f"Negotiating {po_number}: {sku_id} x{quantity} with {supplier_id}")

        session_id = uuid.uuid4().hex[:12]
        task = dict(task, session_id=session_id)
        self.redis.set_hash(
            RedisMemory.negotiation_key(session_id),
            {"po_number": po_number, "supplier_id": supplier_id,
             "sku_id": sku_id, "quantity": quantity, "status": "in_progress"},
            ttl=NEGOTIATION_SESSION_TTL
        )
        self.save_state({"status": "negotiating", "session_id": session_id})

        prompt = PROMPT_TEMPLATE.format(
            po_number=po_number, supplier_id=supplier_id, sku_id=sku_id,
            quantity=quantity, target_price=target_price, session_id=session_id,
            max_rounds=NEGOTIATION_MAX_ROUNDS,
            opening_discount=NEGOTIATION_OPENING_DISCOUNT,
            walkaway_discount=NEGOTIATION_WALKAWAY_DISCOUNT,
        )
        parsed, raw = self._reason(prompt, task)
        self._persist(session_id, parsed, task)

        result = {"agent": self.name, "session_id": session_id,
                  "po_number": po_number, "raw_response": raw}
        result.update(parsed)
        result["session_id"] = session_id
        result["po_number"] = po_number

        self.redis.hset(RedisMemory.negotiation_key(session_id), "status", "completed")
        self.save_state({"status": "completed", "session_id": session_id})
        return result
