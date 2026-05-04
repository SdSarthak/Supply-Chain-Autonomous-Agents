import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_PRO_MODEL = "gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini-2.0-flash"

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

SQLITE_PATH = os.getenv("SQLITE_PATH", "supply_chain.db")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

DEMAND_HISTORY_FILE = os.path.join(DATA_DIR, "demand_history.json")
INVENTORY_FILE = os.path.join(DATA_DIR, "inventory.json")
SUPPLIERS_FILE = os.path.join(DATA_DIR, "suppliers.json")
LOGISTICS_ROUTES_FILE = os.path.join(DATA_DIR, "logistics_routes.json")

AGENT_CHAT_HISTORY_MAX = 20
INVENTORY_CACHE_TTL = 60
NEGOTIATION_SESSION_TTL = 3600

COUNTRY_RISK_SCORES = {
    "Germany": 0.05, "USA": 0.05, "Poland": 0.08,
    "Taiwan": 0.15, "South Korea": 0.12, "Italy": 0.10,
    "Mexico": 0.18, "India": 0.20,
}
