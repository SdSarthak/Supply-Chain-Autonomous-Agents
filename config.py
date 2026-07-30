import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ── Gemini ───────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_PRO_MODEL = os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
GEMINI_FLASH_MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.0-flash")

# Maximum tool-calling rounds inside a single agent turn before the agent is
# asked to produce a final answer with whatever it has gathered.
MAX_TOOL_ROUNDS = _env_int("MAX_TOOL_ROUNDS", 8)
# Retries for transient Gemini errors (429 / 5xx), exponential backoff.
GEMINI_MAX_RETRIES = _env_int("GEMINI_MAX_RETRIES", 3)
GEMINI_RETRY_BASE_DELAY = _env_float("GEMINI_RETRY_BASE_DELAY", 2.0)

# When true, agents skip Gemini entirely and use their deterministic
# heuristic engines. Lets the full 7-step cycle run with no API key.
OFFLINE_MODE = _env_bool("OFFLINE_MODE", False)

LOG_LEVEL = os.getenv("LOG_LEVEL", "WARNING").upper()

# ── Memory ───────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = _env_int("REDIS_PORT", 6379)
REDIS_DB = _env_int("REDIS_DB", 0)
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

SQLITE_PATH = os.getenv("SQLITE_PATH", "supply_chain.db")

# ── Data files ───────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

DEMAND_HISTORY_FILE = os.path.join(DATA_DIR, "demand_history.json")
INVENTORY_FILE = os.path.join(DATA_DIR, "inventory.json")
SUPPLIERS_FILE = os.path.join(DATA_DIR, "suppliers.json")
LOGISTICS_ROUTES_FILE = os.path.join(DATA_DIR, "logistics_routes.json")

# ── Agent memory tuning ──────────────────────────────────────
AGENT_CHAT_HISTORY_MAX = _env_int("AGENT_CHAT_HISTORY_MAX", 20)
INVENTORY_CACHE_TTL = _env_int("INVENTORY_CACHE_TTL", 60)
NEGOTIATION_SESSION_TTL = _env_int("NEGOTIATION_SESSION_TTL", 3600)

# ── Procurement policy ───────────────────────────────────────
# Reorder up to this fraction of max_stock.
PROCUREMENT_TARGET_FILL = _env_float("PROCUREMENT_TARGET_FILL", 0.80)
# Minimum reliability a supplier must clear to be considered at all.
SUPPLIER_MIN_RELIABILITY = _env_float("SUPPLIER_MIN_RELIABILITY", 0.85)
# Preferred thresholds — suppliers clearing both get a selection bonus.
SUPPLIER_PREFERRED_RELIABILITY = _env_float("SUPPLIER_PREFERRED_RELIABILITY", 0.88)
SUPPLIER_PREFERRED_OTD = _env_float("SUPPLIER_PREFERRED_OTD", 0.90)
# Days of cover below which a SKU is treated as urgent / critical.
URGENT_DAYS_OF_STOCK = _env_int("URGENT_DAYS_OF_STOCK", 21)
CRITICAL_DAYS_OF_STOCK = _env_int("CRITICAL_DAYS_OF_STOCK", 10)

# ── Negotiation policy ───────────────────────────────────────
NEGOTIATION_MAX_ROUNDS = _env_int("NEGOTIATION_MAX_ROUNDS", 5)
NEGOTIATION_TARGET_DISCOUNT = _env_float("NEGOTIATION_TARGET_DISCOUNT", 0.08)
NEGOTIATION_OPENING_DISCOUNT = _env_float("NEGOTIATION_OPENING_DISCOUNT", 0.12)
NEGOTIATION_WALKAWAY_DISCOUNT = _env_float("NEGOTIATION_WALKAWAY_DISCOUNT", 0.05)

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
DEFAULT_COUNTRY_RISK = _env_float("DEFAULT_COUNTRY_RISK", 0.25)
# A single region holding more than this share of open POs is a concentration risk.
REGION_CONCENTRATION_THRESHOLD = _env_float("REGION_CONCENTRATION_THRESHOLD", 0.50)
# Lead times above this many days are flagged as a vulnerability.
LEAD_TIME_RISK_DAYS = _env_int("LEAD_TIME_RISK_DAYS", 20)
