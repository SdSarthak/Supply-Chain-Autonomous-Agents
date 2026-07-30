from orchestrator.message_bus import Message, DEFAULT_PRIORITY

ORCHESTRATOR = "orchestrator"

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
    def __init__(self, routes: dict = None):
        self._routes = dict(routes) if routes else AGENT_TASK_MAP.copy()

    def route(self, task_type: str) -> str:
        agent = self._routes.get(task_type)
        if not agent:
            raise ValueError(f"No agent registered for task type: {task_type}")
        return agent

    def register(self, task_type: str, agent_name: str) -> None:
        """Add or override a route — used when extending the network."""
        self._routes[task_type] = agent_name

    def task_types(self) -> list:
        return sorted(self._routes)

    def build_message(self, task_type: str, payload: dict,
                       from_agent: str = ORCHESTRATOR,
                       priority: int = DEFAULT_PRIORITY) -> Message:
        return Message(
            from_agent=from_agent,
            to_agent=self.route(task_type),
            msg_type=task_type,
            payload=payload,
            priority=priority
        )

    @staticmethod
    def build_result(request: Message, result: dict) -> Message:
        """Wrap an agent's output as a reply that keeps the request's correlation id."""
        return Message(
            from_agent=request.to_agent,
            to_agent=request.from_agent,
            msg_type=f"{request.type}_result",
            payload=result,
            priority=request.priority,
            correlation_id=request.correlation_id,
        )
