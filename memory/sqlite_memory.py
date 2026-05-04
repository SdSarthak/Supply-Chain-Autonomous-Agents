import sqlite3
import json
from datetime import datetime
from typing import Any, Optional
from config import SQLITE_PATH


class SQLiteMemory:
    def __init__(self, db_path: str = SQLITE_PATH):
        self.db_path = db_path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
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

                CREATE INDEX IF NOT EXISTS idx_demand_sku ON demand_forecasts(sku_id);
                CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status);
                CREATE INDEX IF NOT EXISTS idx_neg_session ON negotiation_log(session_id);
            """)

    def insert(self, table: str, data: dict) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._conn() as conn:
            cur = conn.execute(sql, list(data.values()))
            return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._conn() as conn:
            conn.execute(sql, params)

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
        overall = round((delivery * 0.4 + quality * 0.35 + price * 0.25), 4)
        today = datetime.utcnow().date().isoformat()
        with self._conn() as conn:
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
                          expected_delivery: str = None) -> None:
        if route_id and expected_delivery:
            self.execute(
                "UPDATE purchase_orders SET status=?, route_id=?, expected_delivery=? WHERE po_number=?",
                (status, route_id, expected_delivery, po_number)
            )
        else:
            self.execute(
                "UPDATE purchase_orders SET status=? WHERE po_number=?",
                (status, po_number)
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
