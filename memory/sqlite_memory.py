import os
import sqlite3
from contextlib import contextmanager
import json
from datetime import datetime
from typing import Optional
from config import (SQLITE_PATH, SCORE_WEIGHT_DELIVERY, SCORE_WEIGHT_QUALITY,
                    SCORE_WEIGHT_PRICE)

OPEN_PO_STATUSES = ("pending", "negotiated", "in_transit")


class SQLiteMemory:
    def __init__(self, db_path: str = SQLITE_PATH):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        """Open a connection, commit on success, and always close it.

        sqlite3's own `with conn:` block manages the transaction but leaves the
        connection open — under a long cycle that leaks handles, so closing is
        done explicitly here.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS demand_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku_id TEXT NOT NULL,
                    forecast_date TEXT NOT NULL,
                    predicted_units REAL NOT NULL,
                    confidence REAL NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS supplier_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    delivery_score REAL NOT NULL,
                    quality_score REAL NOT NULL,
                    price_score REAL NOT NULL,
                    overall REAL NOT NULL,
                    UNIQUE(supplier_id, date)
                );

                CREATE TABLE IF NOT EXISTS purchase_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    po_number TEXT UNIQUE NOT NULL,
                    supplier_id TEXT NOT NULL,
                    sku_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    unit_price REAL NOT NULL,
                    total_value REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    route_id TEXT,
                    created_at TEXT NOT NULL,
                    expected_delivery TEXT
                );

                CREATE TABLE IF NOT EXISTS negotiation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    supplier_id TEXT NOT NULL,
                    sku_id TEXT NOT NULL,
                    round_num INTEGER NOT NULL,
                    our_offer REAL NOT NULL,
                    their_offer REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ongoing',
                    timestamp TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS disruption_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    region TEXT NOT NULL,
                    severity INTEGER NOT NULL,
                    affected_skus TEXT NOT NULL,
                    description TEXT,
                    detected_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS cycle_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT UNIQUE NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    mode TEXT NOT NULL,
                    pos_created INTEGER NOT NULL DEFAULT 0,
                    risks_found INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_demand_sku ON demand_forecasts(sku_id);
                CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status);
                CREATE INDEX IF NOT EXISTS idx_po_supplier ON purchase_orders(supplier_id);
                CREATE INDEX IF NOT EXISTS idx_neg_session ON negotiation_log(session_id);
                CREATE INDEX IF NOT EXISTS idx_disruption_open ON disruption_events(resolved_at);
            """)

    def insert(self, table: str, data: dict) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._connect() as conn:
            cur = conn.execute(sql, list(data.values()))
            return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run a statement and return how many rows it actually touched."""
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    def save_forecast(self, sku_id: str, forecast_date: str, predicted_units: float,
                      confidence: float, horizon_days: int) -> int:
        return self.insert("demand_forecasts", {
            "sku_id": sku_id,
            "forecast_date": forecast_date,
            "predicted_units": predicted_units,
            "confidence": confidence,
            "horizon_days": horizon_days,
            "created_at": datetime.utcnow().isoformat()
        })

    def get_forecasts(self, sku_id: str, horizon_days: int = 30) -> list[dict]:
        return self.query(
            "SELECT * FROM demand_forecasts WHERE sku_id=? AND horizon_days=? ORDER BY created_at DESC LIMIT 1",
            (sku_id, horizon_days)
        )

    def upsert_supplier_score(self, supplier_id: str, delivery: float,
                               quality: float, price: float) -> None:
        overall = round(
            delivery * SCORE_WEIGHT_DELIVERY
            + quality * SCORE_WEIGHT_QUALITY
            + price * SCORE_WEIGHT_PRICE,
            4
        )
        today = datetime.utcnow().date().isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO supplier_scores (supplier_id, date, delivery_score, quality_score, price_score, overall)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(supplier_id, date) DO UPDATE SET
                    delivery_score=excluded.delivery_score,
                    quality_score=excluded.quality_score,
                    price_score=excluded.price_score,
                    overall=excluded.overall
            """, (supplier_id, today, delivery, quality, price, overall))

    def get_supplier_score(self, supplier_id: str) -> Optional[dict]:
        rows = self.query(
            "SELECT * FROM supplier_scores WHERE supplier_id=? ORDER BY date DESC LIMIT 1",
            (supplier_id,)
        )
        return rows[0] if rows else None

    def create_purchase_order(self, po_number: str, supplier_id: str, sku_id: str,
                               quantity: int, unit_price: float) -> int:
        return self.insert("purchase_orders", {
            "po_number": po_number,
            "supplier_id": supplier_id,
            "sku_id": sku_id,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_value": round(quantity * unit_price, 2),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        })

    def update_po_status(self, po_number: str, status: str, route_id: str = None,
                          expected_delivery: str = None) -> int:
        """Move a PO to a new status. Returns the number of rows updated.

        Zero means the PO number does not exist — worth reporting, because on
        the Gemini path the number came out of a model response.
        """
        assignments = ["status=?"]
        params: list = [status]
        if route_id:
            assignments.append("route_id=?")
            params.append(route_id)
        if expected_delivery:
            assignments.append("expected_delivery=?")
            params.append(expected_delivery)
        params.append(po_number)
        return self.execute(
            f"UPDATE purchase_orders SET {', '.join(assignments)} WHERE po_number=?",
            tuple(params)
        )

    def update_po_price(self, po_number: str, unit_price: float) -> int:
        """Re-price a PO after negotiation, keeping total_value consistent."""
        return self.execute(
            "UPDATE purchase_orders SET unit_price=?, total_value=ROUND(quantity*?, 2) WHERE po_number=?",
            (unit_price, unit_price, po_number)
        )

    def get_purchase_order(self, po_number: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM purchase_orders WHERE po_number=?", (po_number,))
        return rows[0] if rows else None

    def get_open_purchase_orders(self) -> list[dict]:
        placeholders = ", ".join("?" * len(OPEN_PO_STATUSES))
        return self.query(
            f"SELECT * FROM purchase_orders WHERE status IN ({placeholders}) ORDER BY created_at DESC",
            OPEN_PO_STATUSES
        )

    def log_negotiation(self, session_id: str, supplier_id: str, sku_id: str,
                         round_num: int, our_offer: float, their_offer: float,
                         status: str = "ongoing") -> int:
        return self.insert("negotiation_log", {
            "session_id": session_id,
            "supplier_id": supplier_id,
            "sku_id": sku_id,
            "round_num": round_num,
            "our_offer": our_offer,
            "their_offer": their_offer,
            "status": status,
            "timestamp": datetime.utcnow().isoformat()
        })

    def get_negotiation_history(self, session_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM negotiation_log WHERE session_id=? ORDER BY round_num",
            (session_id,)
        )

    def log_disruption(self, event_type: str, region: str, severity: int,
                        affected_skus: list, description: str = "") -> int:
        return self.insert("disruption_events", {
            "event_type": event_type,
            "region": region,
            "severity": severity,
            "affected_skus": json.dumps(affected_skus),
            "description": description,
            "detected_at": datetime.utcnow().isoformat()
        })

    def record_disruption(self, event_type: str, region: str, severity: int,
                          affected_skus: list, description: str = "") -> int:
        """Open a disruption event, or refresh the matching one already open.

        The risk agent re-assesses the same standing conditions on every cycle.
        Inserting unconditionally would pile up identical rows in an open-events
        table, so an unresolved event with the same type, region and SKU set is
        updated in place instead — keeping its original `detected_at`, which is
        what "how long has this been open" is measured from.
        """
        skus_json = json.dumps(affected_skus)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM disruption_events WHERE event_type=? AND region=? "
                "AND affected_skus=? AND resolved_at IS NULL LIMIT 1",
                (event_type, region, skus_json)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE disruption_events SET severity=?, description=? WHERE id=?",
                    (severity, description, existing["id"])
                )
                return existing["id"]
            cur = conn.execute(
                "INSERT INTO disruption_events (event_type, region, severity, "
                "affected_skus, description, detected_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event_type, region, severity, skus_json, description,
                 datetime.utcnow().isoformat())
            )
            return cur.lastrowid

    def resolve_disruptions_except(self, keep_ids: list) -> int:
        """Close every open event the latest assessment did not re-report."""
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            if keep_ids:
                placeholders = ", ".join("?" * len(keep_ids))
                cur = conn.execute(
                    "UPDATE disruption_events SET resolved_at=? WHERE resolved_at IS NULL "
                    f"AND id NOT IN ({placeholders})",
                    (now, *keep_ids)
                )
            else:
                cur = conn.execute(
                    "UPDATE disruption_events SET resolved_at=? WHERE resolved_at IS NULL",
                    (now,)
                )
            return cur.rowcount

    def get_active_disruptions(self) -> list[dict]:
        rows = self.query(
            "SELECT * FROM disruption_events WHERE resolved_at IS NULL ORDER BY severity DESC"
        )
        for r in rows:
            r["affected_skus"] = json.loads(r["affected_skus"])
        return rows

    def get_all_purchase_orders(self) -> list[dict]:
        return self.query("SELECT * FROM purchase_orders ORDER BY created_at DESC")

    def get_all_supplier_scores(self) -> list[dict]:
        return self.query("""
            SELECT ss.* FROM supplier_scores ss
            INNER JOIN (
                SELECT supplier_id, MAX(date) as max_date FROM supplier_scores GROUP BY supplier_id
            ) latest ON ss.supplier_id=latest.supplier_id AND ss.date=latest.max_date
            ORDER BY ss.overall DESC
        """)

    def save_cycle_run(self, cycle_id: str, started_at: str, completed_at: str,
                        duration_seconds: float, mode: str, pos_created: int,
                        risks_found: int, summary: dict) -> None:
        """Persist a cycle summary so `main.py --report` can replay history."""
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO cycle_runs (cycle_id, started_at, completed_at, duration_seconds,
                                        mode, pos_created, risks_found, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    completed_at=excluded.completed_at,
                    duration_seconds=excluded.duration_seconds,
                    mode=excluded.mode,
                    pos_created=excluded.pos_created,
                    risks_found=excluded.risks_found,
                    summary=excluded.summary
            """, (cycle_id, started_at, completed_at, round(duration_seconds, 2), mode,
                  pos_created, risks_found, json.dumps(summary)))

    def get_cycle_runs(self, limit: int = 10) -> list[dict]:
        rows = self.query(
            "SELECT * FROM cycle_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        for r in rows:
            try:
                r["summary"] = json.loads(r["summary"])
            except (TypeError, ValueError):
                r["summary"] = {}
        return rows

    def get_latest_forecasts(self, horizon_days: int = 30) -> list[dict]:
        """Most recent forecast per SKU for one horizon."""
        return self.query("""
            SELECT df.* FROM demand_forecasts df
            INNER JOIN (
                SELECT sku_id, MAX(created_at) AS max_created
                FROM demand_forecasts WHERE horizon_days=? GROUP BY sku_id
            ) latest ON df.sku_id=latest.sku_id AND df.created_at=latest.max_created
            WHERE df.horizon_days=?
            ORDER BY df.sku_id
        """, (horizon_days, horizon_days))

    def get_po_status_counts(self) -> dict:
        rows = self.query(
            "SELECT status, COUNT(*) AS n, SUM(total_value) AS value "
            "FROM purchase_orders GROUP BY status"
        )
        return {r["status"]: {"count": r["n"], "value": round(r["value"] or 0.0, 2)} for r in rows}
