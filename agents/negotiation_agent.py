import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.vendor_tools import (get_supplier_offer, get_market_price_benchmark,
                                 simulate_supplier_counter_offer)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import GEMINI_PRO_MODEL, NEGOTIATION_SESSION_TTL
from datetime import datetime
import uuid

SYSTEM_PROMPT = """You are a Negotiation Agent for an industrial supply chain company.
Your role is to negotiate the best possible price and terms with suppliers for purchase orders.

Negotiation strategy:
- Target at least 8% discount from the supplier's initial offer
- Never accept the first offer — always counter-negotiate
- Use market benchmarks to justify your position
- Maximum 5 negotiation rounds per deal
- Walk away if supplier won't go below 5% from list price (find alternatives)
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
        description="Simulate the supplier's counter-offer response to our price proposal",
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


class NegotiationAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory):
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
        )

    def run(self, task: dict) -> dict:
        po_number = task.get("po_number", "UNKNOWN")
        supplier_id = task.get("supplier_id")
        sku_id = task.get("sku_id")
        quantity = task.get("quantity", 0)
        target_price = task.get("target_price", 0.0)

        self._log(f"Negotiating {po_number}: {sku_id} x{quantity} with {supplier_id}")

        session_id = str(uuid.uuid4())[:12]
        self.redis.set_hash(
            RedisMemory.negotiation_key(session_id),
            {"po_number": po_number, "supplier_id": supplier_id,
             "sku_id": sku_id, "quantity": quantity, "status": "in_progress"},
            ttl=NEGOTIATION_SESSION_TTL
        )
        self.save_state({"status": "negotiating", "session_id": session_id})

        prompt = f"""Negotiate a purchase order with the following details:
- PO Number: {po_number}
- Supplier ID: {supplier_id}
- SKU ID: {sku_id}
- Quantity: {quantity} units
- Our target price: ${target_price:.2f}/unit (aim for at most this price)

Steps:
1. Get the supplier's initial offer for this quantity
2. Get market benchmark prices to understand fair value
3. Conduct negotiation rounds (max 5):
   - Start by offering 12% below their initial offer
   - Each round, use simulate_supplier_counter_offer to get their counter
   - Gradually concede but stay within your target
4. Accept when price is at or below target, or at round 5 take the best offer

Return final JSON:
{{
  "session_id": "{session_id}",
  "po_number": "{po_number}",
  "supplier_id": "{supplier_id}",
  "sku_id": "{sku_id}",
  "quantity": {quantity},
  "initial_list_price": <number>,
  "final_agreed_price": <number>,
  "discount_achieved_pct": <number>,
  "rounds_taken": <number>,
  "outcome": "deal_accepted|walk_away",
  "total_value": <number>,
  "savings_vs_list": <number>
}}"""

        response = self._call_gemini(prompt)
        self._log(f"Negotiation complete for {po_number}")

        result = {"agent": self.name, "session_id": session_id, "po_number": po_number, "raw_response": response}
        try:
            import re, json
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                result.update(parsed)

                rounds = parsed.get("rounds_taken", 1)
                list_price = parsed.get("initial_list_price", target_price)
                final_price = parsed.get("final_agreed_price", target_price)
                for r in range(1, int(rounds) + 1):
                    our_offer = list_price * (1 - 0.12 - (r - 1) * 0.02)
                    self.sqlite.log_negotiation(
                        session_id=session_id, supplier_id=supplier_id, sku_id=sku_id,
                        round_num=r, our_offer=round(our_offer, 2),
                        their_offer=round(float(final_price) + (rounds - r) * 0.5, 2),
                        status="completed" if r == rounds else "ongoing"
                    )

                if parsed.get("outcome") == "deal_accepted":
                    self.sqlite.update_po_status(po_number, "negotiated")
                    self._log(f"Deal accepted at ${final_price:.2f}/unit ({parsed.get('discount_achieved_pct', 0):.1f}% discount)")

        except Exception as e:
            self._log(f"Warning: Could not parse negotiation JSON: {e}")

        self.redis.hset(RedisMemory.negotiation_key(session_id), "status", "completed")
        self.save_state({"status": "completed", "session_id": session_id})
        return result
