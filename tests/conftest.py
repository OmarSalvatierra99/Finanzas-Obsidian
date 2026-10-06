import os
from pathlib import Path
import sys
import tempfile
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Importing app creates its default database; keep that side effect outside real data.
_bootstrap = tempfile.TemporaryDirectory(prefix="finanzas-tests-")
os.environ["FINANZAS_DB_PATH"] = str(Path(_bootstrap.name) / "bootstrap.sqlite3")
os.environ["QUINCENAL_INCOME_CENTS"] = "1000000"
import app as finance


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(finance, "_local_today", lambda timezone: date(2026, 10, 5))
    app = finance.create_app({"TESTING": True, "AUTH_MODE": "none", "SECRET_KEY": "test-only",
                              "DATABASE_PATH": tmp_path / "test.sqlite3",
                              "LOGS_DIR": tmp_path / "logs"})
    with finance._db_connection(app) as db:
        for table in ("xml_imports", "movement_allocations", "movements", "debt_balance_adjustments",
                      "scheduled_debt_payments", "debts", "commitment_exceptions", "recurring_commitments"):
            db.execute(f"DELETE FROM {table}")
    app.config["AUTH_USER"] = {"username": "admin", "display_name": "Test", "password_hash": "test-only"}
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "test-token"
        session["auth_user"] = "admin"
    return app, client


def post(client, path, **fields):
    return client.post(path, data={"csrf_token": "test-token", **fields}, headers={"Accept": "application/json"})


def add_debt(app, amounts=(10000,), dates=("2026-10-05",), creditor_type="person"):
    with finance._db_connection(app) as db:
        debt_id = db.execute("""INSERT INTO debts
            (name, creditor, creditor_type, original_cents, balance_cents, due_on, created_at, schedule_type)
            VALUES ('Test', 'Test creditor', ?, ?, ?, ?, '2026-10-05', ?)""",
            (creditor_type, sum(amounts), sum(amounts), dates[0], "single" if len(amounts) == 1 else "custom")).lastrowid
        ids = [db.execute("INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at) VALUES (?, ?, ?, '2026-10-05')",
                          (debt_id, due, amount)).lastrowid for due, amount in zip(dates, amounts)]
    return debt_id, ids


def add_recurring(app):
    with finance._db_connection(app) as db:
        return db.execute("""INSERT INTO recurring_commitments
            (title, amount_cents, frequency, starts_on, weekday, category, created_at)
            VALUES ('Weekly test', 10000, 'weekly', '2026-10-05', 0, 'Deudas', '2026-10-05')""").lastrowid
