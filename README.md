# Supply Chain Autonomous Intelligence Network

A multi-agent system that runs procurement, logistics, forecasting, negotiation, and risk
management for an industrial supply chain. Seven agents reason with Google Gemini and act
through a shared tool layer; every agent also ships a deterministic engine, so the whole
network runs end to end with no API key at all.

```bash
git clone https://github.com/SdSarthak/Supply-Chain-Autonomous-Agents
cd Supply-Chain-Autonomous-Agents
pip install -r requirements.txt
python main.py --offline      # full 7-step cycle, no API key, no Redis needed
```

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
   │2.5-Pro │ │2.5-Pro │ │2.5-Pro │ │Flash │ │2.5-Pro  │ │Flash │ │2.5Pro│
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
   │  (in-memory fallback) │          │  · disruption_events · cycle_runs    │
   └───────────────────────┘          └──────────────────────────────────────┘
```

Every step of a cycle is published as a `Message` on the priority bus, pulled off it, routed
to an agent by task type, executed in a worker thread, and answered with a correlated result
message. `bus.get_history()` is therefore a complete audit trail of the run.

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

## Two execution modes

Each agent implements the same decision problem twice, and both paths write identical
structures to Redis and SQLite:

| Mode | How decisions are made | Needs |
|---|---|---|
| **Gemini** (default) | The agent plans, calls its tools through Gemini function calling, and returns JSON | `GEMINI_API_KEY` |
| **Offline** (`--offline`) | The agent's deterministic engine computes the answer from the same tools | nothing |

Offline mode exists so the system is demonstrable, testable and debuggable without spending
tokens. It is also the safety net: if Gemini returns a response with no parsable JSON, the
agent falls back to its deterministic engine for that step rather than dropping the step.

With no `GEMINI_API_KEY` set, `main.py` selects offline mode automatically and says so.

---

## Quick Start

### Option A — Local, zero setup

```bash
pip install -r requirements.txt
python main.py --offline
```

No API key and no Redis required: agent state falls back to an in-process store that
implements the same Redis semantics, and SQLite is created on first run.

### Option B — Local with Gemini

```bash
# Prerequisites: Python 3.10+, Redis on localhost:6379
pip install -r requirements.txt
cp .env.example .env          # then set GEMINI_API_KEY
python main.py
```

### Option C — Docker

```bash
cp .env.example .env          # set GEMINI_API_KEY, or leave it empty for offline mode
docker compose up --build
```

The app waits for Redis to be healthy before starting. SQLite is persisted to a named
Docker volume (`db_data`).

---

## CLI

```
python main.py [--offline] [--report] [--check] [--skus SKU ...]
               [--allow-memory-fallback] [--json] [--log-level LEVEL]
```

| Flag | Effect |
|---|---|
| `--offline` | Run every agent on its deterministic engine (no Gemini calls) |
| `--report` | Print the current database state — cycles, POs, scores, forecasts, disruptions — and exit |
| `--check` | Verify config, Redis and SQLite connectivity, then exit |
| `--skus SKU-001 SKU-002` | Restrict the cycle to specific SKUs |
| `--allow-memory-fallback` | Keep running with in-process state when Redis is unreachable |
| `--json` | Print only the cycle summary as JSON (suitable for piping) |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

```bash
python main.py --offline --skus SKU-002 SKU-005 --json | jq .procurement
python main.py --report
```

---

## Configuration

Everything is environment-driven — copy `.env.example` to `.env` and edit. The file
documents every variable; the ones that change behaviour most:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Required unless running offline |
| `GEMINI_PRO_MODEL` / `GEMINI_FLASH_MODEL` | `gemini-2.5-pro` / `gemini-2.0-flash` | Strategic vs operational agents |
| `OFFLINE_MODE` | `false` | Force deterministic engines |
| `MAX_TOOL_ROUNDS` | `8` | Tool-calling rounds per agent turn |
| `GEMINI_MAX_RETRIES` | `3` | Retries on 429/5xx with exponential backoff |
| `PROCUREMENT_TARGET_FILL` | `0.80` | Reorder up to this share of `max_stock` |
| `SUPPLIER_MIN_RELIABILITY` | `0.85` | Minimum reliability to qualify a supplier |
| `NEGOTIATION_MAX_ROUNDS` | `5` | Rounds before taking the best offer on the table |
| `NEGOTIATION_TARGET_DISCOUNT` | `0.08` | Discount the negotiator aims for |
| `LEAD_TIME_RISK_DAYS` | `20` | Lead times above this are flagged as a risk |

---

## Decision logic

The policies below are what the deterministic engines implement and what the Gemini prompts
instruct — the two modes are held to the same rules.

**Procurement.** Order quantity is `max(80% of max_stock − available, 30-day forecast −
available)`, capped at `max_stock − available` so an order still fits in the warehouses, and
raised to the supplier's `min_order_qty`. Suppliers are ranked on reliability (0.35),
on-time delivery (0.25), price against the market benchmark (0.25) and lead time (0.15),
with a bonus for clearing both preferred thresholds.

**Negotiation.** Opens 12% below the supplier's quote, then concedes a quarter of the
remaining gap each round for up to five rounds. The supplier holds a floor a little below
their own quote but never under 88% of list. A deal is taken when the discount clears 5% or
when the price already beats the market average — a deep volume tier can be the best price
available even when the supplier will not move. Every round is written to `negotiation_log`,
and an accepted deal re-prices the PO.

**Supplier scoring.** `overall = delivery×0.40 + quality×0.35 + price×0.25`, where delivery
is the on-time rate minus 0.05 per late past delivery, quality is `1 − rejection_rate` minus
0.05 per failed delivery, and price is the average of `market_avg / their price` across the
SKUs they carry, minus 0.05 per negotiation they walked away from.

```
Tiers:  preferred >= 0.90   approved 0.80-0.89   conditional 0.70-0.79   at_risk < 0.70
```

**Risk.** Flags supplier reliability below threshold, country risk (>=18% high, >=12%
medium), lead times over 20 days, single-source SKUs (severity 4) and regional concentration
above 50% of open POs. The headline score is `min(10, Σ severity × 0.5)`; anything severity 3
or above is written to `disruption_events`.

---

## Project Structure

```
.
├── Dockerfile · docker-compose.yml · .env.example · requirements.txt
├── config.py               # models, memory, policy thresholds — all env-overridable
├── main.py                 # CLI: run a cycle, report, or check prerequisites
│
├── agents/
│   ├── base_agent.py       # Gemini chat + function calling, retries, JSON extraction,
│   │                       # history sanitising, offline dispatch
│   ├── demand_forecasting_agent.py
│   ├── procurement_agent.py
│   ├── negotiation_agent.py
│   ├── logistics_agent.py
│   ├── inventory_agent.py
│   ├── supplier_performance_agent.py
│   └── risk_agent.py
│
├── orchestrator/
│   ├── orchestrator.py     # async 7-step cycle over the bus, summary aggregation
│   ├── message_bus.py      # asyncio priority queue + message history
│   └── task_router.py      # task type → agent, request/result correlation
│
├── memory/
│   ├── redis_memory.py     # JSON wrapper over Redis + in-process fallback store
│   └── sqlite_memory.py    # six tables, CRUD and reporting queries
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
    └── test_stress.py      # 98-test suite across 10 suites
```

---

## Mock Data

All external systems are simulated with realistic JSON files generated by
`data/generate_mock_data.py`.

| File | Records | Description |
|---|---|---|
| `demand_history.json` | 7,300 | Daily units sold per SKU with seasonality, trend, regional split |
| `inventory.json` | 30 | Stock per SKU per warehouse (WH-NORTH / WH-SOUTH / WH-EAST) |
| `suppliers.json` | 8 | Vendors with 3-tier pricing, lead times, reliability, delivery history |
| `logistics_routes.json` | 12 | Carrier routes (air/sea/road) across EMEA, APAC, AMER |

`reorder_point` and `max_stock` are network-wide figures, so stock is generated as a network
total and split across the three warehouses. Three SKUs are deliberately seeded below their
reorder point and one above 90% of max stock, so every cycle has real work to do.

```bash
python data/generate_mock_data.py   # regenerate (seeded, reproducible)
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

If Redis is unreachable and fallback is allowed, `InMemoryStore` takes over. It implements
the same commands with the same semantics — including TTL expiry, `LTRIM` windows and
`WRONGTYPE` errors on mismatched key types — so behaviour does not silently diverge.

### SQLite (persistent)

| Table | Purpose |
|---|---|
| `demand_forecasts` | 30/60/90-day predictions per SKU |
| `purchase_orders` | Full PO lifecycle (pending → negotiated → in_transit / cancelled) |
| `supplier_scores` | Daily delivery/quality/price scores per vendor |
| `negotiation_log` | Round-by-round negotiation history |
| `disruption_events` | Risk events flagged by the Risk Agent |
| `cycle_runs` | One row per cycle, with the full summary JSON |

---

## Gemini Function Calling

Each agent exposes its domain tools as `glm.FunctionDeclaration` objects. The base agent loop:

```
User prompt → Gemini → function_call response
    → Python tool executes → function_response fed back
    → Gemini → function_call (if more tools needed)
    → ... (up to MAX_TOOL_ROUNDS)
    → Gemini → text response (final answer)
```

Tool results are fed back into the same `chat.send_message()` session, so Gemini sees all
prior tool outputs when reasoning about the next step. The loop also:

- retries 429/5xx responses with exponential backoff, and fails fast on auth errors;
- asks once for a final answer if the tool budget runs out mid-plan;
- parses JSON by brace matching, so fenced blocks and trailing prose are handled;
- sanitises replayed chat history (drops empty, orphaned or dangling turns) before sending;
- never lets a tool exception escape — errors are returned to the model as `{"error": ...}`.

---

## Running Tests

```bash
python tests/test_stress.py
```

98 tests across 10 suites — no Gemini API key required, and no test mutates tracked data.
Without a Redis server 90 run and the Redis suite auto-skips:

| Suite | Covers |
|---|---|
| Config | Model names, risk scores, data paths, score weights, env parsing |
| Data Integrity | Record counts, field validity, pricing tiers, seasonality |
| Tool Functions | Every tool, edge cases, error paths, tiered pricing, route selection |
| SQLite Memory | Full CRUD, upsert, PO lifecycle, 500-insert throughput |
| In-Memory Store | TTL, sliding window, hash ops, type enforcement, 8-thread concurrency |
| Redis Memory | Same against a real server — auto-skips if Redis is not running |
| Orchestrator | Message bus, priority ordering, timeout, task routing |
| Agent Plumbing | JSON extraction, retry classification, history sanitising, tool dispatch |
| Agent Engines | All seven decision engines and their SQLite side effects |
| Full Cycle | End-to-end offline cycle, bus audit trail, persistence, repeat runs |

---

## License

MIT
