"""Сховище на SQLite: сесії анкети, заявки, підключення менеджера.

Навмисно без ORM — стандартна бібліотека, нульові залежності.
Якщо колись знадобиться PostgreSQL, замінюється лише цей файл.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Order, Quote
from ..questionnaire.engine import Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    channel          TEXT NOT NULL,
    external_user_id TEXT,
    state            TEXT NOT NULL,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (channel, external_user_id);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    channel          TEXT NOT NULL,
    external_user_id TEXT,
    answers          TEXT NOT NULL,
    quote            TEXT,
    total            REAL,
    status           TEXT NOT NULL DEFAULT 'new',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status, created_at);

CREATE TABLE IF NOT EXISTS handoffs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER,
    session_id   TEXT NOT NULL,
    channel      TEXT NOT NULL,
    client_ref   TEXT NOT NULL,      -- chat_id клієнта або інший ідентифікатор
    client_name  TEXT,
    client_phone TEXT,
    manager_ref  TEXT,               -- chat_id менеджера, який узяв заявку
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'requested',  -- requested|active|closed
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs (status);
CREATE INDEX IF NOT EXISTS idx_handoffs_client ON handoffs (client_ref, status);

CREATE TABLE IF NOT EXISTS handoff_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,
    sender     TEXT NOT NULL,        -- client | manager
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass
class Handoff:
    """Запит на підключення менеджера до бесіди."""

    id: int | None
    session_id: str
    channel: str
    client_ref: str
    client_name: str = ""
    client_phone: str = ""
    manager_ref: str | None = None
    order_id: int | None = None
    reason: str = ""
    status: str = "requested"
    created_at: datetime = field(default_factory=datetime.now)


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------- сесії ----------------
    def save_session(self, session: Session) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, channel, external_user_id, state, updated_at)"
                " VALUES (?, ?, ?, ?, datetime('now'))"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "   state = excluded.state, updated_at = datetime('now')",
                (
                    session.session_id,
                    session.channel,
                    session.external_user_id,
                    json.dumps(session.to_dict(), ensure_ascii=False),
                ),
            )

    def load_session(self, session_id: str) -> Session | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return Session.from_dict(json.loads(row["state"])) if row else None

    def find_active_session(self, channel: str, external_user_id: str) -> Session | None:
        """Остання незавершена анкета користувача (щоб продовжити з місця зупинки)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM sessions WHERE channel = ? AND external_user_id = ?"
                " ORDER BY updated_at DESC LIMIT 5",
                (channel, str(external_user_id)),
            ).fetchall()
        for row in rows:
            session = Session.from_dict(json.loads(row["state"]))
            if not session.is_complete:
                return session
        return None

    def delete_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # ---------------- заявки ----------------
    def save_order(self, order: Order) -> int:
        payload = (
            order.session_id,
            order.channel,
            order.external_user_id,
            json.dumps(order.answers, ensure_ascii=False, default=str),
            json.dumps(order.quote.as_dict(), ensure_ascii=False) if order.quote else None,
            order.quote.total if order.quote else None,
            order.status,
        )
        with self._connect() as conn:
            if order.id:
                conn.execute(
                    "UPDATE orders SET session_id=?, channel=?, external_user_id=?,"
                    " answers=?, quote=?, total=?, status=? WHERE id=?",
                    (*payload, order.id),
                )
                return order.id
            cursor = conn.execute(
                "INSERT INTO orders (session_id, channel, external_user_id, answers,"
                " quote, total, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            order.id = int(cursor.lastrowid)
            return order.id

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["answers"] = json.loads(data["answers"])
        data["quote"] = json.loads(data["quote"]) if data["quote"] else None
        return data

    def recent_orders(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, created_at, total, status, answers FROM orders"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, limit)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["answers"] = json.loads(data["answers"])
            out.append(data)
        return out

    def set_order_status(self, order_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

    # ---------------- підключення менеджера ----------------
    def create_handoff(self, handoff: Handoff) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO handoffs (order_id, session_id, channel, client_ref,"
                " client_name, client_phone, reason, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'requested')",
                (
                    handoff.order_id,
                    handoff.session_id,
                    handoff.channel,
                    str(handoff.client_ref),
                    handoff.client_name,
                    handoff.client_phone,
                    handoff.reason,
                ),
            )
            handoff.id = int(cursor.lastrowid)
            return handoff.id

    def claim_handoff(self, handoff_id: int, manager_ref: str) -> bool:
        """Менеджер бере заявку. False = хтось уже взяв раніше."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE handoffs SET manager_ref = ?, status = 'active'"
                " WHERE id = ? AND status = 'requested'",
                (str(manager_ref), handoff_id),
            )
            return cursor.rowcount > 0

    def close_handoff(self, handoff_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE handoffs SET status = 'closed', closed_at = datetime('now')"
                " WHERE id = ?",
                (handoff_id,),
            )

    def active_handoff_for_client(self, client_ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE client_ref = ? AND status IN"
                " ('requested', 'active') ORDER BY id DESC LIMIT 1",
                (str(client_ref),),
            ).fetchone()
        return dict(row) if row else None

    def active_handoff_for_manager(self, manager_ref: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE manager_ref = ? AND status = 'active'"
                " ORDER BY id DESC LIMIT 1",
                (str(manager_ref),),
            ).fetchone()
        return dict(row) if row else None

    def get_handoff(self, handoff_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        return dict(row) if row else None

    def log_message(self, handoff_id: int, sender: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO handoff_messages (handoff_id, sender, text) VALUES (?, ?, ?)",
                (handoff_id, sender, text),
            )

    def history(self, handoff_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sender, text, created_at FROM handoff_messages"
                " WHERE handoff_id = ? ORDER BY id LIMIT ?",
                (handoff_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            orders = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(AVG(total), 0) AS avg_total FROM orders"
            ).fetchone()
            today = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE date(created_at) = date('now')"
            ).fetchone()
            waiting = conn.execute(
                "SELECT COUNT(*) AS n FROM handoffs WHERE status = 'requested'"
            ).fetchone()
        return {
            "orders_total": orders["n"],
            "orders_today": today["n"],
            "average_total": round(orders["avg_total"], 2),
            "handoffs_waiting": waiting["n"],
        }
