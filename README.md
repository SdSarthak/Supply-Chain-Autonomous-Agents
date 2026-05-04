# Supply Chain Autonomous Intelligence Network

A fully autonomous multi-agent system that runs procurement, logistics, forecasting, negotiation, and risk management for an industrial supply chain — powered entirely by Google Gemini.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR                             │
│          Async Python · Priority Message Bus · Task Router      │
└───────┬──────────┬─────────┬──────────┬────────┬───────────────┘
        │          │         │          │        │
   ┌────▼───┐ ┌───▼────┐ ┌──▼─────┐ ┌──▼───┐ ┌──▼──────┐ ┌──────┐ ┌──────┐
   │Demand  │ │Procure-│ │Negotia-│ │Logis-│ │Supplier │ │Inven-│ │Risk  │
   │Forecast│ │ment    │ │tion    │ │tics  │ │Performa-│ │tory  │ │Agent │
   │Agent   │ │Agent   │ │Agent   │ │Agent │ │nce Agent│ │Agent │ │      │
   │2.5-Pro │ │2.5-Pro │ │2.5-Pro │ │Flash │ │2.5-Pro  │ │Flash │ │2-Pro │
   └────────┘ └────────┘ └────────┘ └──────┘ └─────────┘ └──────┘ └──────┘
        │          │         │          │        │            │        │
   ┌────▼──────────▼─────────▼──────────▼────────▼────────────▼────────▼────┐
   │                    TOOL LAYER  (pure Python functions)                  │
   │   inventory_tools · vendor_tools · logistics_tools                      │
   └─────────────────────────────────────────────────────────────────────────┘
        │                                              │
   ┌────▼──────────────────┐          ┌───────────────▼──────────────────────┐
   │  Redis 7              │          │  SQLite                              │
   │  · Agent session state│          │  · demand_forecasts                  │
   │  · Chat history       │          │  · purchase_orders                   │
   │  · Live inventory TTL │          │  · supplier_scores                   │
   │  · Negotiation context│          │  · negotiation_log                   │
   └───────────────────────┘          │  · disruption_events                 │
                                      └──────────────────────────────────────┘
```

### Agents

| Agent | Model | Responsibility |
|---|---|---|
| **Demand Forecasting** | gemini-2.5-pro | 30/60/90-day SKU-level demand forecasts with seasonality |
| **Procurement** | gemini-2.5-pro | PO decisions from forecasts + inventory gaps |
| **Negotiation** | gemini-2.5-pro | Multi-round autonomous price negotiation with suppliers |
| **Logistics** | gemini-2.0-flash | Route optimization, carrier assignment, ETA estimation |
| **Inventory** | gemini-2.0-flash | Stock monitoring, reorder alerts, IoT sensor integration |
| **Supplier Performance** | gemini-2.5-pro | Vendor scoring, tier classification, relationship history |
| **Risk & Resilience** | gemini-2.5-pro | Disruption detection, geographic risk, mitigation planning |

### Full Procurement Cycle (one `run_cycle()` call)

```
Inventory Check → Demand Forecast → Procurement Decisions
→ Price Negotiation (per PO) → Logistics Assignment
→ Supplier Scoring → Risk Assessment → Summary Report
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| LLM | Google Gemini 2.5 Pro + 2.0 Flash |
| Orchestration | Custom async Python (no framework) |
| Fast memory | Redis 7 |
| Persistent memory | SQLite |
| Containerisation | Docker + Docker Compose |
| Language | Python 3.11 |

---

## Project Structure

```
.
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── config.py               # model names, Redis/SQLite config, country risk scores
├── main.py                 # entry point — prerequisite check + run_cycle()
│
├── agents/
│   ├── base_agent.py       # Gemini multi-turn chat, function calling dispatch, memory I/O
│   ├── demand_forecasting_agent.py
│   ├── procurement_agent.py
│   ├── negotiation_agent.py
│   ├── logistics_agent.py
│   ├── inventory_agent.py
│   ├── supplier_performance_agent.py
│   └── risk_agent.py
│
├── orchestrator/
│   ├── orchestrator.py     # 7-step cycle runner, result aggregation
│   ├── message_bus.py      # asyncio priority queue
│   └── task_router.py      # task type → agent mapping
│
├── memory/
│   ├── redis_memory.py     # set/get/list/hash with key builders
│   └── sqlite_memory.py    # all persistent tables + query helpers
│
├── tools/                  # pure Python functions registered as Gemini tools
│   ├── inventory_tools.py
│   ├── vendor_tools.py
│   └── logistics_tools.py
│
├── data/
│   ├── generate_mock_data.py
│   ├── demand_history.json    # 7,300 records — 10 SKUs × 730 days
│   ├── inventory.json         # 30 records — 10 SKUs × 3 warehouses
│   ├── suppliers.json         # 8 suppliers with tiered pricing
│   └── logistics_routes.json  # 12 routes across EMEA / APAC / AMER
│
└── tests/
    └── test_stress.py      # 45-test suite (data, tools, SQLite, Redis, orchestrator)
```

---

## Quick Start

### Option A — Docker (recommended)

```bash
# 1. Clone and configure
cp .env.example .env
#    Open .env and set GEMINI_API_KEY=your_key_here

# 2. Run — Redis + app start automatically
docker compose up --build
```

The app waits for Redis to be healthy before starting. SQLite is persisted to a named Docker volume (`db_data`).

### Option B — Local

```bash
# Prerequisites: Python 3.11+, Redis running on localhost:6379

# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
#    Set GEMINI_API_KEY in .env

# 3. Run
python main.py
```

---

## Configuration

All config lives in `.env` (copy from `.env.example`):

```env
GEMINI_API_KEY=your_gemini_api_key_here
REDIS_HOST=localhost        # use "redis" when running via Docker Compose
REDIS_PORT=6379
REDIS_DB=0
SQLITE_PATH=supply_chain.db # use /db/supply_chain.db in Docker
```

Runtime constants in [config.py](config.py):

| Constant | Value | Purpose |
|---|---|---|
| `GEMINI_PRO_MODEL` | `gemini-2.5-pro` | Strategic agents |
| `GEMINI_FLASH_MODEL` | `gemini-2.0-flash` | Operational agents |
| `AGENT_CHAT_HISTORY_MAX` | `20` | Sliding window per agent in Redis |
| `INVENTORY_CACHE_TTL` | `60s` | Live inventory Redis TTL |
| `NEGOTIATION_SESSION_TTL` | `3600s` | Negotiation context Redis TTL |

---

## Mock Data

All external systems are simulated with realistic JSON files generated by `data/generate_mock_data.py`.

| File | Records | Description |
|---|---|---|
| `demand_history.json` | 7,300 | Daily units sold per SKU with seasonality, trend, regional split |
| `inventory.json` | 30 | Stock levels per SKU per warehouse (WH-NORTH / WH-SOUTH / WH-EAST) |
| `suppliers.json` | 8 | Vendors with 3-tier pricing, lead times, reliability scores, delivery history |
| `logistics_routes.json` | 12 | Carrier routes (air/sea/road) across EMEA, APAC, AMER |

Regenerate at any time:

```bash
python data/generate_mock_data.py
```

---

## Memory Architecture

### Redis (fast / ephemeral)

| Key pattern | Type | TTL | Stores |
|---|---|---|---|
| `agent:{name}:state` | JSON | — | Current agent task state |
| `agent:{name}:chat_history` | List | — | Last 20 Gemini turns |
| `inventory:live:{sku_id}` | JSON | 60s | Live stock snapshot |
| `negotiation:{session_id}` | Hash | 1h | Active negotiation context |
| `orchestrator:cycle_state` | Hash | — | Step-by-step cycle progress |

### SQLite (persistent)

| Table | Purpose |
|---|---|
| `demand_forecasts` | 30/60/90-day predictions per SKU |
| `purchase_orders` | Full PO lifecycle (pending → negotiated → in_transit) |
| `supplier_scores` | Daily delivery/quality/price scores per vendor |
| `negotiation_log` | Round-by-round negotiation history |
| `disruption_events` | Risk events flagged by the Risk Agent |

---

## Gemini Function Calling

Each agent exposes domain tools as `glm.FunctionDeclaration` objects. The base agent loop:

```
User prompt → Gemini → function_call response
    → Python tool executes → function_response fed back
    → Gemini → function_call (if more tools needed)
    → ... (up to 8 rounds)
    → Gemini → text response (final answer)
```

Tool results are fed back into the same `chat.send_message()` session, so Gemini sees all prior tool outputs when reasoning about the next step.

---

## Running Tests

```bash
python tests/test_stress.py
```

45 tests across 6 suites — no Gemini API key required:

| Suite | Tests | Covers |
|---|---|---|
| Config | 3 | Model names, risk scores, data file paths |
| Data Integrity | 5 | Record counts, field validity, pricing tiers, seasonality |
| Tool Functions | 22 | All tools, edge cases, error paths, tiered pricing logic |
| SQLite Memory | 8 | Full CRUD, upsert, lifecycle, 500-insert throughput |
| Redis Memory | 8 | TTL, sliding window, hash ops, concurrent writes, 1k-op throughput |
| Orchestrator | 7 | Message bus, priority ordering, timeout, task routing |

Redis tests auto-skip if Redis is not running.

---

## Supplier Scoring Model

The Supplier Performance Agent scores vendors on three dimensions:

```
overall = delivery × 0.40 + quality × 0.35 + price × 0.25

Tiers:
  preferred    overall >= 0.90
  approved     0.80 – 0.89
  conditional  0.70 – 0.79
  at_risk      < 0.70
```

---

## License

MIT
