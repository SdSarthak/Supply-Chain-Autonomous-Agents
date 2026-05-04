import sys
import os
import logging

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from config import GEMINI_API_KEY, REDIS_URL, SQLITE_PATH


def check_prerequisites():
    errors = []
    if not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
    if errors:
        print("\nConfiguration errors:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    # Test Redis connection
    try:
        from memory.redis_memory import RedisMemory
        rm = RedisMemory()
        if not rm.ping():
            print("\n  ✗ Redis is not reachable at localhost:6379")
            print("    Start Redis: docker run -d -p 6379:6379 redis")
            print("    Or install locally: https://redis.io/download")
            sys.exit(1)
        print("  ✓ Redis connected")
    except Exception as e:
        print(f"\n  ✗ Redis connection failed: {e}")
        print("    Start Redis: docker run -d -p 6379:6379 redis")
        sys.exit(1)

    print("  ✓ Gemini API key configured")
    print(f"  ✓ SQLite database: {SQLITE_PATH}")


def main():
    print("\nSupply Chain Autonomous Intelligence Network")
    print("Checking prerequisites...")
    check_prerequisites()

    from orchestrator.orchestrator import Orchestrator
    orchestrator = Orchestrator()
    summary = orchestrator.run_cycle()

    print(f"\nCycle completed in {summary['duration_seconds']}s")
    print(f"Database: {SQLITE_PATH}")
    return summary


if __name__ == "__main__":
    main()
