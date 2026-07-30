from datetime import datetime
from typing import Optional

import google.ai.generativelanguage as glm
from agents.base_agent import BaseAgent
from tools.vendor_tools import (get_supplier_info, get_alternative_supplier,
                                get_qualified_suppliers)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory
from config import (GEMINI_PRO_MODEL, COUNTRY_RISK_SCORES, DEFAULT_COUNTRY_RISK,
                    SUPPLIER_MIN_RELIABILITY, REGION_CONCENTRATION_THRESHOLD,
                    LEAD_TIME_RISK_DAYS)

# Country risk at or above these levels becomes a high / medium severity finding.
GEO_RISK_HIGH = 0.18
GEO_RISK_MEDIUM = 0.12
# Severity totals mapped onto the 0-10 headline risk score.
RISK_SCORE_PER_SEVERITY_POINT = 0.5
RISK_LEVEL_BANDS = (("critical", 7.5), ("high", 5.0), ("medium", 2.5), ("low", 0.0))

SYSTEM_PROMPT = """You are a Risk and Resilience Agent for an industrial supply chain company.
Your role is to identify, assess, and mitigate supply chain risks including:
- Supplier concentration risk (too dependent on one supplier)
- Geographic/geopolitical risk (high-risk regions)
- Lead time risk (long lead times in volatile categories)
- Single-source risk (no backup supplier for critical SKUs)
- Active disruption events

Risk severity levels: 1=low, 2=medium, 3=high, 4=critical

For each risk, provide a concrete mitigation recommendation.
Return structured JSON with complete risk assessment."""

TOOL_DECLARATIONS = [
    glm.FunctionDeclaration(
        name="get_supplier_info",
        description="Get supplier details for risk assessment",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"supplier_id": glm.Schema(type=glm.Type.STRING)},
            required=["supplier_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_alternative_supplier",
        description="Find an alternative supplier for a SKU if the primary supplier fails",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={
                "sku_id": glm.Schema(type=glm.Type.STRING),
                "exclude_supplier_id": glm.Schema(type=glm.Type.STRING),
            },
            required=["sku_id", "exclude_supplier_id"]
        )
    ),
    glm.FunctionDeclaration(
        name="get_qualified_suppliers",
        description="List every supplier able to supply a SKU — used to detect single-source risk",
        parameters=glm.Schema(
            type=glm.Type.OBJECT,
            properties={"sku_id": glm.Schema(type=glm.Type.STRING)},
            required=["sku_id"]
        )
    ),
]

COUNTRY_RISK_SCORES_DESC = {k: f"{v:.0%} risk" for k, v in COUNTRY_RISK_SCORES.items()}

PROMPT_TEMPLATE = """Perform a comprehensive supply chain risk assessment.

Active purchase orders: {active_pos}
Current supplier scores: {supplier_scores}
Country risk levels: {country_risk}

Analysis tasks:
1. For each unique supplier in the active POs, get_supplier_info and check:
   - Country risk using the levels above (unlisted countries count as {default_risk:.0%})
   - Reliability score below {min_reliability} = at-risk
   - Lead time above {lead_time_days} days = vulnerability
2. Identify at-risk suppliers and find alternatives using get_alternative_supplier
3. Check for geographic concentration (more than {concentration:.0%} of POs from one region)
4. Use get_qualified_suppliers to find SKUs with only one possible supplier (single-source)

Return JSON:
{{
  "overall_risk_level": "low|medium|high|critical",
  "risk_score": <0.0-10.0>,
  "risks": [
    {{
      "risk_id": "R-001",
      "type": "supplier_reliability|geographic|single_source|lead_time|concentration",
      "severity": <1-4>,
      "description": "...",
      "affected_skus": ["SKU-XXX"],
      "affected_supplier": "SUP-XXX",
      "region": "EMEA|APAC|AMER|GLOBAL",
      "mitigation": "specific action to take",
      "alternative_supplier": null
    }}
  ],
  "critical_actions": ["..."],
  "resilience_recommendations": ["..."]
}}"""


class RiskAgent(BaseAgent):
    def __init__(self, redis_mem: RedisMemory, sqlite_mem: SQLiteMemory,
                 offline: Optional[bool] = None):
        super().__init__(
            name="risk",
            model=GEMINI_PRO_MODEL,
            system_prompt=SYSTEM_PROMPT,
            tools={
                "get_supplier_info": get_supplier_info,
                "get_alternative_supplier": get_alternative_supplier,
                "get_qualified_suppliers": get_qualified_suppliers,
            },
            tool_declarations=TOOL_DECLARATIONS,
            redis_mem=redis_mem,
            sqlite_mem=sqlite_mem,
            offline=offline,
        )

    # ── deterministic engine ─────────────────────────────────
    @staticmethod
    def _risk_level(score: float) -> str:
        for level, threshold in RISK_LEVEL_BANDS:
            if score >= threshold:
                return level
        return "low"

    def _offline_result(self, task: dict) -> dict:
        active_pos = [p for p in task.get("active_pos", []) if isinstance(p, dict)]
        tier_by_supplier = {s.get("supplier_id"): s.get("tier")
                            for s in task.get("supplier_scores", []) if isinstance(s, dict)}

        skus_by_supplier: dict[str, list] = {}
        for po in active_pos:
            supplier_id = po.get("supplier_id")
            if not supplier_id:
                continue
            skus = skus_by_supplier.setdefault(supplier_id, [])
            if po.get("sku_id") and po["sku_id"] not in skus:
                skus.append(po["sku_id"])

        risks = []
        region_counts: dict[str, int] = {}

        def add_risk(risk_type, severity, description, affected_skus, supplier_id,
                     region, mitigation, alternative=None):
            risks.append({
                "risk_id": f"R-{len(risks) + 1:03d}",
                "type": risk_type,
                "severity": severity,
                "description": description,
                "affected_skus": affected_skus,
                "affected_supplier": supplier_id,
                "region": region,
                "mitigation": mitigation,
                "alternative_supplier": alternative,
            })

        for supplier_id, skus in skus_by_supplier.items():
            supplier = get_supplier_info(supplier_id)
            if "error" in supplier:
                continue
            region = supplier["region"]
            region_counts[region] = region_counts.get(region, 0) + len(
                [p for p in active_pos if p.get("supplier_id") == supplier_id])

            alternative = get_alternative_supplier(skus[0], supplier_id) if skus else {"error": "n/a"}
            alt_id = alternative.get("supplier_id") if "error" not in alternative else None

            reliability = supplier["reliability_score"]
            if reliability < SUPPLIER_MIN_RELIABILITY or tier_by_supplier.get(supplier_id) == "at_risk":
                add_risk(
                    "supplier_reliability", 3,
                    f"{supplier['name']} reliability {reliability:.2f} is below the "
                    f"{SUPPLIER_MIN_RELIABILITY:.2f} threshold",
                    skus, supplier_id, region,
                    f"Dual-source these SKUs and shift volume to {alt_id or 'an alternative vendor'}",
                    alt_id,
                )

            country_risk = COUNTRY_RISK_SCORES.get(supplier["country"], DEFAULT_COUNTRY_RISK)
            if country_risk >= GEO_RISK_MEDIUM:
                severity = 3 if country_risk >= GEO_RISK_HIGH else 2
                add_risk(
                    "geographic", severity,
                    f"{supplier['country']} carries {country_risk:.0%} country risk "
                    f"affecting {len(skus)} SKU(s)",
                    skus, supplier_id, region,
                    "Hold additional safety stock and qualify a supplier in a lower-risk country",
                    alt_id,
                )

            if supplier["lead_time_days"] > LEAD_TIME_RISK_DAYS:
                add_risk(
                    "lead_time", 2,
                    f"{supplier['name']} lead time is {supplier['lead_time_days']} days "
                    f"(above {LEAD_TIME_RISK_DAYS})",
                    skus, supplier_id, region,
                    "Increase reorder point to cover the extra lead time or use air freight for urgent lines",
                    alt_id,
                )

        for sku_id in {p.get("sku_id") for p in active_pos if p.get("sku_id")}:
            qualified = get_qualified_suppliers(sku_id)
            if qualified["count"] <= 1:
                only = qualified["qualified_suppliers"][0]["supplier_id"] if qualified["count"] else None
                add_risk(
                    "single_source", 4,
                    f"{sku_id} has only {qualified['count']} qualified supplier",
                    [sku_id], only, "GLOBAL",
                    "Qualify a second source for this SKU before the next cycle",
                )

        total_pos = sum(region_counts.values())
        for region, count in region_counts.items():
            share = count / total_pos if total_pos else 0
            if share > REGION_CONCENTRATION_THRESHOLD:
                add_risk(
                    "concentration", 3,
                    f"{share:.0%} of open purchase orders are sourced from {region}",
                    sorted({p["sku_id"] for p in active_pos if p.get("sku_id")}),
                    None, region,
                    f"Rebalance volume away from {region} towards a second region",
                )

        risk_score = min(10.0, round(sum(r["severity"] for r in risks)
                                     * RISK_SCORE_PER_SEVERITY_POINT, 1))
        critical_actions = [r["mitigation"] for r in risks if r["severity"] >= 4]
        recommendations = sorted({r["mitigation"] for r in risks if r["severity"] == 3})

        return {
            "overall_risk_level": self._risk_level(risk_score),
            "risk_score": risk_score,
            "risks": risks,
            "critical_actions": critical_actions,
            "resilience_recommendations": recommendations,
        }

    # ── cycle step ───────────────────────────────────────────
    def _persist(self, risks: list) -> int:
        logged = 0
        for r in risks:
            try:
                severity = int(r.get("severity", 0))
            except (TypeError, ValueError):
                continue
            if severity < 3:
                continue
            self.sqlite.log_disruption(
                event_type=r.get("type", "unknown"),
                region=r.get("region") or "GLOBAL",
                severity=severity,
                affected_skus=r.get("affected_skus", []),
                description=r.get("description", ""),
            )
            logged += 1
        return logged

    def run(self, task: dict) -> dict:
        active_pos = task.get("active_pos", [])
        supplier_scores = task.get("supplier_scores", [])
        self._log(f"Running risk assessment over {len(active_pos)} active POs...")
        self.save_state({"status": "running", "started_at": datetime.utcnow().isoformat()})

        prompt = PROMPT_TEMPLATE.format(
            active_pos=active_pos,
            supplier_scores=supplier_scores,
            country_risk=COUNTRY_RISK_SCORES_DESC,
            default_risk=DEFAULT_COUNTRY_RISK,
            min_reliability=SUPPLIER_MIN_RELIABILITY,
            lead_time_days=LEAD_TIME_RISK_DAYS,
            concentration=REGION_CONCENTRATION_THRESHOLD,
        )
        parsed, raw = self._reason(prompt, task)

        risks = parsed.get("risks", [])
        logged = self._persist(risks)
        level = parsed.get("overall_risk_level", "unknown")
        critical = [r for r in risks if r.get("severity", 0) >= 3]
        self._log(f"Risk level: {str(level).upper()} | {len(risks)} risks | "
                  f"{len(critical)} high/critical | {logged} logged as disruptions")

        self.save_state({"status": "completed", "risk_count": len(risks)})
        return {
            "agent": self.name,
            "risks": risks,
            "overall_risk_level": level,
            "risk_score": parsed.get("risk_score", 0.0),
            "critical_actions": parsed.get("critical_actions", []),
            "resilience_recommendations": parsed.get("resilience_recommendations", []),
            "disruptions_logged": logged,
            "raw_response": raw,
        }
