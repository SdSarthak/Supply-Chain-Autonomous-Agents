import argparse
import json
import logging
import os
import sys

# Ensure project root is on the path regardless of how main.py is invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (GEMINI_API_KEY, GEMINI_PRO_MODEL, GEMINI_FLASH_MODEL, LOG_LEVEL,
                    OFFLINE_MODE, REDIS_HOST, REDIS_PORT, SQLITE_PATH)
from memory.redis_memory import RedisMemory
from memory.sqlite_memory import SQLiteMemory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Supply Chain Autonomous Intelligence Network — "
                    "runs a full procurement cycle across seven agents.",
    )
    parser.add_argument("--offline", action="store_true",
                        help="run the deterministic engines instead of Gemini "
                             "(no API key needed, falls back to in-memory state "
                             "if Redis is unavailable)")
    parser.add_argument("--report", action="store_true",
                        help="print the current state of the database and exit")
    parser.add_argument("--check", action="store_true",
                        help="verify configuration and connectivity, then exit")
    parser.add_argument("--skus", nargs="+", metavar="SKU",
                        help="limit the cycle to these SKU ids (default: all)")
    parser.add_argument("--allow-memory-fallback", action="store_true",
                        help="continue with in-memory state when Redis is unreachable")
    parser.add_argument("--json", action="store_true", dest="json_only",
                        help="print only the cycle summary as JSON")
    parser.add_argument("--log-level", default=LOG_LEVEL,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help=f"logging verbosity (default: {LOG_LEVEL})")
    return parser


def check_prerequisites(offline: bool, allow_memory_fallback: bool,
                        implicit_offline: bool = False) -> bool:
    """Report on configuration and connectivity. Returns False if unusable."""
    ok = True

    if offline:
        print("  - Mode: offline (deterministic engines, Gemini not used)")
        if implicit_offline:
            print("    (no GEMINI_API_KEY found — set one in .env to run the agents on Gemini)")
    elif GEMINI_API_KEY:
        print(f"  - Gemini API key configured ({GEMINI_PRO_MODEL} / {GEMINI_FLASH_MODEL})")
    else:
        print("  x GEMINI_API_KEY is not set. Copy .env.example to .env and add your key,")
        print("    or run in deterministic mode with: python main.py --offline")
        ok = False

    redis_mem = RedisMemory()
    if redis_mem.ping():
        print(f"  - Redis connected at {REDIS_HOST}:{REDIS_PORT}")
    elif allow_memory_fallback:
        print(f"  - Redis unreachable at {REDIS_HOST}:{REDIS_PORT} — using in-memory state")
    else:
        print(f"  x Redis is not reachable at {REDIS_HOST}:{REDIS_PORT}")
        print("    Start Redis:  docker run -d -p 6379:6379 redis")
        print("    Or continue without it: python main.py --allow-memory-fallback")
        ok = False

    try:
        SQLiteMemory()
        print(f"  - SQLite ready at {SQLITE_PATH}")
    except Exception as e:
        print(f"  x SQLite unavailable at {SQLITE_PATH}: {e}")
        ok = False

    return ok


def print_report() -> None:
    """Summarise what previous cycles have written to the database."""
    db = SQLiteMemory()
    print("\n" + "=" * 60)
    print("  SUPPLY CHAIN STATE REPORT")
    print("=" * 60)

    cycles = db.get_cycle_runs(limit=5)
    print(f"\nRecent cycles ({len(cycles)}):")
    if not cycles:
        print("  none recorded yet — run `python main.py --offline` to create one")
    for c in cycles:
        print(f"  {c['cycle_id']}  {c['mode']:<8} {c['duration_seconds']:>6.1f}s  "
              f"{c['pos_created']} POs  {c['risks_found']} risks")

    status_counts = db.get_po_status_counts()
    total = sum(s["count"] for s in status_counts.values())
    print(f"\nPurchase orders ({total}):")
    for status, stats in sorted(status_counts.items()):
        print(f"  {status:<12} {stats['count']:>4}   ${stats['value']:>14,.2f}")

    scores = db.get_all_supplier_scores()
    print(f"\nSupplier scores ({len(scores)}):")
    for s in scores:
        print(f"  {s['supplier_id']}  overall {s['overall']:.3f}  "
              f"(delivery {s['delivery_score']:.2f} / quality {s['quality_score']:.2f} "
              f"/ price {s['price_score']:.2f})")

    forecasts = db.get_latest_forecasts(30)
    print(f"\nLatest 30-day forecasts ({len(forecasts)}):")
    for f in forecasts:
        print(f"  {f['sku_id']}  {f['predicted_units']:>10,.0f} units  "
              f"confidence {f['confidence']:.2f}")

    disruptions = db.get_active_disruptions()
    print(f"\nOpen disruption events ({len(disruptions)}):")
    for d in disruptions[:10]:
        print(f"  [sev {d['severity']}] {d['event_type']:<22} {d['region']:<7} "
              f"{', '.join(d['affected_skus'][:4])}")
    print("=" * 60)


def main(argv: list = None) -> dict:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.report:
        print_report()
        return {}

    offline = args.offline or OFFLINE_MODE or not GEMINI_API_KEY
    implicit_offline = offline and not (args.offline or OFFLINE_MODE)
    allow_fallback = args.allow_memory_fallback or offline

    if not args.json_only:
        print("\nSupply Chain Autonomous Intelligence Network")
        print("Checking prerequisites...")

    if not check_prerequisites(offline, allow_fallback, implicit_offline):
        sys.exit(1)
    if args.check:
        return {}

    from orchestrator.orchestrator import Orchestrator
    orchestrator = Orchestrator(offline=offline, allow_memory_fallback=allow_fallback,
                                quiet=args.json_only)
    summary = orchestrator.run_cycle(sku_ids=args.skus)

    if args.json_only:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\nCycle completed in {summary['duration_seconds']}s")
        print(f"Database: {SQLITE_PATH}")
    return summary


if __name__ == "__main__":
    main()
