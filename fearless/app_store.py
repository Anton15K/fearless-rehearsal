"""SQLite persistence for transactional rehearsal plan edits."""

from collections.abc import Callable
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from fearless.app_error import RehearsalError


class PlanStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, owner TEXT, snapshot TEXT NOT NULL)"
            )
            columns = {column[1] for column in db.execute("PRAGMA table_info(plans)")}
            if "owner" not in columns:
                db.execute("ALTER TABLE plans ADD COLUMN owner TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS plans_owner ON plans (owner)")

    def create(self, owner: str, plan: dict) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO plans (id, owner, snapshot) VALUES (?, ?, ?)",
                (plan["id"], owner, self._encode(plan)),
            )

    def list(self, owner: str) -> list[dict]:
        with closing(self.connect()) as db:
            return [
                json.loads(snapshot)
                for (snapshot,) in db.execute(
                    "SELECT snapshot FROM plans WHERE owner = ? ORDER BY rowid DESC", (owner,),
                )
            ]

    def get(self, owner: str, plan_id: str) -> dict:
        with closing(self.connect()) as db:
            return self._get(db, owner, plan_id)

    def edit(self, owner: str, plan_id: str, update: Callable[[dict], None]) -> dict:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._get(db, owner, plan_id)
            update(plan)
            db.execute(
                "UPDATE plans SET snapshot = ? WHERE id = ? AND owner = ?",
                (self._encode(plan), plan_id, owner),
            )
        return plan

    @staticmethod
    def _get(db: sqlite3.Connection, owner: str, plan_id: str) -> dict:
        result = db.execute(
            "SELECT snapshot FROM plans WHERE id = ? AND owner = ?", (plan_id, owner),
        ).fetchone()
        if result is None:
            raise RehearsalError(404, "Rehearsal plan not found.")
        return json.loads(result[0])

    @staticmethod
    def _encode(plan: dict) -> str:
        return json.dumps(plan, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
