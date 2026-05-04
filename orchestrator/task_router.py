from orchestrator.message_bus import Message


AGENT_TASK_MAP = {
    "forecast_demand": "demand_forecasting",
    "check_inventory": "inventory",
    "create_procurement": "procurement",
    "negotiate_po": "negotiation",
    "assign_logistics": "logistics",
    "score_suppliers": "supplier_performance",
    "assess_risk": "risk",
}


class TaskRouter:
    def __init__(self):
        self._routes = AGENT_TASK_MAP.copy()

    def route(self, task_type: str) -> str:
        agent = self._routes.get(task_type)
        if not agent:
            raise ValueError(f"No agent registered for task type: {task_type}")
        return agent

    def build_message(self, task_type: str, payload: dict,
                       from_agent: str = "orchestrator",
                       priority: int = 3) -> Message:
        to_agent = self.route(task_type)
        return Message(
            from_agent=from_agent,
            to_agent=to_agent,
            msg_type=task_type,
            payload=payload,
            priority=priority
        )
