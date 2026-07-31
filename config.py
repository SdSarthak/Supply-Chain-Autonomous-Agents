import logging
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _clamp(name: str, value, minimum, maximum):
    """Hold a setting inside the range the code can actually honour.

    An out-of-range override is a configuration mistake, not a licence to
    misbehave: `NEGOTIATION_MAX_ROUNDS=0` used to make the negotiation engine
    crash on a `None` price and `AGENT_CHAT_HISTORY_MAX=0` turned the LTRIM
    window into "keep everything", growing agent history without bound.
    """
    if minimum is not None and value < minimum:
        logger.warning("%s=%s is below the minimum %s — using %s", name, value, minimum, minimum)
        return minimum
    if maximum is not None and value > maximum:
        logger.warning("%s=%s is above the maximum %s — using %s", name, value, maximum, maximum)
        return maximum
    return value


def _env_int(name: str, default: int, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer — using %s", name, raw, default)
        value = default
    return _clamp(name, value, minimum, maximum)


def _env_float(name: str, default: float, minimum: Optional[float] = None,
               maximum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number — using %s", name, raw, default)
        value = default
    return _clamp(name, value, minimum, maximum)


# ── Gemini ───────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_PRO_MODEL = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
GEMINI_FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.0-flash")

# Maximum tool-calling rounds inside a single agent turn before the agent is
# asked to produce a final answer with whatever it has gathered.
MAX_TOOL_ROUNDS = _env_int("MAX_TOOL_ROUNDS", 8, minimum=1, maximum=50)
# Retries for transient Gemini errors (429 / 5xx), exponential backoff.
GEMINI_MAX_RETRIES = _env_int("GEMINI_MAX_RETRIES", 3, minimum=0, maximum=10)
GEMINI_RETRY_BASE_DELAY = _env_float("GEMINI_RETRY_BASE_DELAY", 2.0, minimum=0.0, maximum=60.0)

# When true, agents skip Gemini entirely and use their deterministic
# heuristic engines. Lets the full 7-step cycle run with no API key.
OFFLINE_MODE = _env_bool("OFFLINE_MODE", False)

LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()

# ── Memory ───────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = _env_int("REDIS_PORT", 6379, minimum=1, maximum=65535)
REDIS_DB = _env_int("REDIS_DB", 0, minimum=0)
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

SQLITE_PATH = os.getenv("SQLITE_PATH", "supply_chain.db")

# ── Data files ───────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

DEMAND_HISTORY_FILE = os.path.join(DATA_DIR, "demand_history.json")
INVENTORY_FILE = os.path.join(DATA_DIR, "inventory.json")
SUPPLIERS_FILE = os.path.join(DATA_DIR, "suppliers.json")
LOGISTICS_ROUTES_FILE = os.path.join(DATA_DIR, "logistics_routes.json")

# ── Agent memory tuning ──────────────────────────────────────
AGENT_CHAT_HISTORY_MAX = _env_int("AGENT_CHAT_HISTORY_MAX", 20, minimum=2, maximum=500)
INVENTORY_CACHE_TTL = _env_int("INVENTORY_CACHE_TTL", 60, minimum=1)
NEGOTIATION_SESSION_TTL = _env_int("NEGOTIATION_SESSION_TTL", 3600, minimum=1)

# ── Procurement policy ───────────────────────────────────────
# Reorder up to this fraction of max_stock.
PROCUREMENT_TARGET_FILL = _env_float("PROCUREMENT_TARGET_FILL", 0.80, minimum=0.0, maximum=1.0)
# Minimum reliability a supplier must clear to be considered at all.
SUPPLIER_MIN_RELIABILITY = _env_float("SUPPLIER_MIN_RELIABILITY", 0.85, minimum=0.0, maximum=1.0)
# Preferred thresholds — suppliers clearing both get a selection bonus.
SUPPLIER_PREFERRED_RELIABILITY = _env_float("SUPPLIER_PREFERRED_RELIABILITY", 0.88, minimum=0.0, maximum=1.0)
SUPPLIER_PREFERRED_OTD = _env_float("SUPPLIER_PREFERRED_OTD", 0.90, minimum=0.0, maximum=1.0)
# Days of cover below which a SKU is treated as urgent / critical.
URGENT_DAYS_OF_STOCK = _env_int("URGENT_DAYS_OF_STOCK", 21, minimum=1)
CRITICAL_DAYS_OF_STOCK = _env_int("CRITICAL_DAYS_OF_STOCK", 10, minimum=1)

# ── Negotiation policy ───────────────────────────────────────
NEGOTIATION_MAX_ROUNDS = _env_int("NEGOTIATION_MAX_ROUNDS", 5, minimum=1, maximum=50)
NEGOTIATION_TARGET_DISCOUNT = _env_float("NEGOTIATION_TARGET_DISCOUNT", 0.08, minimum=0.0, maximum=1.0)
NEGOTIATION_OPENING_DISCOUNT = _env_float("NEGOTIATION_OPENING_DISCOUNT", 0.12, minimum=0.0, maximum=1.0)
NEGOTIATION_WALKAWAY_DISCOUNT = _env_float("NEGOTIATION_WALKAWAY_DISCOUNT", 0.05, minimum=0.0, maximum=1.0)

# ── Supplier scoring model ───────────────────────────────────
SCORE_WEIGHT_DELIVERY = 0.40
SCORE_WEIGHT_QUALITY = 0.35
SCORE_WEIGHT_PRICE = 0.25

SUPPLIER_TIERS = (
    ("preferred", 0.90),
    ("approved", 0.80),
    ("conditional", 0.70),
    ("at_risk", 0.0),
)


def classify_supplier_tier(overall: float) -> str:
    """Map an overall supplier score onto its relationship tier."""
    for tier, threshold in SUPPLIER_TIERS:
        if overall >= threshold:
            return tier
    return "at_risk"


# ── Risk model ───────────────────────────────────────────────
COUNTRY_RISK_SCORES = {
    "Germany": 0.05, "USA": 0.05, "Poland": 0.08,
    "Taiwan": 0.15, "South Korea": 0.12, "Italy": 0.10,
    "Mexico": 0.18, "India": 0.20,
}
DEFAULT_COUNTRY_RISK = _env_float("DEFAULT_COUNTRY_RISK", 0.25, minimum=0.0, maximum=1.0)
# A single region holding more than this share of open POs is a concentration risk.
REGION_CONCENTRATION_THRESHOLD = _env_float("REGION_CONCENTRATION_THRESHOLD", 0.50, minimum=0.0, maximum=1.0)
# Lead times above this many days are flagged as a vulnerability.
LEAD_TIME_RISK_DAYS = _env_int("LEAD_TIME_RISK_DAYS", 20, minimum=1)
