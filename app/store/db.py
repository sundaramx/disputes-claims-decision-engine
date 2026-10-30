"""SQLite-backed store.

Design: narrow repository API over a hybrid schema — a few indexed columns for the
query paths that matter (status, queue, dates, merchant) and a JSON document column
holding the full pydantic payload. At demo scale (thousands of cases) aggregate KPIs
are computed in Python from these rows; in production this layer maps to the shared
lakehouse the paper describes (§5).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from app.domain.models import (
    Account,
    Case,
    Claim,
    Customer,
    Decision,
    EvidenceItem,
    LedgerEntry,
    Outcome,
    Transaction,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_accounts_customer ON accounts(customer_id);
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  merchant_id TEXT NOT NULL, occurred_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_txn_customer_merchant ON transactions(customer_id, merchant_id);
CREATE INDEX IF NOT EXISTS ix_txn_customer_time ON transactions(customer_id, occurred_at);
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, filed_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_claims_customer ON claims(customer_id, filed_at);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  status TEXT NOT NULL, queue TEXT NOT NULL, regime TEXT NOT NULL,
  opened_at TEXT NOT NULL, resolved_at TEXT, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS ix_cases_queue ON cases(queue);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, decided_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_decisions_case ON decisions(case_id);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_evidence_case ON evidence(case_id);
CREATE TABLE IF NOT EXISTS outcomes (
  case_id TEXT PRIMARY KEY, closed_at TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inquiries (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, case_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ledger_case ON ledger(case_id);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # -- generic helpers -------------------------------------------------
    def _put(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- customers / accounts / transactions -----------------------------
    def put_customer(self, c: Customer) -> None:
        self._put("INSERT OR REPLACE INTO customers VALUES (?,?)", (c.id, c.model_dump_json()))

    def get_customer(self, cid: str) -> Optional[Customer]:
        r = self._rows("SELECT doc FROM customers WHERE id=?", (cid,))
        return Customer.model_validate_json(r[0]["doc"]) if r else None

    def put_account(self, a: Account) -> None:
        self._put("INSERT OR REPLACE INTO accounts VALUES (?,?,?)",
                  (a.id, a.customer_id, a.model_dump_json()))

    def get_account(self, aid: str) -> Optional[Account]:
        r = self._rows("SELECT doc FROM accounts WHERE id=?", (aid,))
        return Account.model_validate_json(r[0]["doc"]) if r else None

    def put_transaction(self, t: Transaction) -> None:
        self._put("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?)",
                  (t.id, t.account_id, t.customer_id, t.merchant_id,
                   t.occurred_at.isoformat(), t.model_dump_json()))

    def get_transaction(self, tid: str) -> Optional[Transaction]:
        r = self._rows("SELECT doc FROM transactions WHERE id=?", (tid,))
        return Transaction.model_validate_json(r[0]["doc"]) if r else None

    def transactions_for_customer(self, customer_id: str,
                                  merchant_id: Optional[str] = None) -> list[Transaction]:
        if merchant_id:
            rows = self._rows(
                "SELECT doc FROM transactions WHERE customer_id=? AND merchant_id=? "
                "ORDER BY occurred_at", (customer_id, merchant_id))
        else:
            rows = self._rows(
                "SELECT doc FROM transactions WHERE customer_id=? ORDER BY occurred_at",
                (customer_id,))
        return [Transaction.model_validate_json(r["doc"]) for r in rows]

    # -- claims / cases ---------------------------------------------------
    def put_claim(self, c: Claim) -> None:
        self._put("INSERT OR REPLACE INTO claims VALUES (?,?,?,?)",
                  (c.id, c.customer_id, c.filed_at.isoformat(), c.model_dump_json()))

    def get_claim(self, cid: str) -> Optional[Claim]:
        r = self._rows("SELECT doc FROM claims WHERE id=?", (cid,))
        return Claim.model_validate_json(r[0]["doc"]) if r else None

    def claims_for_customer(self, customer_id: str) -> list[Claim]:
        rows = self._rows("SELECT doc FROM claims WHERE customer_id=? ORDER BY filed_at",
                          (customer_id,))
        return [Claim.model_validate_json(r["doc"]) for r in rows]

    def put_case(self, c: Case) -> None:
        self._put(
            "INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?,?,?)",
            (c.id, c.claim_id, c.customer_id, c.status.value, c.assigned_queue,
             c.regime.value, c.opened_at.isoformat(),
             c.resolved_at.isoformat() if c.resolved_at else None, c.model_dump_json()))

    def get_case(self, cid: str) -> Optional[Case]:
        r = self._rows("SELECT doc FROM cases WHERE id=?", (cid,))
        return Case.model_validate_json(r[0]["doc"]) if r else None

    def cases(self, status: Optional[str] = None, queue: Optional[str] = None,
              limit: int = 10000) -> list[Case]:
        sql, params = "SELECT doc FROM cases", []
        conds = []
        if status:
            conds.append("status=?"); params.append(status)
        if queue:
            conds.append("queue=?"); params.append(queue)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY opened_at LIMIT ?"
        params.append(limit)
        return [Case.model_validate_json(r["doc"]) for r in self._rows(sql, tuple(params))]

    def all_cases(self) -> list[Case]:
        return [Case.model_validate_json(r["doc"])
                for r in self._rows("SELECT doc FROM cases ORDER BY opened_at")]

    # -- decisions / evidence / outcomes ----------------------------------
    def put_decision(self, d: Decision) -> None:
        self._put("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)",
                  (d.id, d.case_id, d.decided_at.isoformat(), d.model_dump_json()))

    def decisions_for_case(self, case_id: str) -> list[Decision]:
        rows = self._rows("SELECT doc FROM decisions WHERE case_id=? ORDER BY decided_at",
                          (case_id,))
        return [Decision.model_validate_json(r["doc"]) for r in rows]

    def all_decisions(self) -> list[Decision]:
        return [Decision.model_validate_json(r["doc"])
                for r in self._rows("SELECT doc FROM decisions")]

    def put_evidence(self, e: EvidenceItem) -> None:
        self._put("INSERT OR REPLACE INTO evidence VALUES (?,?,?)",
                  (e.id, e.case_id, e.model_dump_json()))

    def evidence_for_case(self, case_id: str) -> list[EvidenceItem]:
        rows = self._rows("SELECT doc FROM evidence WHERE case_id=?", (case_id,))
        return [EvidenceItem.model_validate_json(r["doc"]) for r in rows]

    def put_outcome(self, o: Outcome) -> None:
        self._put("INSERT OR REPLACE INTO outcomes VALUES (?,?,?)",
                  (o.case_id, o.closed_at.isoformat(), o.model_dump_json()))

    def get_outcome(self, case_id: str) -> Optional[Outcome]:
        r = self._rows("SELECT doc FROM outcomes WHERE case_id=?", (case_id,))
        return Outcome.model_validate_json(r[0]["doc"]) if r else None

    def all_outcomes(self) -> list[Outcome]:
        return [Outcome.model_validate_json(r["doc"])
                for r in self._rows("SELECT doc FROM outcomes")]

    # -- inquiries (pre-claim deflection surface) --------------------------
    def put_inquiry(self, i) -> None:
        self._put("INSERT OR REPLACE INTO inquiries VALUES (?,?,?)",
                  (i.id, i.customer_id, i.model_dump_json()))

    def all_inquiries(self) -> list:
        from app.domain.models import Inquiry
        return [Inquiry.model_validate_json(r["doc"])
                for r in self._rows("SELECT doc FROM inquiries")]

    # -- ledger (append-only; hashing lives in decisioning.ledger) ---------
    def append_ledger(self, ts: datetime, case_id: str, event_type: str, payload: str,
                      payload_hash: str, prev_hash: str, hash_: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO ledger (ts, case_id, event_type, payload, payload_hash, "
                "prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts.isoformat(), case_id, event_type, payload, payload_hash, prev_hash, hash_))
            self._conn.commit()
            return int(cur.lastrowid)

    def ledger_entries(self, case_id: Optional[str] = None) -> list[LedgerEntry]:
        if case_id:
            rows = self._rows("SELECT * FROM ledger WHERE case_id=? ORDER BY seq", (case_id,))
        else:
            rows = self._rows("SELECT * FROM ledger ORDER BY seq")
        return [LedgerEntry(seq=r["seq"], ts=datetime.fromisoformat(r["ts"]),
                            case_id=r["case_id"], event_type=r["event_type"],
                            payload=json.loads(r["payload"]), payload_hash=r["payload_hash"],
                            prev_hash=r["prev_hash"], hash=r["hash"]) for r in rows]

    def last_ledger_row(self) -> Optional[sqlite3.Row]:
        r = self._rows("SELECT * FROM ledger ORDER BY seq DESC LIMIT 1")
        return r[0] if r else None
