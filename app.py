#!/usr/bin/env python3
"""Agenda financiera personal."""

from __future__ import annotations

import calendar
import base64
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:
    system_packages = Path(f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    if not system_packages.is_dir():
        raise
    sys.path.append(str(system_packages))
    from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


ROOT_DIR = Path(__file__).resolve().parent


PROJECT_KEY = "27-finanzas"
ASSET_VERSION = max(path.stat().st_mtime_ns for path in (ROOT_DIR / "static").rglob("*") if path.suffix in {".css", ".js"})
KINDS = {"income": "Ingreso", "expense": "Gasto"}
UNPLANNED_EXPENSES = ("Transporte", "Comida", "Salud", "Servicios", "Otros")
EXPENSE_GROUPS = ({
    "label": "Gastos",
    "items": tuple({"value": name, "amount_cents": 0, "cadence": ""} for name in UNPLANNED_EXPENSES),
},)
CATEGORIES = {
    "income": ("Sueldo", "Venta", "Freelance", "Rendimiento", "Otro ingreso"),
    "expense": tuple(item["value"] for group in EXPENSE_GROUPS for item in group["items"]),
}
CREDITOR_TYPES = {
    "family": "Familia",
    "person": "Amigo/a",
    "coworker": "Compañero de trabajo",
    "bank": "Banco",
}
SHARED_GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "").strip().lower()
XML_SENDER_EMAIL = os.getenv("XML_SENDER_EMAIL", "").strip().lower()
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_AMOUNT_CENTS = 99_999_999_999
PRIORITIES = {"urgent": "Urgente", "high": "Alta", "normal": "Normal", "low": "Baja"}
SCHEDULE_TYPES = {
    "single": "Un pago", "fortnightly": "Quincenal", "monthly": "Mensual",
    "weekly": "Semanal", "custom": "Personalizado",
}
FUND_TYPES = {"emergency": "Emergencia", "family": "Familia", "planned": "Planificado", "reward": "Recompensa"}
BARBERSHOP_GOAL_TITLE = "Meta personal"
BARBERSHOP_LEGACY_GOAL_TITLE = "Meta de ahorro"
BARBERSHOP_GOAL_TITLES = (BARBERSHOP_GOAL_TITLE, BARBERSHOP_LEGACY_GOAL_TITLE)
BARBERSHOP_GOAL_TARGET_CENTS = 0
BARBERSHOP_GOAL_TARGET_ON = ""
COMMITMENT_FREQUENCIES = {
    "monthly": "Mensual", "fortnightly": "Quincenal", "weekly": "Semanal",
    "weekdays": "Lunes a viernes", "once": "Una fecha",
}
MONTH_NAMES = (
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_path() -> Path:
    explicit_path = os.getenv("FINANZAS_DB_PATH", "").strip()
    if explicit_path:
        return Path(explicit_path)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url.startswith("sqlite:///"):
        path = Path(database_url.removeprefix("sqlite:///"))
        return path if path.is_absolute() else ROOT_DIR / path
    return ROOT_DIR / "instance" / "finanzas.sqlite3"


def _money_to_cents(value: object) -> int:
    raw_value = str(value or "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(raw_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Captura un monto valido.") from None
    cents = int(amount * 100)
    if cents <= 0:
        raise ValueError("El monto debe ser mayor a cero.")
    if cents > MAX_AMOUNT_CENTS:
        raise ValueError("El monto excede el limite permitido.")
    return cents


def _optional_money_to_cents(value: object) -> int:
    return _money_to_cents(value) if str(value or "").strip() else 0


def _nonnegative_money_to_cents(value: object) -> int:
    raw_value = str(value or "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(raw_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Captura un monto valido.") from None
    cents = int(amount * 100)
    if not 0 <= cents <= MAX_AMOUNT_CENTS:
        raise ValueError("El monto debe estar entre cero y el limite permitido.")
    return cents


def _format_money(cents: int | None) -> str:
    amount = Decimal(abs(int(cents or 0))) / Decimal(100)
    sign = "-" if int(cents or 0) < 0 else ""
    return f"{sign}${amount:,.2f}"


def _parse_date(value: object, field_name: str) -> str:
    try:
        return date.fromisoformat(str(value or "").strip()).isoformat()
    except ValueError:
        raise ValueError(f"Captura una fecha valida para {field_name}.") from None


def _local_today(timezone_name: str = "America/Mexico_City", now: datetime | None = None) -> date:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(ZoneInfo(timezone_name)).date()


def _month_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month_zero = divmod(month_index, 12)
    return _month_date(year, month_zero + 1, value.day)


def _fortnight_dates(value: date, count: int) -> list[date]:
    month_end = _month_date(value.year, value.month, 30)
    current = date(value.year, value.month, 15) if value.day <= 15 else month_end
    if value > month_end:
        current = _add_months(date(value.year, value.month, 15), 1)
    dates = []
    for _ in range(count):
        dates.append(current)
        current = (
            _month_date(current.year, current.month, 30)
            if current.day == 15
            else _add_months(date(current.year, current.month, 15), 1)
        )
    return dates


def _scheduled_amounts(total: int, count: int, requested: int) -> list[int]:
    if total < count:
        raise ValueError("El monto no alcanza para crear esa cantidad de cuotas.")
    if requested <= 0:
        requested = (total + count - 1) // count
    if count == 1:
        return [total]
    amounts = [requested] * (count - 1)
    last = total - sum(amounts)
    if last > 0:
        return [*amounts, last]
    base, remainder = divmod(total, count)
    return [base + int(index < remainder) for index in range(count)]


def _parse_custom_schedule(raw: object) -> list[tuple[str, int]]:
    rows = []
    try:
        parsed = csv.reader(io.StringIO(str(raw or "").strip()))
        for line_number, row in enumerate(parsed, 1):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != 2:
                raise ValueError(f"La linea {line_number} debe tener fecha y monto.")
            rows.append((_parse_date(row[0], f"la linea {line_number}"), _money_to_cents(row[1])))
    except csv.Error:
        raise ValueError("El calendario personalizado no tiene un formato CSV valido.") from None
    if not rows:
        raise ValueError("Captura al menos una cuota personalizada.")
    if len({due_on for due_on, _ in rows}) != len(rows):
        raise ValueError("Cada cuota personalizada debe tener una fecha distinta.")
    return sorted(rows)


def _clean_text(value: object, field_name: str, *, required: bool = True, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError(f"Captura {field_name}.")
    if len(text) > limit:
        raise ValueError(f"{field_name.capitalize()} no puede exceder {limit} caracteres.")
    return text


def _debt_form_data(form) -> dict:
    creditor_value = form.get("creditor")
    if not creditor_value and form.get("schedule_type") == "single":
        creditor_value = form.get("name")
    creditor = _clean_text(creditor_value, "a quien le debes", limit=100)
    name = _clean_text(form.get("name") or creditor, "el nombre de la deuda", limit=100)
    creditor_type = str(form.get("creditor_type") or "person").strip()
    if creditor_type not in CREDITOR_TYPES:
        raise ValueError("Selecciona tu relacion con la persona o institucion.")
    amount = _money_to_cents(form.get("amount"))
    balance_raw = str(form.get("balance") or "").strip()
    balance = _nonnegative_money_to_cents(balance_raw) if balance_raw else amount
    requested_mode = str(form.get("payment_mode") or "single").strip()
    schedule_type = str(form.get("schedule_type") or ("monthly" if requested_mode == "installments" else "single")).strip()
    if schedule_type not in SCHEDULE_TYPES:
        raise ValueError("Selecciona un calendario de pago valido.")
    payment_mode = "single" if schedule_type == "single" else "installments"
    priority = str(form.get("priority") or "normal").strip()
    if priority not in PRIORITIES:
        raise ValueError("Selecciona una prioridad valida.")
    min_payment = _optional_money_to_cents(form.get("min_payment"))
    if min_payment > amount:
        raise ValueError("El pago minimo no puede superar el saldo inicial.")
    installment_count = 1
    if schedule_type in {"fortnightly", "monthly", "weekly"}:
        count_raw = str(form.get("installment_count") or "").strip()
        try:
            installment_count = int(count_raw) if count_raw else (amount + min_payment - 1) // min_payment
        except (ValueError, ZeroDivisionError):
            raise ValueError("Captura el monto de cada abono.") from None
        if not 1 <= installment_count <= 360:
            raise ValueError("Los pagos a plazos deben tener entre 1 y 360 pagos.")
    if schedule_type == "single":
        min_payment = amount
    elif schedule_type in {"fortnightly", "monthly", "weekly"} and not min_payment:
        min_payment = (amount + installment_count - 1) // installment_count
    try:
        reminder_days = int(form.get("reminder_days") or 1)
    except ValueError:
        raise ValueError("Selecciona un recordatorio valido.") from None
    if reminder_days not in {0, 1}:
        raise ValueError("Selecciona un recordatorio valido.")
    custom_raw = str(form.get("custom_schedule") or "").strip()
    custom_rows = _parse_custom_schedule(custom_raw) if schedule_type == "custom" and custom_raw else []
    if schedule_type == "custom" and not custom_rows and not form.get("calendar_pending"):
        raise ValueError("Captura al menos una cuota personalizada.")
    due_on = custom_rows[0][0] if custom_rows else ("" if schedule_type == "single" and not form.get("due_on") else _parse_date(form.get("due_on"), "el vencimiento"))
    if custom_rows:
        installment_count = len(custom_rows)
        min_payment = custom_rows[0][1]
    amounts = _scheduled_amounts(amount, installment_count, min_payment)
    if schedule_type == "single":
        schedule = [(due_on, amount)] if due_on else []
    elif schedule_type == "fortnightly":
        dates = _fortnight_dates(date.fromisoformat(due_on), installment_count)
        schedule = [(due.isoformat(), installment) for due, installment in zip(dates, amounts)]
        due_on = schedule[0][0]
    elif schedule_type == "monthly":
        first = date.fromisoformat(due_on)
        schedule = [(_add_months(first, index).isoformat(), installment) for index, installment in enumerate(amounts)]
    elif schedule_type == "weekly":
        first = date.fromisoformat(due_on)
        schedule = [((first + timedelta(days=7 * index)).isoformat(), installment) for index, installment in enumerate(amounts)]
    else:
        schedule = custom_rows
    return {
        "name": name,
        "creditor": creditor,
        "creditor_type": creditor_type,
        "amount": amount,
        "balance": balance,
        "payment_mode": payment_mode,
        "schedule_type": schedule_type,
        "priority": priority,
        "installment_count": installment_count,
        "min_payment": min_payment,
        "reminder_days": reminder_days,
        "due_on": due_on,
        "schedule": schedule,
        "notes": _clean_text(form.get("notes"), "las notas", required=False, limit=300),
    }


def _gmail_form_credentials(form) -> tuple[str, str]:
    email = _clean_text(form.get("email"), "el correo de Gmail", limit=254).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Captura un correo valido.")
    if SHARED_GMAIL_EMAIL and email != SHARED_GMAIL_EMAIL:
        raise ValueError(f"La cuenta debe ser {SHARED_GMAIL_EMAIL}.")
    password = str(form.get("app_password") or "").replace(" ", "")
    if password and len(password) != 16:
        raise ValueError("La clave de aplicacion de Gmail debe tener 16 caracteres.")
    return email, password


def _selected_month(value: object) -> str:
    month = str(value or "").strip()
    return month if MONTH_RE.fullmatch(month) else _local_today(os.getenv("APP_TIMEZONE", "America/Mexico_City")).strftime("%Y-%m")


def _month_bounds(month: str) -> tuple[str, str]:
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _secret_cipher(app: Flask) -> Fernet:
    digest = hashlib.sha256(str(app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _gmail_credentials(connection: sqlite3.Connection, app: Flask) -> dict:
    values = {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key, value FROM settings")
    }
    encrypted_password = values.get("gmail_app_password", "")
    try:
        stored_password = (
            _secret_cipher(app).decrypt(encrypted_password.encode()).decode()
            if encrypted_password
            else ""
        )
    except InvalidToken:
        stored_password = ""
    return {
        "email": values.get("gmail_email", "") or SHARED_GMAIL_EMAIL,
        "password": stored_password or os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        "stored_password": bool(encrypted_password and stored_password),
    }


def _reminder_settings(connection: sqlite3.Connection, app: Flask) -> dict:
    settings = _gmail_credentials(connection, app)
    enabled = connection.execute(
        "SELECT value FROM settings WHERE key = 'reminders_enabled'"
    ).fetchone()
    settings["enabled"] = not enabled or enabled["value"] == "1"
    return settings


def _xml_gmail_settings(connection: sqlite3.Connection, app: Flask) -> dict:
    settings = _gmail_credentials(connection, app)
    enabled = connection.execute(
        "SELECT value FROM settings WHERE key = 'xml_imports_enabled'"
    ).fetchone()
    settings["enabled"] = not enabled or enabled["value"] == "1"
    return settings


def _send_gmail(settings: dict, message) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(settings["email"], settings["password"])
        smtp.send_message(message)


def _connect_db(app: Flask) -> sqlite3.Connection:
    path = Path(app.config["DATABASE_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _db_connection(app: Flask):
    connection = _connect_db(app)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _init_db(app: Flask) -> None:
    from minimal_debts import backup_before_migration
    backup_before_migration(app.config["DATABASE_PATH"])
    with _db_connection(app) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                creditor TEXT NOT NULL DEFAULT '',
                creditor_type TEXT NOT NULL DEFAULT 'person' CHECK (creditor_type IN ('coworker', 'family', 'person', 'bank')),
                original_cents INTEGER NOT NULL CHECK (original_cents > 0),
                balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
                due_on TEXT NOT NULL,
                min_payment_cents INTEGER NOT NULL DEFAULT 0 CHECK (min_payment_cents >= 0),
                payment_mode TEXT NOT NULL DEFAULT 'single' CHECK (payment_mode IN ('single', 'installments')),
                installment_count INTEGER NOT NULL DEFAULT 1 CHECK (installment_count >= 1),
                reminder_days INTEGER NOT NULL DEFAULT 1 CHECK (reminder_days BETWEEN 0 AND 1),
                reminded_due_on TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                occurred_on TEXT NOT NULL,
                debt_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS debt_balance_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL REFERENCES debts(id) ON DELETE RESTRICT,
                previous_cents INTEGER NOT NULL CHECK (previous_cents >= 0),
                balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                target_on TEXT NOT NULL DEFAULT '',
                target_cents INTEGER NOT NULL DEFAULT 0 CHECK (target_cents >= 0),
                saved_cents INTEGER NOT NULL DEFAULT 0 CHECK (saved_cents >= 0),
                notes TEXT NOT NULL DEFAULT '',
                is_completed INTEGER NOT NULL DEFAULT 0 CHECK (is_completed IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expense_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
                cadence TEXT NOT NULL DEFAULT 'Sin frecuencia',
                group_name TEXT NOT NULL DEFAULT 'Flexibles y futuro',
                position INTEGER NOT NULL DEFAULT 0,
                requires_confirmation INTEGER NOT NULL DEFAULT 0 CHECK (requires_confirmation IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS xml_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                xml_hash TEXT NOT NULL UNIQUE,
                uuid TEXT UNIQUE,
                filename TEXT NOT NULL,
                movement_id INTEGER NOT NULL UNIQUE,
                imported_at TEXT NOT NULL,
                FOREIGN KEY (movement_id) REFERENCES movements(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scheduled_debt_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL,
                due_on TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at TEXT NOT NULL,
                UNIQUE (debt_id, due_on),
                FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recurring_commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                frequency TEXT NOT NULL CHECK (frequency IN ('weekdays', 'weekly', 'monthly', 'once')),
                starts_on TEXT NOT NULL,
                weekday INTEGER CHECK (weekday BETWEEN 0 AND 6),
                month_day INTEGER CHECK (month_day BETWEEN 1 AND 31),
                category TEXT NOT NULL DEFAULT 'Servicios',
                notes TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                auto_record INTEGER NOT NULL DEFAULT 0 CHECK (auto_record IN (0, 1)),
                deleted_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commitment_exceptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commitment_id INTEGER NOT NULL,
                occurrence_on TEXT NOT NULL,
                amount_cents INTEGER CHECK (amount_cents > 0),
                is_omitted INTEGER NOT NULL DEFAULT 0 CHECK (is_omitted IN (0, 1)),
                moved_to TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (commitment_id, occurrence_on),
                FOREIGN KEY (commitment_id) REFERENCES recurring_commitments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS movement_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                movement_id INTEGER NOT NULL,
                scheduled_debt_payment_id INTEGER,
                recurring_commitment_id INTEGER,
                occurrence_on TEXT NOT NULL DEFAULT '',
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at TEXT NOT NULL,
                CHECK ((scheduled_debt_payment_id IS NOT NULL) != (recurring_commitment_id IS NOT NULL)),
                FOREIGN KEY (movement_id) REFERENCES movements(id) ON DELETE CASCADE,
                FOREIGN KEY (scheduled_debt_payment_id) REFERENCES scheduled_debt_payments(id) ON DELETE CASCADE,
                FOREIGN KEY (recurring_commitment_id) REFERENCES recurring_commitments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS fund_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('contribution', 'withdrawal')),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                occurred_on TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE RESTRICT
            );


            CREATE INDEX IF NOT EXISTS idx_movements_occurred_on
                ON movements(occurred_on DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_debts_due_on
                ON debts(balance_cents, due_on);
            CREATE INDEX IF NOT EXISTS idx_goals_status
                ON goals(is_completed, target_on, id DESC);
            CREATE INDEX IF NOT EXISTS idx_scheduled_debt_dates
                ON scheduled_debt_payments(due_on, debt_id);
            CREATE INDEX IF NOT EXISTS idx_commitments_start
                ON recurring_commitments(is_active, starts_on, id);
            CREATE INDEX IF NOT EXISTS idx_commitment_exceptions_date
                ON commitment_exceptions(occurrence_on, commitment_id);
            CREATE INDEX IF NOT EXISTS idx_allocations_debt
                ON movement_allocations(scheduled_debt_payment_id, movement_id);
            CREATE INDEX IF NOT EXISTS idx_allocations_commitment
                ON movement_allocations(recurring_commitment_id, occurrence_on, movement_id);
            CREATE INDEX IF NOT EXISTS idx_fund_transactions_date
                ON fund_transactions(goal_id, occurred_on DESC, id DESC);
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(debts)")}
        if "deleted_at" not in columns:
            connection.execute("ALTER TABLE debts ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        migrated = False
        if "payment_mode" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'single'"
            )
            migrated = True
        if "installment_count" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN installment_count INTEGER NOT NULL DEFAULT 1"
            )
            migrated = True
        if "reminder_days" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN reminder_days INTEGER NOT NULL DEFAULT 1"
            )
        if "reminded_due_on" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN reminded_due_on TEXT NOT NULL DEFAULT ''"
            )
        if "creditor_type" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN creditor_type TEXT NOT NULL DEFAULT 'person'"
            )
        if "priority" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
            )
        if "schedule_type" not in columns:
            connection.execute(
                "ALTER TABLE debts ADD COLUMN schedule_type TEXT NOT NULL DEFAULT 'single'"
            )
        connection.execute("UPDATE debts SET reminder_days = 1 WHERE reminder_days > 1")
        if migrated:
            connection.execute(
                """
                UPDATE debts
                SET payment_mode = 'installments',
                    installment_count = (original_cents + min_payment_cents - 1) / min_payment_cents
                WHERE min_payment_cents > 0 AND min_payment_cents < original_cents
                """
            )
        connection.execute(
            "UPDATE debts SET schedule_type = CASE WHEN payment_mode = 'installments' THEN 'monthly' ELSE 'single' END WHERE schedule_type = 'single'"
        )
        goal_columns = {row["name"] for row in connection.execute("PRAGMA table_info(goals)")}
        if "target_cents" not in goal_columns:
            connection.execute(
                "ALTER TABLE goals ADD COLUMN target_cents INTEGER NOT NULL DEFAULT 0"
            )
        if "saved_cents" not in goal_columns:
            connection.execute(
                "ALTER TABLE goals ADD COLUMN saved_cents INTEGER NOT NULL DEFAULT 0"
            )
        if "fund_type" not in goal_columns:
            connection.execute(
                "ALTER TABLE goals ADD COLUMN fund_type TEXT NOT NULL DEFAULT 'planned'"
            )
        expense_category_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(expense_categories)")
        }
        if "requires_confirmation" not in expense_category_columns:
            connection.execute(
                "ALTER TABLE expense_categories ADD COLUMN requires_confirmation INTEGER NOT NULL DEFAULT 0 CHECK (requires_confirmation IN (0, 1))"
            )
        exception_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(commitment_exceptions)")
        }
        if "moved_to" not in exception_columns:
            connection.execute(
                "ALTER TABLE commitment_exceptions ADD COLUMN moved_to TEXT NOT NULL DEFAULT ''"
            )
        commitment_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(recurring_commitments)")
        }
        if "deleted_at" not in commitment_columns:
            connection.execute("ALTER TABLE recurring_commitments ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        if "auto_record" not in commitment_columns:
            connection.execute(
                "ALTER TABLE recurring_commitments ADD COLUMN auto_record INTEGER NOT NULL DEFAULT 0 CHECK (auto_record IN (0, 1))"
            )
        connection.execute(
            "UPDATE expense_categories SET group_name = 'Flexibles y futuro' WHERE group_name = 'Personalizados'"
        )
        connection.execute("DELETE FROM expense_categories WHERE lower(name) IN ('deuda', 'deudas')")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_debts_priority ON debts(priority, balance_cents, due_on)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_commitment_exceptions_moved ON commitment_exceptions(moved_to, commitment_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_goals_type ON goals(fund_type, is_completed, id)")
        from minimal_debts import initialize
        initialize(connection)
        now = _now()
        connection.executemany(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (
                ("urgent_debt_target_cents", "0", now),
            ),
        )
        if not connection.execute("SELECT 1 FROM expense_categories LIMIT 1").fetchone():
            position = 0
            for group in EXPENSE_GROUPS:
                for item in group["items"]:
                    position += 1
                    connection.execute(
                        """
                        INSERT INTO expense_categories
                            (name, amount_cents, cadence, group_name, position, requires_confirmation, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["value"], item["amount_cents"], item["cadence"], group["label"], position,
                            0, now,
                        ),
                    )
        position = connection.execute("SELECT COALESCE(MAX(position), 0) FROM expense_categories").fetchone()[0]
        connection.execute(
            """
            INSERT OR IGNORE INTO expense_categories
                (name, amount_cents, cadence, group_name, position, created_at)
            SELECT category, 0, 'Sin frecuencia', 'Flexibles y futuro',
                   ? + ROW_NUMBER() OVER (ORDER BY category), ?
            FROM (
                SELECT DISTINCT category FROM movements WHERE kind = 'expense'
                UNION
                SELECT DISTINCT category FROM recurring_commitments
            )
            WHERE lower(category) NOT IN ('deuda', 'deudas')
            """,
            (position, now),
        )
        connection.execute(
            "UPDATE movements SET category = 'Otros', description = 'Otros' "
            "WHERE kind = 'expense' AND category = 'Otro gasto'"
        )
        connection.execute(
            "DELETE FROM expense_categories WHERE name = 'Otro gasto'"
        )
        for offset, name in enumerate(UNPLANNED_EXPENSES, 1):
            requires_confirmation = int(name == "Otros")
            connection.execute(
                """
                INSERT INTO expense_categories
                    (name, amount_cents, cadence, group_name, position, requires_confirmation, created_at)
                VALUES (?, 0, '', 'Imprevistos', ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    amount_cents = 0, cadence = '', group_name = 'Imprevistos',
                    position = excluded.position, requires_confirmation = excluded.requires_confirmation
                """,
                (name, 1000 + offset, requires_confirmation, now),
            )
        connection.execute(
            "DELETE FROM expense_categories WHERE name = 'Pasajes / Transporte' "
            "AND NOT EXISTS (SELECT 1 FROM movements WHERE category = 'Pasajes / Transporte') "
            "AND NOT EXISTS (SELECT 1 FROM recurring_commitments WHERE category = 'Pasajes / Transporte')"
        )
        connection.execute(
            "UPDATE expense_categories SET amount_cents = 50000, cadence = '$500 por quincena', "
            "group_name = 'Movilidad y compromisos' WHERE name = 'Pasajes'"
        )
        connection.execute(
            "UPDATE expense_categories SET amount_cents = 70000, cadence = '$700 por quincena', "
            "group_name = 'Movilidad y compromisos' WHERE name = 'Transporte'"
        )
        for debt in connection.execute(
            """
            SELECT id, balance_cents, due_on, min_payment_cents, payment_mode, installment_count
            FROM debts
            WHERE balance_cents > 0 AND deleted_at = '' AND due_on != ''
              AND NOT EXISTS (SELECT 1 FROM scheduled_debt_payments WHERE debt_id = debts.id)
            """
        ).fetchall():
            count = 1 if debt["payment_mode"] == "single" else max(1, int(debt["installment_count"]))
            amounts = _scheduled_amounts(int(debt["balance_cents"]), count, int(debt["min_payment_cents"]))
            first_due = date.fromisoformat(debt["due_on"])
            connection.executemany(
                "INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                [
                    (debt["id"], _add_months(first_due, index).isoformat(), amount, now)
                    for index, amount in enumerate(amounts)
                ],
            )


def _configure_logging(app: Flask) -> None:
    logs_dir = Path(app.config["LOGS_DIR"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs_dir / "app.log", maxBytes=1_048_576, backupCount=3)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    app.logger.addHandler(handler)
    app.logger.setLevel(getattr(logging, app.config["LOG_LEVEL"].upper(), logging.INFO))


def _build_auth_user() -> dict | None:
    username = os.getenv("AUTH_USERNAME", "admin").strip().lower()
    password_hash = os.getenv("AUTH_PASSWORD_HASH", "").strip()
    if not username or not password_hash:
        return None
    return {"username": username, "display_name": username, "password_hash": password_hash}


def _summary(connection: sqlite3.Connection, month: str) -> dict:
    start_on, end_on = _month_bounds(month)
    month_totals = connection.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN kind = 'income' THEN amount_cents END), 0) AS income,
            COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount_cents END), 0) AS expense
        FROM movements
        WHERE occurred_on BETWEEN ? AND ?
        """,
        (start_on, end_on),
    ).fetchone()
    all_time = connection.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN kind = 'income' THEN amount_cents ELSE -amount_cents END), 0) AS balance
        FROM movements
        """
    ).fetchone()
    debt_total = connection.execute(
        "SELECT COALESCE(SUM(balance_cents), 0) AS total FROM debts WHERE deleted_at = ''"
    ).fetchone()
    income = int(month_totals["income"] or 0)
    expense = int(month_totals["expense"] or 0)
    balance = (
        int(all_time["balance"] or 0)
        + _setting_int(connection, "balance_adjustment_cents", 0)
    )
    return {
        "balance": _format_money(balance),
        "balance_cents": balance,
        "income": _format_money(income),
        "expense": _format_money(expense),
        "month_result": _format_money(income - expense),
        "month_result_cents": income - expense,
        "debt": _format_money(int(debt_total["total"] or 0)),
    }


def _list_movements(
    connection: sqlite3.Connection, month: str | None, kind: str, *, unplanned_only: bool = False,
    cfdi_only: bool = False,
) -> list[dict]:
    date_clause = "AND m.occurred_on BETWEEN ? AND ?" if month else ""
    source_clause = "AND x.id IS NOT NULL" if cfdi_only else ""
    unplanned_clause = """
        AND NOT EXISTS (SELECT 1 FROM movement_allocations a WHERE a.movement_id = m.id)
        AND EXISTS (
            SELECT 1 FROM expense_categories c
            WHERE c.name = m.category AND c.group_name = 'Imprevistos'
        )
    """ if unplanned_only else ""
    params = (kind, *_month_bounds(month)) if month else (kind,)
    rows = connection.execute(
        f"""
        SELECT m.id, m.kind, m.category, m.description, m.amount_cents, m.occurred_on,
               m.debt_id, (x.id IS NOT NULL) AS is_cfdi
        FROM movements AS m
        LEFT JOIN xml_imports AS x ON x.movement_id = m.id
        WHERE m.kind = ? {date_clause} {unplanned_clause} {source_clause}
        ORDER BY m.occurred_on DESC, m.id DESC
        LIMIT 100
        """,
        params,
    ).fetchall()
    return [
        {
            **dict(row),
            "kind_label": KINDS[row["kind"]],
            "source_label": "Nómina CFDI" if row["is_cfdi"] else ("Ingreso extra" if row["kind"] == "income" else "Manual"),
            "amount": _format_money(row["amount_cents"]),
            "signed_amount": f"{'+' if row['kind'] == 'income' else '-'}{_format_money(row['amount_cents'])}",
        }
        for row in rows
    ]


def _movement_totals(
    connection: sqlite3.Connection, month: str | None, kind: str, *, unplanned_only: bool = False
) -> dict:
    date_clause = "AND m.occurred_on BETWEEN ? AND ?" if month else ""
    unplanned_clause = """
        AND NOT EXISTS (SELECT 1 FROM movement_allocations a WHERE a.movement_id = m.id)
        AND EXISTS (
            SELECT 1 FROM expense_categories c
            WHERE c.name = m.category AND c.group_name = 'Imprevistos'
        )
    """ if unplanned_only else ""
    params = (kind, *_month_bounds(month)) if month else (kind,)
    row = connection.execute(
        f"""
        SELECT COALESCE(SUM(m.amount_cents), 0) AS total,
               COALESCE(SUM(CASE WHEN x.id IS NOT NULL THEN m.amount_cents ELSE 0 END), 0) AS cfdi,
               COALESCE(SUM(CASE WHEN x.id IS NULL THEN m.amount_cents ELSE 0 END), 0) AS manual
        FROM movements AS m
        LEFT JOIN xml_imports AS x ON x.movement_id = m.id
        WHERE m.kind = ? {date_clause} {unplanned_clause}
        """,
        params,
    ).fetchone()
    return {name: _format_money(row[name]) for name in ("total", "cfdi", "manual")}


def _expense_category_rows(connection: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT id, name, amount_cents, cadence, group_name, requires_confirmation FROM expense_categories ORDER BY position, id"
        )
    ]


def _expense_category_groups(connection: sqlite3.Connection, *, unplanned_only: bool = False) -> list[dict]:
    groups = {}
    for row in _expense_category_rows(connection):
        if unplanned_only and row["group_name"] != "Imprevistos":
            continue
        group = groups.setdefault(row["group_name"], {"label": row["group_name"], "items": []})
        group["items"].append(
            {
                "value": row["name"], "amount_cents": row["amount_cents"], "cadence": row["cadence"],
                "requires_confirmation": row["requires_confirmation"],
            }
        )
    return list(groups.values())


def _expense_chart(connection: sqlite3.Connection, month: str) -> dict:
    rows = connection.execute(
        """
        SELECT category, SUM(amount_cents) AS amount_cents
        FROM movements
        WHERE kind = 'expense' AND occurred_on BETWEEN ? AND ?
        GROUP BY category
        ORDER BY amount_cents DESC, category
        """,
        _month_bounds(month),
    ).fetchall()
    total = sum(row["amount_cents"] for row in rows)
    segments = []
    stops = []
    cumulative = 0.0
    for index, row in enumerate(rows, 1):
        color = (index - 1) % 11 + 1
        start = cumulative
        cumulative += row["amount_cents"] / total * 100
        end = 100.0 if index == len(rows) else cumulative
        percentage = row["amount_cents"] / total * 100
        percent_label = f"{percentage:.1f}".rstrip("0").rstrip(".") + "%"
        stops.append(f"var(--chart-{color}) {start:.4f}% {end:.4f}%")
        segments.append(
            {
                "category": row["category"],
                "amount": _format_money(row["amount_cents"]),
                "percent": percent_label,
                "color": color,
            }
        )
    return {
        "segments": segments,
        "gradient": ", ".join(stops),
        "total": _format_money(total),
    }


def _commitment_dates(commitment, start: date, end: date) -> list[date]:
    begins = date.fromisoformat(commitment["starts_on"])
    if begins > end:
        return []
    start = max(start, begins)
    frequency = commitment["frequency"]
    if frequency == "once":
        return [begins] if start <= begins <= end else []
    if frequency == "weekdays":
        return [start + timedelta(days=offset) for offset in range((end - start).days + 1) if (start + timedelta(days=offset)).weekday() < 5]
    if frequency == "weekly":
        weekday = commitment["weekday"] if commitment["weekday"] is not None else begins.weekday()
        first = start + timedelta(days=(int(weekday) - start.weekday()) % 7)
        return [first + timedelta(days=offset) for offset in range(0, (end - first).days + 1, 7)] if first <= end else []
    dates = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        occurrence = _month_date(cursor.year, cursor.month, int(commitment["month_day"] or begins.day))
        if start <= occurrence <= end and occurrence >= begins:
            dates.append(occurrence)
        cursor = _add_months(cursor, 1)
    return dates


def _commitment_occurrences(connection: sqlite3.Connection, start: date, end: date) -> list[dict]:
    if end < start:
        return []
    commitments = connection.execute(
        """
        SELECT DISTINCT c.*
        FROM recurring_commitments AS c
        LEFT JOIN commitment_exceptions AS e ON e.commitment_id = c.id
        WHERE c.is_active = 1 AND c.deleted_at = '' AND (c.starts_on <= ? OR e.moved_to BETWEEN ? AND ?)
        ORDER BY c.id
        """,
        (end.isoformat(), start.isoformat(), end.isoformat()),
    ).fetchall()
    exception_rows = connection.execute(
        """
        SELECT * FROM commitment_exceptions
        WHERE occurrence_on BETWEEN ? AND ? OR moved_to BETWEEN ? AND ?
        """,
        (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
    ).fetchall()
    exceptions = {(row["commitment_id"], row["occurrence_on"]): row for row in exception_rows}
    allocations = {
        (row["recurring_commitment_id"], row["occurrence_on"]): int(row["paid"])
        for row in connection.execute(
            """
            SELECT recurring_commitment_id, occurrence_on, SUM(amount_cents) AS paid
            FROM movement_allocations
            WHERE recurring_commitment_id IS NOT NULL
            GROUP BY recurring_commitment_id, occurrence_on
            """
        )
    }
    occurrences = []
    for commitment in commitments:
        source_dates = _commitment_dates(commitment, start, end)
        source_dates.extend(
            date.fromisoformat(row["occurrence_on"])
            for row in exception_rows
            if row["commitment_id"] == commitment["id"]
            and row["moved_to"]
            and start.isoformat() <= row["moved_to"] <= end.isoformat()
            and date.fromisoformat(row["occurrence_on"]) not in source_dates
        )
        for occurrence in source_dates:
            source_on = occurrence.isoformat()
            exception = exceptions.get((commitment["id"], source_on))
            occurrence_on = exception["moved_to"] if exception and exception["moved_to"] else source_on
            if not start.isoformat() <= occurrence_on <= end.isoformat():
                continue
            omitted = bool(exception and exception["is_omitted"])
            amount = int(exception["amount_cents"] if exception and exception["amount_cents"] else commitment["amount_cents"])
            paid = allocations.get((commitment["id"], source_on), 0)
            remaining = 0 if omitted else max(0, amount - paid)
            occurrences.append(
                {
                    **dict(commitment),
                    "commitment_id": commitment["id"],
                    "occurrence_on": occurrence_on,
                    "source_occurrence_on": source_on,
                    "amount_cents": amount,
                    "amount": _format_money(amount),
                    "paid_cents": paid,
                    "remaining_cents": remaining,
                    "remaining": _format_money(remaining),
                    "is_omitted": omitted,
                }
            )
    return occurrences


def _record_due_automatic_expenses(connection: sqlite3.Connection, today: date) -> int:
    first = connection.execute(
        "SELECT MIN(starts_on) FROM recurring_commitments WHERE is_active = 1 AND auto_record = 1"
    ).fetchone()[0]
    if not first:
        return 0
    recorded = 0
    for occurrence in sorted(
        _commitment_occurrences(connection, min(date.fromisoformat(first), today), today),
        key=lambda item: (item["occurrence_on"], item["commitment_id"]),
    ):
        if not occurrence["auto_record"] or not occurrence["remaining_cents"]:
            continue
        movement = connection.execute(
            """
            INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at)
            VALUES ('expense', ?, ?, ?, ?, ?)
            """,
            (
                occurrence["category"], occurrence["title"], occurrence["remaining_cents"],
                occurrence["occurrence_on"], _now(),
            ),
        )
        connection.execute(
            """
            INSERT INTO movement_allocations
                (movement_id, recurring_commitment_id, occurrence_on, amount_cents, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                movement.lastrowid, occurrence["commitment_id"], occurrence["source_occurrence_on"],
                occurrence["remaining_cents"], _now(),
            ),
        )
        recorded += 1
    return recorded


def _debt_installments(connection: sqlite3.Connection, start: date | None = None, end: date | None = None) -> list[dict]:
    clauses, params = ["d.deleted_at = ''"], []
    if start:
        clauses.append("p.due_on >= ?")
        params.append(start.isoformat())
    if end:
        clauses.append("p.due_on <= ?")
        params.append(end.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [
        {
            **dict(row),
            "paid_cents": int(row["paid_cents"] or 0),
            "remaining_cents": max(0, int(row["amount_cents"]) - int(row["paid_cents"] or 0)),
        }
        for row in connection.execute(
            f"""
            SELECT p.id, p.debt_id, p.due_on, p.amount_cents, d.name, d.creditor, d.priority,
                   d.creditor_type, d.original_cents, d.balance_cents, d.reminder_days,
                   d.schedule_type, d.notes,
                   COALESCE(SUM(a.amount_cents), 0) AS paid_cents
            FROM scheduled_debt_payments AS p
            JOIN debts AS d ON d.id = p.debt_id
            LEFT JOIN movement_allocations AS a ON a.scheduled_debt_payment_id = p.id
            {where}
            GROUP BY p.id
            ORDER BY p.due_on, p.id
            """,
            params,
        ).fetchall()
    ]


def _sync_debt_due_on(connection: sqlite3.Connection, debt_id: int) -> None:
    next_due = connection.execute(
        """
        SELECT p.due_on
        FROM scheduled_debt_payments AS p
        LEFT JOIN movement_allocations AS a ON a.scheduled_debt_payment_id = p.id
        WHERE p.debt_id = ?
        GROUP BY p.id
        HAVING COALESCE(SUM(a.amount_cents), 0) < p.amount_cents
        ORDER BY p.due_on, p.id LIMIT 1
        """,
        (debt_id,),
    ).fetchone()
    connection.execute(
        "UPDATE debts SET due_on = ?, reminded_due_on = '' WHERE id = ? AND balance_cents > 0",
        (next_due["due_on"] if next_due else "", debt_id),
    )


def _trim_future_installments_to_balance(connection: sqlite3.Connection, debt_id: int, balance: int) -> None:
    installments = [
        item for item in _debt_installments(connection)
        if item["debt_id"] == debt_id and item["remaining_cents"]
    ]
    reduce = sum(item["remaining_cents"] for item in installments) - balance
    for installment in reversed(installments):
        if reduce <= 0:
            break
        paid = int(installment["paid_cents"])
        remaining = int(installment["remaining_cents"])
        if reduce >= remaining:
            if paid:
                connection.execute(
                    "UPDATE scheduled_debt_payments SET amount_cents = ? WHERE id = ?",
                    (paid, installment["id"]),
                )
            else:
                connection.execute("DELETE FROM scheduled_debt_payments WHERE id = ?", (installment["id"],))
            reduce -= remaining
        else:
            connection.execute(
                "UPDATE scheduled_debt_payments SET amount_cents = amount_cents - ? WHERE id = ?",
                (reduce, installment["id"]),
            )
            reduce = 0


def _apply_debt_payment(
    connection: sqlite3.Connection, debt_id: int, amount: int, occurred_on: str,
    selected_ids: list[int] | None = None, capital_payment: bool = False,
) -> bool:
    debt = connection.execute(
        "SELECT id, balance_cents, schedule_type FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,)
    ).fetchone()
    if not debt:
        raise ValueError("Deuda no encontrada.")
    if not debt["balance_cents"]:
        raise ValueError("La deuda ya esta liquidada.")
    if amount > debt["balance_cents"]:
        raise ValueError("El abono no puede superar el saldo pendiente.")
    if amount <= 0:
        raise ValueError("El abono debe ser mayor a cero.")
    if capital_payment and debt["schedule_type"] == "single":
        raise ValueError("El abono a capital aplica solo a deudas con abonos.")

    installments = [
        item for item in _debt_installments(connection)
        if item["debt_id"] == debt_id and item["remaining_cents"]
    ]
    selected_ids = selected_ids or []
    if selected_ids:
        selected = [item for item in installments if item["id"] in selected_ids]
        if len(selected) != len(set(selected_ids)):
            raise ValueError("Una cuota seleccionada no pertenece a esta deuda o ya esta pagada.")
        selected.extend(item for item in installments if item["id"] not in selected_ids)
    else:
        selected = installments

    movement = connection.execute(
        """
        INSERT INTO movements
            (kind, category, description, amount_cents, occurred_on, debt_id, created_at)
        VALUES ('expense', 'Deudas', 'Pago de deuda', ?, ?, ?, ?)
        """,
        (amount, occurred_on, debt_id, _now()),
    )
    settled = amount == debt["balance_cents"]
    if debt["schedule_type"] == "single":
        connection.execute("UPDATE debts SET todo_amount_cents = CASE WHEN todo_amount_cents = 0 THEN original_cents ELSE todo_amount_cents END, todo_due_on = CASE WHEN todo_due_on = '' THEN due_on ELSE todo_due_on END, todo_completed = ? WHERE id = ?", (int(settled), debt_id))
    if capital_payment:
        connection.execute(
            "UPDATE debts SET balance_cents = balance_cents - ?, reminded_due_on = '' WHERE id = ?",
            (amount, debt_id),
        )
        _trim_future_installments_to_balance(connection, debt_id, debt["balance_cents"] - amount)
        _sync_debt_due_on(connection, debt_id)
        if settled:
            connection.execute("UPDATE debts SET due_on = '' WHERE id = ?", (debt_id,))
        return settled

    available = amount
    allocations = []
    for installment in selected:
        allocated = min(available, installment["remaining_cents"])
        if allocated:
            allocations.append((movement.lastrowid, installment["id"], allocated, _now()))
            available -= allocated
        if not available:
            break
    connection.executemany(
        """
        INSERT INTO movement_allocations
            (movement_id, scheduled_debt_payment_id, amount_cents, created_at)
        VALUES (?, ?, ?, ?)
        """,
        allocations,
    )
    connection.execute(
        "UPDATE debts SET balance_cents = balance_cents - ?, reminded_due_on = '' WHERE id = ?",
        (amount, debt_id),
    )
    _trim_future_installments_to_balance(connection, debt_id, debt["balance_cents"] - amount)
    _sync_debt_due_on(connection, debt_id)
    if settled:
        connection.execute("UPDATE debts SET due_on = '' WHERE id = ?", (debt_id,))
    return settled


def _calendar_debt_group_key(payment: dict) -> str:
    if payment.get("kind") != "debt" or payment.get("creditor_type") != "family":
        return ""
    return str(payment.get("creditor") or "").split(maxsplit=1)[0].casefold()


def _setting_int(connection: sqlite3.Connection, key: str, default: int) -> int:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    try:
        return int(row["value"]) if row else default
    except ValueError:
        return default


def _financial_snapshot(connection: sqlite3.Connection, today: date) -> dict:
    month = today.strftime("%Y-%m")
    month_start = date(today.year, today.month, 1)
    month_end = date.fromisoformat(_month_bounds(month)[1])
    movements = connection.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN kind = 'income' THEN amount_cents ELSE -amount_cents END), 0) AS balance,
               COALESCE(SUM(CASE WHEN kind = 'income' AND occurred_on BETWEEN ? AND ? THEN amount_cents ELSE 0 END), 0) AS income,
               COALESCE(SUM(CASE WHEN kind = 'expense' AND occurred_on BETWEEN ? AND ? THEN amount_cents ELSE 0 END), 0) AS expense
        FROM movements
        """,
        (month_start.isoformat(), month_end.isoformat(), month_start.isoformat(), month_end.isoformat()),
    ).fetchone()
    first_commitment = connection.execute(
        "SELECT MIN(starts_on) FROM recurring_commitments WHERE is_active = 1 AND starts_on <= ?",
        (month_end.isoformat(),),
    ).fetchone()[0]
    commitment_start = date.fromisoformat(first_commitment) if first_commitment else today
    pending_commitments = sum(
        item["remaining_cents"] for item in _commitment_occurrences(connection, commitment_start, month_end)
        if item["occurrence_on"] <= month_end.isoformat()
    )
    installment_rows = [row for row in _debt_installments(connection, None, month_end) if row["remaining_cents"]]
    debt_rows = connection.execute("SELECT id, balance_cents, priority FROM debts WHERE deleted_at = ''").fetchall()
    balances = {row["id"]: int(row["balance_cents"]) for row in debt_rows}
    scheduled_by_debt: dict[int, int] = {}
    priority_by_debt = {}
    for row in installment_rows:
        scheduled_by_debt[row["debt_id"]] = scheduled_by_debt.get(row["debt_id"], 0) + row["remaining_cents"]
        priority_by_debt[row["debt_id"]] = row["priority"]
    urgent_due = sum(min(total, balances.get(debt_id, 0)) for debt_id, total in scheduled_by_debt.items() if priority_by_debt[debt_id] == "urgent")
    nonurgent_due = sum(min(total, balances.get(debt_id, 0)) for debt_id, total in scheduled_by_debt.items() if priority_by_debt[debt_id] != "urgent")
    urgent_paid = connection.execute(
        """
        SELECT COALESCE(SUM(m.amount_cents), 0)
        FROM movements AS m JOIN debts AS d ON d.id = m.debt_id
        WHERE d.priority = 'urgent' AND m.occurred_on BETWEEN ? AND ?
        """,
        (month_start.isoformat(), month_end.isoformat()),
    ).fetchone()[0]
    urgent_target = _setting_int(connection, "urgent_debt_target_cents", 500_000)
    urgent_remaining = max(0, urgent_target - int(urgent_paid))
    urgent_balance = sum(int(row["balance_cents"]) for row in debt_rows if row["priority"] == "urgent")
    urgent_protected = min(urgent_balance, max(urgent_remaining, urgent_due))
    debt_total = sum(balances.values())
    committed = pending_commitments + debt_total
    balance = (
        int(movements["balance"])
        + _setting_int(connection, "balance_adjustment_cents", 0)
    )
    free = balance - committed
    return {
        "balance_cents": balance, "balance": _format_money(balance),
        "pending_commitments_cents": pending_commitments, "pending_commitments": _format_money(pending_commitments),
        "pending_debts_cents": debt_total, "pending_debts": _format_money(debt_total),
        "committed_cents": committed, "committed": _format_money(committed),
        "urgent_target_cents": urgent_target, "urgent_target": _format_money(urgent_target),
        "urgent_protected_cents": urgent_protected, "urgent_protected": _format_money(urgent_protected),
        "free_cents": free, "free": _format_money(free),
        "income_cents": int(movements["income"]),
        "income": _format_money(int(movements["income"])),
        "expense_cents": int(movements["expense"]), "expense": _format_money(movements["expense"]),
    }


def _list_debts(connection: sqlite3.Connection, today: date | None = None) -> list[dict]:
    today = today or date.today()
    rows = connection.execute(
        """
        SELECT id, name, creditor, creditor_type, original_cents, balance_cents, due_on,
               min_payment_cents, payment_mode, installment_count, reminder_days,
               reminded_due_on, notes, created_at, priority, schedule_type
        FROM debts
        WHERE deleted_at = ''
        ORDER BY (balance_cents = 0), CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                 (due_on = ''), due_on, id DESC
        """
    ).fetchall()
    payments_by_debt: dict[int, list[dict]] = {}
    for payment in connection.execute(
        """
        SELECT id, debt_id, amount_cents, occurred_on
        FROM movements
        WHERE debt_id IS NOT NULL
        ORDER BY occurred_on DESC, id DESC
        """
    ):
        payments_by_debt.setdefault(payment["debt_id"], []).append(
            {
                **dict(payment),
                "amount": _format_money(payment["amount_cents"]),
            }
        )
    installments_by_debt: dict[int, list[dict]] = {}
    for installment in _debt_installments(connection):
        due = date.fromisoformat(installment["due_on"])
        remaining = installment["remaining_cents"]
        state = "paid" if not remaining else "overdue" if due < today else "today" if due == today else "upcoming"
        installments_by_debt.setdefault(installment["debt_id"], []).append(
            {
                **installment, "amount": _format_money(installment["amount_cents"]),
                "remaining": _format_money(remaining), "state": state,
                "state_label": {"paid": "Pagada", "overdue": "Vencida", "today": "Vence hoy", "upcoming": "Próxima"}[state],
            }
        )
    debts = []
    for row in rows:
        balance = int(row["balance_cents"])
        original = int(row["original_cents"])
        payments = payments_by_debt.get(row["id"], [])
        installments = installments_by_debt.get(row["id"], [])
        pending_installments = [item for item in installments if item["remaining_cents"]]
        next_installment = pending_installments[0] if pending_installments else None
        due_on = date.fromisoformat(next_installment["due_on"]) if next_installment else None
        paid_cents = sum(payment["amount_cents"] for payment in payments)
        paid_percent = min(100, round(paid_cents / (paid_cents + balance) * 100)) if paid_cents + balance else 0
        if not balance:
            timing, timing_label = "paid", "Liquidada"
        elif not due_on:
            timing, timing_label = "active", "Calendario pendiente"
        elif due_on < today:
            timing, timing_label = "overdue", f"Vencida hace {(today - due_on).days} dias"
        elif due_on <= today + timedelta(days=7):
            timing, timing_label = "soon", f"Vence en {(due_on - today).days} dias"
        else:
            timing, timing_label = "active", "Al corriente"
        debts.append(
            {
                **dict(row),
                "original": _format_money(original),
                "balance": _format_money(balance),
                "creditor_type_label": CREDITOR_TYPES.get(row["creditor_type"], "Amigo/a"),
                "priority_label": PRIORITIES.get(row["priority"], "Normal"),
                "min_payment": _format_money(row["min_payment_cents"]),
                "plan_label": SCHEDULE_TYPES.get(row["schedule_type"], "Un pago"),
                "due_on": next_installment["due_on"] if next_installment else "",
                "installments": installments,
                "pending_installments": pending_installments,
                "payments": payments,
                "payment_count": len(payments),
                "paid_cents": paid_cents,
                "paid": _format_money(paid_cents),
                "adjustments": [
                    {**dict(adjustment), "previous": _format_money(adjustment["previous_cents"]),
                     "balance": _format_money(adjustment["balance_cents"])}
                    for adjustment in connection.execute(
                        "SELECT * FROM debt_balance_adjustments WHERE debt_id = ? ORDER BY id DESC", (row["id"],)
                    )
                ],
                "suggested_payment_cents": balance if row["schedule_type"] == "single" else min(balance, next_installment["remaining_cents"] if next_installment else balance),
                "created_on": str(row["created_at"])[:10],
                "paid_percent": paid_percent,
                "timing": timing,
                "timing_label": timing_label,
            }
        )
    return debts


def _list_recurring_debts(connection: sqlite3.Connection) -> list[dict]:
    rows = [
        dict(row) for row in connection.execute(
            "SELECT * FROM recurring_commitments WHERE deleted_at = '' ORDER BY is_active DESC, title, id"
        )
    ]
    payments_by_debt: dict[int, list[dict]] = {}
    for payment in connection.execute(
        """
        SELECT a.recurring_commitment_id,
               COALESCE(NULLIF(e.moved_to, ''), a.occurrence_on) AS occurrence_on,
               a.amount_cents, m.occurred_on, m.id AS movement_id
        FROM movement_allocations AS a
        JOIN movements AS m ON m.id = a.movement_id
        LEFT JOIN commitment_exceptions AS e
          ON e.commitment_id = a.recurring_commitment_id
         AND e.occurrence_on = a.occurrence_on
        WHERE a.recurring_commitment_id IS NOT NULL
        ORDER BY m.occurred_on DESC, m.id DESC, a.id DESC
        """
    ):
        payments_by_debt.setdefault(payment["recurring_commitment_id"], []).append(
            {**dict(payment), "amount": _format_money(payment["amount_cents"])}
        )

    debts, grouped_ids = [], set()
    for item in rows:
        if item["id"] in grouped_ids:
            continue
        pair = next(
            (
                candidate for candidate in rows
                if candidate["id"] not in grouped_ids and candidate["id"] != item["id"]
                and item["frequency"] == candidate["frequency"] == "monthly"
                and {item["month_day"], candidate["month_day"]} in ({15, 30}, {15, 31})
                and all(item[key] == candidate[key] for key in ("title", "amount_cents", "category", "notes", "is_active"))
            ),
            None,
        )
        grouped_ids.add(item["id"])
        if pair:
            grouped_ids.add(pair["id"])
            schedule_label = "Días 15 y 30 de cada mes"
        elif item["frequency"] == "monthly":
            schedule_label = f"Día {item['month_day']} de cada mes"
        elif item["frequency"] == "weekly":
            schedule_label = (
                "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábados", "Domingos",
            )[int(item["weekday"])]
        elif item["frequency"] == "weekdays":
            schedule_label = "Lunes a viernes"
        else:
            schedule_label = item["starts_on"]
        member_ids = (item["id"], pair["id"]) if pair else (item["id"],)
        payments = sorted(
            (payment for debt_id in member_ids for payment in payments_by_debt.get(debt_id, [])),
            key=lambda payment: (payment["occurred_on"], payment["movement_id"]),
            reverse=True,
        )
        debts.append(
            {
                **item,
                "pair_id": pair["id"] if pair else 0,
                "frequency_label": "Quincenal" if pair else COMMITMENT_FREQUENCIES.get(item["frequency"], item["frequency"]),
                "schedule_label": schedule_label,
                "amount": _format_money(item["amount_cents"]),
                "payments": payments,
                "remaining_cents": max(0, item["amount_cents"] - sum(payment["amount_cents"] for payment in payments)),
                "remaining": _format_money(max(0, item["amount_cents"] - sum(payment["amount_cents"] for payment in payments))),
            }
        )
    return debts


def _barbershop_blockers(connection: sqlite3.Connection) -> dict:
    row = connection.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN schedule_type = 'single' THEN balance_cents ELSE 0 END), 0) AS single_cents,
            COALESCE(SUM(CASE WHEN schedule_type != 'single' THEN balance_cents ELSE 0 END), 0) AS installment_cents
        FROM debts
        WHERE balance_cents > 0 AND deleted_at = ''
        """
    ).fetchone()
    single = int(row["single_cents"] or 0)
    installment = int(row["installment_cents"] or 0)
    return {
        "single_cents": single,
        "installment_cents": installment,
        "total_cents": single + installment,
        "single": _format_money(single),
        "installment": _format_money(installment),
        "total": _format_money(single + installment),
        "blocked": bool(single or installment),
    }


def _barbershop_goal(connection: sqlite3.Connection) -> dict:
    placeholders = ", ".join("?" for _ in BARBERSHOP_GOAL_TITLES)
    goal = connection.execute(
        f"""
        SELECT * FROM goals
        WHERE title IN ({placeholders})
        ORDER BY CASE title WHEN ? THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (*BARBERSHOP_GOAL_TITLES, BARBERSHOP_GOAL_TITLE),
    ).fetchone()
    suggested_cents = BARBERSHOP_GOAL_TARGET_CENTS
    target = int(goal["target_cents"] or 0) if goal else suggested_cents
    saved = int(goal["saved_cents"] or 0) if goal else 0
    target = target or suggested_cents
    return {
        **(dict(goal) if goal else {"id": None, "title": BARBERSHOP_GOAL_TITLE, "notes": ""}),
        "title": BARBERSHOP_GOAL_TITLE,
        "target_on": (goal["target_on"] if goal else "") or BARBERSHOP_GOAL_TARGET_ON,
        "target_cents": target,
        "suggested_cents": suggested_cents,
        "suggested": _format_money(suggested_cents),
        "saved_cents": saved,
        "remaining_cents": max(0, target - saved),
        "target": _format_money(target),
        "saved": _format_money(saved),
        "remaining": _format_money(max(0, target - saved)),
        "saved_percent": min(100, round(saved / target * 100)) if target else 0,
    }


def _commitment_form_data(form, today: date) -> dict:
    title = _clean_text(form.get("title"), "la deuda", limit=120)
    amount = _money_to_cents(form.get("amount"))
    frequency = str(form.get("frequency") or "monthly").strip()
    if frequency not in COMMITMENT_FREQUENCIES:
        raise ValueError("Selecciona una frecuencia valida.")
    starts_on = _parse_date(form.get("starts_on") or today, "la deuda")
    begins = date.fromisoformat(starts_on)
    weekday = begins.weekday()
    month_day = begins.day
    if frequency == "weekly":
        try:
            weekday = int(form.get("weekday") if str(form.get("weekday") or "").strip() else begins.weekday())
        except ValueError:
            raise ValueError("Selecciona un dia semanal valido.") from None
        if weekday not in range(7):
            raise ValueError("Selecciona un dia semanal valido.")
    if frequency == "monthly":
        try:
            month_day = int(form.get("month_day") or begins.day)
        except ValueError:
            raise ValueError("Captura un dia mensual valido.") from None
        if not 1 <= month_day <= 31:
            raise ValueError("El dia mensual debe estar entre 1 y 31.")
    category = _clean_text(form.get("category"), "la categoria", limit=60)
    return {
        "title": title, "amount_cents": amount, "frequency": frequency, "starts_on": starts_on,
        "weekday": weekday if frequency == "weekly" else None,
        "month_day": month_day if frequency == "monthly" else None,
        "category": category,
        "notes": _clean_text(form.get("notes"), "las notas", required=False, limit=500),
    }


def _update_commitment_calendar(connection, commitment_id, pair_id, item, now):
    current = connection.execute("SELECT * FROM recurring_commitments WHERE id = ? AND deleted_at = ''", (commitment_id,)).fetchone()
    if not current:
        raise ValueError("Deuda no encontrada.")
    entries = [{**item, "frequency": "monthly", "month_day": day} for day in (15, 30)] if item["frequency"] == "fortnightly" else [item]
    for index, entry in enumerate(entries):
        target_id = commitment_id if index == 0 else pair_id
        if target_id:
            connection.execute(
                "UPDATE recurring_commitments SET title = ?, amount_cents = ?, frequency = ?, starts_on = ?, weekday = ?, month_day = ?, category = ?, notes = ? WHERE id = ? AND deleted_at = ''",
                (*entry.values(), target_id),
            )
        else:
            connection.execute(
                "INSERT INTO recurring_commitments (title, amount_cents, frequency, starts_on, weekday, month_day, category, notes, created_at, is_active, auto_record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*entry.values(), now, current["is_active"], current["auto_record"]),
            )
    if pair_id and len(entries) == 1:
        connection.execute("UPDATE recurring_commitments SET is_active = 0, deleted_at = ? WHERE id = ?", (now, pair_id))


def _is_fortnightly_pair(connection: sqlite3.Connection, first_id: int, second_id: int) -> bool:
    rows = connection.execute(
        "SELECT title, amount_cents, frequency, month_day, category, notes FROM recurring_commitments WHERE id IN (?, ?) AND deleted_at = ''",
        (first_id, second_id),
    ).fetchall()
    return (
        len(rows) == 2
        and all(row["frequency"] == "monthly" for row in rows)
        and {row["month_day"] for row in rows} in ({15, 30}, {15, 31})
        and all(rows[0][key] == rows[1][key] for key in ("title", "amount_cents", "category", "notes"))
    )


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        PROJECT_NAME=os.getenv("PROJECT_NAME", "Agenda financiera"),
        PROJECT_TAGLINE=os.getenv("PROJECT_TAGLINE", "Tus ingresos, gastos y deudas en un solo lugar"),
        PORT=int(os.getenv("PORT", "5028")),
        AUTH_MODE=os.getenv("AUTH_MODE", "shared_login").strip().lower(),
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_NAME=os.getenv("SESSION_COOKIE_NAME", "session_finanzas"),
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE", False),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
        DATABASE_PATH=_database_path(),
        LOGS_DIR=Path(os.getenv("LOGS_DIR", ROOT_DIR / "logs")),
        LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO"),
        APP_TIMEZONE=os.getenv("APP_TIMEZONE", "America/Mexico_City"),
        MAX_CONTENT_LENGTH=9 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if os.getenv("FLASK_ENV") == "production":
        if len(os.getenv("SECRET_KEY", "")) < 32:
            raise RuntimeError("SECRET_KEY debe tener al menos 32 caracteres en produccion.")
        app.config["SESSION_COOKIE_SECURE"] = True
    if not app.config["TESTING"] and app.config["AUTH_MODE"] in {"public", "none", "off"}:
        app.config["AUTH_MODE"] = "shared_login"

    _configure_logging(app)
    _init_db(app)
    app.config["AUTH_USER"] = _build_auth_user()

    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def is_authenticated() -> bool:
        return bool(app.config["AUTH_USER"] and session.get("auth_user") == app.config["AUTH_USER"]["username"])

    @app.context_processor
    def inject_context() -> dict:
        return {
            "csrf_token": csrf_token,
            "project_name": app.config["PROJECT_NAME"],
            "project_tagline": app.config["PROJECT_TAGLINE"],
            "asset_version": ASSET_VERSION,
            "current_user_display": app.config["AUTH_USER"]["display_name"] if is_authenticated() else "",
            "today": _local_today(app.config["APP_TIMEZONE"]).isoformat(),
            "money": _format_money,
            "priorities": PRIORITIES,
            "schedule_types": SCHEDULE_TYPES,
            "fund_types": FUND_TYPES,
            "commitment_frequencies": COMMITMENT_FREQUENCIES,
        }

    @app.before_request
    def protect_requests():
        if request.method == "POST":
            submitted = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(submitted, expected):
                return "Solicitud invalida o expirada.", 400
        if app.config["AUTH_MODE"] in {"public", "none", "off"}:
            return None
        if request.endpoint in {"login", "health", "static", "web_manifest", "service_worker"} or request.path.startswith("/static/"):
            return None
        if is_authenticated():
            return None
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if is_authenticated():
            return redirect(url_for("index"))
        error = ""
        next_url = str(request.values.get("next") or "").strip()
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = ""
        if request.method == "POST":
            username = str(request.form.get("username") or "").strip().lower()
            user = app.config["AUTH_USER"]
            password_valid = bool(user) and check_password_hash(
                user["password_hash"], request.form.get("password", "")
            )
            if not user or username != user["username"] or not password_valid:
                error = "Usuario o contrasena incorrectos."
            else:
                session.clear()
                session.permanent = True
                session["auth_user"] = username
                return redirect(next_url or url_for("index"))
        return render_template(
            "login.html",
            error=error,
            next_url=next_url,
            login_username=os.getenv("AUTH_USERNAME", "admin"),
            catalog_ready=bool(app.config["AUTH_USER"]),
        )

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        current = _local_today(app.config["APP_TIMEZONE"])
        selected_month = _selected_month(request.args.get("month") or current.strftime("%Y-%m"))
        from minimal_debts import view
        import sys
        with _db_connection(app) as connection:
            data = view(connection, sys.modules[__name__], current, selected_month)
        return render_template("minimal_home.html", active_page="home", **data)

    @app.get("/deudas")
    def debts_page():
        current = _local_today(app.config["APP_TIMEZONE"])
        from minimal_debts import view
        import sys
        with _db_connection(app) as connection:
            data = view(connection, sys.modules[__name__], current, current.strftime("%Y-%m"))
        return render_template("minimal_debts.html", active_page="debts", **data)

    @app.get("/ahorros")
    def goals_page():
        return redirect(url_for("settings_page"))

    @app.get("/objetivo")
    def objective_page():
        current = _local_today(app.config["APP_TIMEZONE"])
        with _db_connection(app) as connection:
            goal = _barbershop_goal(connection)
            blockers = _barbershop_blockers(connection)
            active_debts = [debt for debt in _list_debts(connection, current) if debt["balance_cents"] > 0]
        return render_template(
            "objective.html",
            active_page="objective",
            goal=goal,
            blockers=blockers,
            active_debts=active_debts,
            default_target=f"{goal['suggested_cents'] / 100:.2f}",
        )

    @app.get("/objetivos")
    def legacy_goals_page():
        return redirect(url_for("objective_page"))

    @app.get("/configuracion")
    def settings_page():
        with _db_connection(app) as connection:
            gmail = _reminder_settings(connection, app)
            xml_gmail = _xml_gmail_settings(connection, app)
        return render_template(
            "settings.html",
            active_page="settings",
            gmail_email=gmail["email"],
            gmail_configured=bool(gmail["email"] and gmail["password"]),
            reminders_enabled=gmail["enabled"],
            xml_imports_enabled=xml_gmail["enabled"],
        )

    @app.get("/descargas")
    def downloads_page():
        return render_template("downloads.html", active_page="downloads")

    @app.get("/calendario")
    def calendar_page():
        return redirect(url_for("index", month=request.args.get("month")))

    @app.get("/movimientos")
    def movements_page():
        return redirect(url_for("expenses_page", month=request.args.get("month")))

    @app.get("/ingresos")
    def income_page():
        with _db_connection(app) as connection:
            return render_template(
                "income.html", active_page="income",
                movements=_list_movements(connection, None, "income", cfdi_only=True),
                totals=_movement_totals(connection, None, "income"),
            )

    @app.get("/gastos")
    def expenses_page():
        current = _local_today(app.config["APP_TIMEZONE"])
        selected_month = _selected_month(request.args.get("month") or current.strftime("%Y-%m"))
        year, month_number = map(int, selected_month.split("-"))
        with _db_connection(app) as connection:
            start_on, end_on = _month_bounds(selected_month)
            count = connection.execute(
                """
                SELECT COUNT(*) FROM movements AS m
                WHERE kind = 'expense' AND occurred_on BETWEEN ? AND ?
                  AND NOT EXISTS (SELECT 1 FROM movement_allocations a WHERE a.movement_id = m.id)
                  AND EXISTS (
                      SELECT 1 FROM expense_categories c
                      WHERE c.name = m.category AND c.group_name = 'Imprevistos'
                  )
                """,
                (start_on, end_on),
            ).fetchone()[0]
            return render_template(
                "expense.html", active_page="expense", expense_groups=_expense_category_groups(connection, unplanned_only=True),
                movements=_list_movements(connection, selected_month, "expense", unplanned_only=True),
                total=_movement_totals(connection, None, "expense", unplanned_only=True)["total"],
                month_total=_movement_totals(connection, selected_month, "expense", unplanned_only=True)["total"],
                movement_count=count,
                selected_month=selected_month,
                selected_month_label=f"{MONTH_NAMES[month_number]} {year}",
                history_open=bool(request.args.get("month")),
            )

    @app.get("/resumen")
    def summary_page():
        month = _selected_month(request.args.get("month"))
        with _db_connection(app) as connection:
            snapshot = _financial_snapshot(connection, _local_today(app.config["APP_TIMEZONE"]))
            return render_template(
                "summary.html",
                active_page="summary",
                selected_month=month,
                summary=_summary(connection, month),
                expense_chart=_expense_chart(connection, month),
                snapshot=snapshot,
            )

    @app.post("/movimientos")
    def create_movement():
        try:
            kind = str(request.form.get("kind") or "").strip()
            if kind not in KINDS:
                raise ValueError("Selecciona ingreso o gasto.")
            category = "Otro ingreso" if kind == "income" else _clean_text(request.form.get("category"), "una categoria", limit=60)
            description = _clean_text(
                request.form.get("description"), "un concepto",
                required=kind == "income" or category == "Otros",
            ) or category
            amount = _money_to_cents(request.form.get("amount"))
            occurred_on = _parse_date(request.form.get("occurred_on"), "el movimiento")
            with _db_connection(app) as connection:
                if kind == "expense":
                    selected_category = connection.execute(
                        "SELECT name FROM expense_categories WHERE name = ?", (category,)
                    ).fetchone()
                    if not selected_category:
                        raise ValueError("Selecciona una categoria de gasto valida.")
                    category = selected_category["name"]
                connection.execute(
                    """
                    INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (kind, category, description, amount, occurred_on, _now()),
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Movimiento guardado.", "success")
        endpoint = "income_page" if request.form.get("kind") == "income" else "expenses_page"
        return redirect(url_for(endpoint, month=request.form.get("month") or ""))

    @app.post("/movimientos/<int:movement_id>/eliminar")
    def delete_movement(movement_id: int):
        with _db_connection(app) as connection:
            movement = connection.execute(
                """
                SELECT m.id, m.kind, m.amount_cents, m.debt_id, (x.id IS NOT NULL) AS is_cfdi
                FROM movements AS m
                LEFT JOIN xml_imports AS x ON x.movement_id = m.id
                WHERE m.id = ?
                """,
                (movement_id,),
            ).fetchone()
            if not movement:
                flash("Movimiento no encontrado.", "error")
            elif movement["is_cfdi"] or movement["debt_id"] or connection.execute(
                "SELECT 1 FROM movement_allocations WHERE movement_id = ?", (movement_id,)
            ).fetchone():
                flash("Los CFDI y pagos de deuda se conservan en el historial.", "error")
            else:
                connection.execute("DELETE FROM movements WHERE id = ?", (movement_id,))
                flash("Movimiento eliminado.", "success")
        kind = movement["kind"] if movement else request.form.get("kind")
        endpoint = "income_page" if kind == "income" else "expenses_page"
        return redirect(url_for(endpoint, month=request.form.get("month") or ""))

    @app.post("/ingresos/importar-xml")
    def import_income_xml():
        from import_gmail_xml import GMAIL_ERRORS, sync_cfdi

        try:
            with _db_connection(app) as connection:
                settings = _xml_gmail_settings(connection, app)
                if not settings["password"]:
                    raise ValueError("Configura primero la clave de aplicacion de Gmail.")
                result = sync_cfdi(connection, settings)
        except ValueError as exc:
            flash(str(exc), "error")
        except GMAIL_ERRORS as exc:
            app.logger.warning("Fallo la importacion manual de Gmail (%s).", type(exc).__name__)
            flash("No se pudo leer Gmail. Revisa la cuenta y la clave de aplicacion.", "error")
        else:
            flash(
                f"Busqueda completa: {result['imported']} nuevo(s), "
                f"{result['skipped']} duplicado(s) y {result['invalid']} invalido(s).",
                "success",
            )
        return redirect(url_for("income_page", month=request.form.get("month") or ""))

    @app.post("/deudas")
    def create_debt():
        try:
            debt_type = str(request.form.get("debt_type") or "").strip()
            if debt_type == "recurring":
                return create_commitment()
            if debt_type not in {"", "single", "installments"}:
                raise ValueError("Selecciona un tipo de deuda válido.")
            submitted = request.form.copy()
            if debt_type:
                submitted["schedule_type"] = "single" if debt_type == "single" else submitted.get("schedule_type", "monthly")
            debt = _debt_form_data(submitted)
            if debt["creditor_type"] == "bank":
                raise ValueError("Las nuevas deudas bancarias ya no están disponibles.")
            with _db_connection(app) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO debts
                        (name, creditor, creditor_type, original_cents, balance_cents, due_on, min_payment_cents,
                         payment_mode, installment_count, reminder_days, notes, created_at, priority, schedule_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        debt["name"], debt["creditor"], debt["creditor_type"],
                        debt["amount"], debt["balance"], debt["due_on"], debt["min_payment"],
                        debt["payment_mode"], debt["installment_count"], debt["reminder_days"],
                        debt["notes"], _now(), debt["priority"], debt["schedule_type"],
                    ),
                )
                if debt["schedule_type"] == "single":
                    connection.execute("UPDATE debts SET todo_amount_cents = ?, todo_due_on = ? WHERE id = ?", (debt["amount"], debt["due_on"], cursor.lastrowid))
                connection.executemany(
                    "INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                    [(cursor.lastrowid, due_on, amount, _now()) for due_on, amount in debt["schedule"]],
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda agregada a la agenda.", "success")
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("index", month=_selected_month(request.form.get("month"))))
        return redirect(url_for("debts_page"))

    @app.post("/deudas/<int:debt_id>/editar")
    def edit_debt(debt_id: int):
        if request.form.get("metadata_only") == "1":
            try:
                name = _clean_text(request.form.get("name"), "el nombre", limit=100)
                notes = str(request.form.get("notes") or "").strip()[:1000]
                with _db_connection(app) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute("SELECT schedule_type FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,)).fetchone()
                    if not current:
                        raise ValueError("Deuda no encontrada.")
                    creditor = name if current["schedule_type"] == "single" else _clean_text(
                        request.form.get("creditor"), "el acreedor", limit=100,
                    )
                    cursor = connection.execute("UPDATE debts SET name = ?, creditor = ?, notes = ? WHERE id = ?",
                                                (name, creditor, notes, debt_id))
                    if not cursor.rowcount:
                        raise ValueError("Deuda no encontrada.")
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash("Deuda actualizada.", "success")
            if request.form.get("from_calendar") == "1":
                return redirect(url_for("calendar_page" if request.form.get("calendar_view") == "calendar" else "index",
                                        month=_selected_month(request.form.get("month"))))
            return redirect(url_for("debts_page"))
        try:
            updated = _debt_form_data(request.form)
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT original_cents, balance_cents, schedule_type,
                           (SELECT COUNT(*) FROM movements WHERE debt_id = debts.id) AS payment_count,
                           (SELECT COUNT(*) FROM debt_balance_adjustments WHERE debt_id = debts.id) AS adjustment_count
                    FROM debts WHERE id = ? AND deleted_at = ''
                    """,
                    (debt_id,),
                ).fetchone()
                if not current:
                    raise ValueError("Deuda no encontrada.")
                has_history = current["payment_count"] or current["adjustment_count"]
                if has_history and updated["amount"] != current["original_cents"]:
                    raise ValueError("No puedes cambiar el monto original porque ya hay abonos registrados.")
                if has_history and updated["schedule_type"] != current["schedule_type"]:
                    raise ValueError("No puedes cambiar el calendario porque ya hay abonos registrados.")
                new_balance = current["balance_cents"] if has_history else updated["balance"]
                connection.execute(
                    """
                    UPDATE debts
                    SET name = ?, creditor = ?, creditor_type = ?, original_cents = ?,
                        balance_cents = ?, due_on = ?, min_payment_cents = ?, payment_mode = ?,
                        installment_count = ?, reminder_days = ?, notes = ?, priority = ?, schedule_type = ?,
                        reminded_due_on = CASE WHEN due_on = ? THEN reminded_due_on ELSE '' END
                    WHERE id = ?
                    """,
                    (
                        updated["name"], updated["creditor"], updated["creditor_type"],
                        updated["amount"], new_balance, updated["due_on"],
                        updated["min_payment"], updated["payment_mode"],
                        updated["installment_count"], updated["reminder_days"], updated["notes"],
                        updated["priority"], updated["schedule_type"],
                        updated["due_on"], debt_id,
                    ),
                )
                if not has_history:
                    connection.execute("DELETE FROM scheduled_debt_payments WHERE debt_id = ?", (debt_id,))
                    connection.executemany(
                        "INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                        [(debt_id, due_on, amount, _now()) for due_on, amount in updated["schedule"]],
                    )
                _sync_debt_due_on(connection, debt_id)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda actualizada.", "success")
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("index", month=_selected_month(request.form.get("month"))))
        return redirect(url_for("debts_page"))

    @app.post("/deudas/<int:debt_id>/pagar-completo")
    def pay_single_debt(debt_id: int):
        try:
            expected = int(request.form.get("expected_balance"))
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                debt = connection.execute("SELECT balance_cents, schedule_type FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,)).fetchone()
                if not debt or debt["schedule_type"] != "single":
                    raise ValueError("Deuda de un solo pago no encontrada.")
                if not debt["balance_cents"] or debt["balance_cents"] != expected:
                    raise ValueError("El monto cambió o ya fue pagado. Recarga Deudas.")
                _apply_debt_payment(connection, debt_id, debt["balance_cents"], _local_today(app.config["APP_TIMEZONE"]).isoformat(), [])
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda pagada.", "success")
        return redirect(url_for("debts_page"))

    @app.post("/deudas/<int:debt_id>/pagos")
    def pay_debt(debt_id: int):
        settled = False
        try:
            amount = _money_to_cents(request.form.get("amount"))
            occurred_on = _parse_date(request.form.get("occurred_on"), "el pago")
            if date.fromisoformat(occurred_on) > _local_today(app.config["APP_TIMEZONE"]):
                raise ValueError("La fecha del abono no puede estar en el futuro.")
            selected_ids = []
            for raw_id in request.form.getlist("installment_ids"):
                try:
                    selected_ids.append(int(raw_id))
                except ValueError:
                    raise ValueError("Selecciona cuotas validas.") from None
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                token = str(request.form.get("idempotency_token") or "")
                if token and not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
                    raise ValueError("Solicitud de abono inválida.")
                existing = connection.execute("SELECT * FROM debt_payment_requests WHERE token = ?", (token,)).fetchone() if token else None
                if existing:
                    if (existing["debt_id"], existing["amount_cents"], existing["occurred_on"]) != (debt_id, amount, occurred_on):
                        raise ValueError("La solicitud ya fue usada para otro abono. Recarga Deudas.")
                else:
                    settled = _apply_debt_payment(connection, debt_id, amount, occurred_on, selected_ids,
                                                 request.form.get("capital_payment") == "1")
                    if token:
                        connection.execute("INSERT INTO debt_payment_requests VALUES (?, ?, ?, ?)", (token, debt_id, amount, occurred_on))

        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda liquidada y conservada en el historial." if settled else "Abono registrado y saldo actualizado.", "success")
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("calendar_page" if request.form.get("calendar_view") == "calendar" else "index", month=_selected_month(request.form.get("month"))))
        return redirect(url_for("debts_page"))

    @app.post("/deudas/pagos-combinados")
    def pay_grouped_debts():
        try:
            occurred_on = _parse_date(request.form.get("occurred_on"), "el pago")
            try:
                selected_ids = list(dict.fromkeys(int(value) for value in request.form.getlist("installment_ids")))
            except ValueError:
                raise ValueError("Selecciona cuotas validas.") from None
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                pending = {
                    item["id"]: item for item in _debt_installments(connection)
                    if item["remaining_cents"]
                }
                selected = [pending[item_id] for item_id in selected_ids if item_id in pending]
                keys = {
                    _calendar_debt_group_key({"kind": "debt", **item}) for item in selected
                }
                if (
                    len(selected) < 2 or len(selected) != len(selected_ids)
                    or len({item["debt_id"] for item in selected}) != len(selected)
                    or len({item["due_on"] for item in selected}) != 1
                    or len(keys) != 1 or not next(iter(keys), "")
                ):
                    raise ValueError("Las cuotas combinadas ya no coinciden; recarga el calendario.")
                for installment in selected:
                    _apply_debt_payment(
                        connection, installment["debt_id"], installment["remaining_cents"],
                        occurred_on, [installment["id"]],
                    )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Abonos registrados en las dos deudas.", "success")
        return redirect(url_for("index", month=_selected_month(request.form.get("month"))))

    @app.post("/deudas/<int:debt_id>/cuotas")
    def save_debt_installment(debt_id: int):
        try:
            due_on = _parse_date(request.form.get("due_on"), "la cuota")
            if date.fromisoformat(due_on) <= _local_today(app.config["APP_TIMEZONE"]):
                raise ValueError("Solo puedes agregar o corregir cuotas futuras.")
            amount = _money_to_cents(request.form.get("amount"))
            installment_id = int(request.form.get("installment_id") or 0)
            with _db_connection(app) as connection:
                if not connection.execute("SELECT 1 FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,)).fetchone():
                    raise ValueError("Deuda no encontrada.")
                if installment_id:
                    installment = connection.execute(
                        """
                        SELECT p.id, p.due_on, COALESCE(SUM(a.amount_cents), 0) AS paid
                        FROM scheduled_debt_payments AS p
                        LEFT JOIN movement_allocations AS a ON a.scheduled_debt_payment_id = p.id
                        WHERE p.id = ? AND p.debt_id = ? AND EXISTS (SELECT 1 FROM debts d WHERE d.id = p.debt_id AND d.deleted_at = '') GROUP BY p.id
                        """,
                        (installment_id, debt_id),
                    ).fetchone()
                    if not installment or installment["paid"] or date.fromisoformat(installment["due_on"]) <= _local_today(app.config["APP_TIMEZONE"]):
                        raise ValueError("Solo puedes corregir cuotas futuras sin pagos.")
                    connection.execute(
                        "UPDATE scheduled_debt_payments SET due_on = ?, amount_cents = ? WHERE id = ?",
                        (due_on, amount, installment_id),
                    )
                else:
                    connection.execute(
                        "INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at) VALUES (?, ?, ?, ?)",
                        (debt_id, due_on, amount, _now()),
                    )
                _sync_debt_due_on(connection, debt_id)
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash(str(exc) if isinstance(exc, ValueError) else "Ya existe una cuota en esa fecha.", "error")
        else:
            flash("Cuota guardada.", "success")
        return redirect(url_for("debts_page"))

    @app.post("/deudas/<int:debt_id>/cuotas/<int:installment_id>/eliminar")
    def delete_debt_installment(debt_id: int, installment_id: int):
        current = _local_today(app.config["APP_TIMEZONE"])
        with _db_connection(app) as connection:
            installment = connection.execute(
                """
                SELECT p.due_on, COALESCE(SUM(a.amount_cents), 0) AS paid
                FROM scheduled_debt_payments AS p
                LEFT JOIN movement_allocations AS a ON a.scheduled_debt_payment_id = p.id
                WHERE p.id = ? AND p.debt_id = ? AND EXISTS (SELECT 1 FROM debts d WHERE d.id = p.debt_id AND d.deleted_at = '') GROUP BY p.id
                """,
                (installment_id, debt_id),
            ).fetchone()
            if not installment or installment["paid"] or date.fromisoformat(installment["due_on"]) <= current:
                flash("Solo puedes eliminar cuotas futuras sin pagos.", "error")
            else:
                connection.execute("DELETE FROM scheduled_debt_payments WHERE id = ?", (installment_id,))
                _sync_debt_due_on(connection, debt_id)
                flash("Cuota eliminada.", "success")
        return redirect(url_for("debts_page"))

    @app.post("/deudas/<int:debt_id>/eliminar")
    def delete_debt(debt_id: int):
        with _db_connection(app) as connection:
            connection.execute("BEGIN IMMEDIATE")
            history = connection.execute(
                "SELECT 1 FROM movements WHERE debt_id = ? "
                "UNION ALL SELECT 1 FROM debt_balance_adjustments WHERE debt_id = ? "
                "UNION ALL SELECT 1 FROM movement_allocations a "
                "JOIN scheduled_debt_payments p ON p.id = a.scheduled_debt_payment_id "
                "WHERE p.debt_id = ? LIMIT 1",
                (debt_id, debt_id, debt_id),
            ).fetchone()
            if history:
                cursor = connection.execute(
                    "UPDATE debts SET deleted_at = ? WHERE id = ? AND deleted_at = ''",
                    (_now(), debt_id),
                )
            else:
                cursor = connection.execute("DELETE FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,))
            flash(
                "Deuda eliminada. Los movimientos registrados se conservaron." if cursor.rowcount else "Deuda no encontrada.",
                "success" if cursor.rowcount else "error",
            )
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("index", month=_selected_month(request.form.get("month"))))
        return redirect(url_for("debts_page"))

    @app.post("/objetivos")
    def create_personal_goal():
        try:
            title = _clean_text(request.form.get("title"), "el objetivo", limit=140)
            target = _money_to_cents(request.form.get("target"))
            saved = _optional_money_to_cents(request.form.get("saved"))
            fund_type = str(request.form.get("fund_type") or "planned")
            if fund_type not in FUND_TYPES:
                raise ValueError("Selecciona un tipo de fondo valido.")
            target_on = str(request.form.get("target_on") or "").strip()
            if target_on:
                target_on = _parse_date(target_on, "el objetivo")
            notes = _clean_text(
                request.form.get("notes"), "las notas personales", required=False, limit=1000
            )
            with _db_connection(app) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO goals
                        (title, target_on, target_cents, saved_cents, notes, is_completed, created_at, fund_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (title, target_on, target, saved, notes, int(saved >= target), _now(), fund_type),
                )
                if saved:
                    connection.execute(
                        """
                        INSERT INTO fund_transactions (goal_id, kind, amount_cents, occurred_on, notes, created_at)
                        VALUES (?, 'contribution', ?, ?, 'Saldo inicial', ?)
                        """,
                        (cursor.lastrowid, saved, _local_today(app.config["APP_TIMEZONE"]).isoformat(), _now()),
                    )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Objetivo guardado.", "success")
            if title in BARBERSHOP_GOAL_TITLES:
                return redirect(url_for("objective_page"))
        return redirect(url_for("goals_page"))

    @app.post("/objetivos/<int:goal_id>/ahorros")
    def add_goal_savings(goal_id: int):
        try:
            amount = _money_to_cents(request.form.get("amount"))
            occurred_on = _parse_date(request.form.get("occurred_on") or _local_today(app.config["APP_TIMEZONE"]), "la aportacion")
            with _db_connection(app) as connection:
                goal = connection.execute(
                    "SELECT title, target_cents, saved_cents FROM goals WHERE id = ?", (goal_id,)
                ).fetchone()
                if not goal:
                    raise ValueError("Objetivo no encontrado.")
                if goal["title"] in BARBERSHOP_GOAL_TITLES and _barbershop_blockers(connection)["blocked"]:
                    raise ValueError("Primero liquida deudas de un solo pago y deudas con abonos.")
                if goal["target_cents"] <= 0:
                    raise ValueError("Define primero un limite de ahorro.")
                connection.execute(
                    """
                    UPDATE goals
                    SET saved_cents = saved_cents + ?,
                        is_completed = (saved_cents + ? >= target_cents)
                    WHERE id = ?
                    """,
                    (amount, amount, goal_id),
                )
                connection.execute(
                    """
                    INSERT INTO fund_transactions (goal_id, kind, amount_cents, occurred_on, notes, created_at)
                    VALUES (?, 'contribution', ?, ?, ?, ?)
                    """,
                    (goal_id, amount, occurred_on, _clean_text(request.form.get("notes"), "la nota", required=False, limit=300), _now()),
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Ahorro agregado al objetivo.", "success")
        if "goal" in locals() and goal and goal["title"] in BARBERSHOP_GOAL_TITLES:
            return redirect(url_for("objective_page"))
        return redirect(url_for("goals_page"))

    @app.post("/objetivos/<int:goal_id>/retiros")
    def withdraw_goal_savings(goal_id: int):
        try:
            amount = _money_to_cents(request.form.get("amount"))
            occurred_on = _parse_date(request.form.get("occurred_on") or _local_today(app.config["APP_TIMEZONE"]), "el retiro")
            with _db_connection(app) as connection:
                goal = connection.execute("SELECT saved_cents FROM goals WHERE id = ?", (goal_id,)).fetchone()
                if not goal:
                    raise ValueError("Fondo no encontrado.")
                if amount > goal["saved_cents"]:
                    raise ValueError("El retiro no puede dejar el fondo en negativo.")
                connection.execute(
                    "UPDATE goals SET saved_cents = saved_cents - ?, is_completed = 0 WHERE id = ?",
                    (amount, goal_id),
                )
                connection.execute(
                    """
                    INSERT INTO fund_transactions (goal_id, kind, amount_cents, occurred_on, notes, created_at)
                    VALUES (?, 'withdrawal', ?, ?, ?, ?)
                    """,
                    (goal_id, amount, occurred_on, _clean_text(request.form.get("notes"), "la nota", required=False, limit=300), _now()),
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Retiro registrado.", "success")
        return redirect(url_for("goals_page"))

    @app.post("/objetivos/<int:goal_id>/editar")
    def edit_goal(goal_id: int):
        try:
            title = _clean_text(request.form.get("title"), "el fondo", limit=140)
            target = _money_to_cents(request.form.get("target"))
            target_on = str(request.form.get("target_on") or "").strip()
            if target_on:
                target_on = _parse_date(target_on, "el fondo")
            fund_type = str(request.form.get("fund_type") or "planned")
            if fund_type not in FUND_TYPES:
                raise ValueError("Selecciona un tipo de fondo valido.")
            notes = _clean_text(request.form.get("notes"), "las notas", required=False, limit=1000)
            with _db_connection(app) as connection:
                cursor = connection.execute(
                    """
                    UPDATE goals SET title = ?, target_cents = ?, target_on = ?, fund_type = ?, notes = ?,
                                     is_completed = (saved_cents >= ?)
                    WHERE id = ?
                    """,
                    (title, target, target_on, fund_type, notes, target, goal_id),
                )
                if not cursor.rowcount:
                    raise ValueError("Fondo no encontrado.")
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Fondo actualizado.", "success")
        return redirect(url_for("goals_page"))

    @app.post("/objetivos/<int:goal_id>/meta")
    def set_goal_target(goal_id: int):
        try:
            target = _money_to_cents(request.form.get("target"))
            target_on = str(request.form.get("target_on") or "").strip()
            if target_on:
                target_on = _parse_date(target_on, "el objetivo")
            with _db_connection(app) as connection:
                if target_on:
                    cursor = connection.execute(
                        """
                        UPDATE goals
                        SET target_cents = ?, target_on = ?, is_completed = (saved_cents >= ?)
                        WHERE id = ?
                        """,
                        (target, target_on, target, goal_id),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE goals
                        SET target_cents = ?, is_completed = (saved_cents >= ?)
                        WHERE id = ?
                        """,
                        (target, target, goal_id),
                    )
                if not cursor.rowcount:
                    raise ValueError("Objetivo no encontrado.")
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Limite de ahorro actualizado.", "success")
        if request.form.get("return_to") == "objective":
            return redirect(url_for("objective_page"))
        return redirect(url_for("goals_page"))

    @app.post("/objetivos/<int:goal_id>/eliminar")
    def delete_goal(goal_id: int):
        with _db_connection(app) as connection:
            history = connection.execute("SELECT COUNT(*) FROM fund_transactions WHERE goal_id = ?", (goal_id,)).fetchone()[0]
            cursor = connection.execute("DELETE FROM goals WHERE id = ?", (goal_id,)) if not history else None
        flash(
            "Fondo eliminado." if cursor and cursor.rowcount else "No puedes eliminar un fondo con historial.",
            "success" if cursor and cursor.rowcount else "error",
        )
        return redirect(url_for("goals_page"))

    @app.post("/compromisos")
    def create_commitment():
        try:
            current = _local_today(app.config["APP_TIMEZONE"])
            item = _commitment_form_data(request.form, current)
            entries = (
                [{**item, "frequency": "monthly", "month_day": day} for day in (15, 30)]
                if item["frequency"] == "fortnightly" else [item]
            )
            with _db_connection(app) as connection:
                connection.executemany(
                    """
                    INSERT INTO recurring_commitments
                        (title, amount_cents, frequency, starts_on, weekday, month_day, category, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(*entry.values(), _now()) for entry in entries],
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda guardada y agregada al calendario.", "success")
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("index", month=_selected_month(request.form.get("month"))))
        return redirect(f"{url_for('debts_page')}#deudas-recurrentes")

    @app.post("/compromisos/<int:commitment_id>/editar")
    def edit_commitment(commitment_id: int):
        try:
            item = _commitment_form_data(request.form, _local_today(app.config["APP_TIMEZONE"]))
            pair_id = int(request.form.get("pair_id") or 0)
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if pair_id and not _is_fortnightly_pair(connection, commitment_id, pair_id):
                    raise ValueError("La deuda quincenal ya no coincide; recarga Deudas.")
                if request.form.get("calendar_edit") == "1" or item["frequency"] == "fortnightly":
                    _update_commitment_calendar(connection, commitment_id, pair_id, item, _now())
                    cursor = None
                elif pair_id:
                    cursor = connection.execute(
                        "UPDATE recurring_commitments SET title = ?, amount_cents = ?, category = ?, notes = ? WHERE id IN (?, ?) AND deleted_at = ''",
                        (item["title"], item["amount_cents"], item["category"], item["notes"], commitment_id, pair_id),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE recurring_commitments
                        SET title = ?, amount_cents = ?, frequency = ?, starts_on = ?, weekday = ?,
                            month_day = ?, category = ?, notes = ?
                        WHERE id = ? AND deleted_at = ''
                        """,
                        (*item.values(), commitment_id),
                    )
                if cursor is not None and not cursor.rowcount:
                    raise ValueError("Deuda no encontrada.")
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda actualizada.", "success")
        return redirect(f"{url_for('debts_page')}#deudas-recurrentes")

    @app.post("/compromisos/<int:commitment_id>/estado")
    def set_commitment_state(commitment_id: int):
        active = 1 if request.form.get("active") == "1" else 0
        try:
            try:
                pair_id = int(request.form.get("pair_id") or 0)
            except ValueError:
                pair_id = 0
            starts_on = ""
            if active:
                starts_on = _parse_date(request.form.get("starts_on"), "la reanudación")
                if date.fromisoformat(starts_on) < _local_today(app.config["APP_TIMEZONE"]):
                    raise ValueError("La fecha de reanudación no puede estar en el pasado.")
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if pair_id and not _is_fortnightly_pair(connection, commitment_id, pair_id):
                    raise ValueError("La deuda quincenal ya no coincide; recarga Deudas.")
                if active:
                    cursor = connection.execute(
                        "UPDATE recurring_commitments SET is_active = 1, starts_on = ? WHERE id IN (?, ?) AND deleted_at = ''",
                        (starts_on, commitment_id, pair_id or commitment_id),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE recurring_commitments SET is_active = 0 WHERE id IN (?, ?) AND deleted_at = ''",
                        (commitment_id, pair_id or commitment_id),
                    )
                if not cursor.rowcount:
                    raise ValueError("Deuda no encontrada.")
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Deuda reanudada desde {starts_on}." if active else "Deuda pausada.", "success")
        return redirect(f"{url_for('debts_page')}#deudas-recurrentes")

    @app.post("/compromisos/<int:commitment_id>/eliminar")
    def delete_commitment(commitment_id: int):
        try:
            pair_id = int(request.form.get("pair_id") or 0)
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if pair_id and not _is_fortnightly_pair(connection, commitment_id, pair_id):
                    raise ValueError("La deuda quincenal ya no coincide; recarga Deudas.")
                if connection.execute(
                    "SELECT 1 FROM movement_allocations WHERE recurring_commitment_id IN (?, ?) LIMIT 1",
                    (commitment_id, pair_id or commitment_id),
                ).fetchone():
                    cursor = connection.execute(
                        "UPDATE recurring_commitments SET is_active = 0, deleted_at = ? "
                        "WHERE id IN (?, ?) AND deleted_at = ''",
                        (_now(), commitment_id, pair_id or commitment_id),
                    )
                else:
                    cursor = connection.execute(
                        "DELETE FROM recurring_commitments WHERE id IN (?, ?) AND deleted_at = ''",
                        (commitment_id, pair_id or commitment_id),
                    )
                if not cursor.rowcount:
                    raise ValueError("Deuda no encontrada.")
        except (TypeError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash("Deuda eliminada.", "success")
        return redirect(f"{url_for('debts_page')}#deudas-recurrentes")

    @app.post("/compromisos/<int:commitment_id>/excepcion")
    def set_commitment_exception(commitment_id: int):
        try:
            occurrence_on = _parse_date(request.form.get("occurrence_on"), "la excepcion")
            omitted = int(bool(request.form.get("is_omitted")))
            amount_raw = str(request.form.get("amount") or "").strip()
            amount = _money_to_cents(amount_raw) if amount_raw else None
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                commitment = connection.execute("SELECT * FROM recurring_commitments WHERE id = ?", (commitment_id,)).fetchone()
                occurrence = date.fromisoformat(occurrence_on)
                if not commitment or occurrence not in _commitment_dates(commitment, occurrence, occurrence):
                    raise ValueError("La fecha no corresponde a esta deuda.")
                paid = connection.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM movement_allocations WHERE recurring_commitment_id = ? AND occurrence_on = ?",
                                          (commitment_id, occurrence_on)).fetchone()[0]
                if amount is not None and amount < paid:
                    raise ValueError("El monto programado no puede ser menor a los abonos realizados.")
                if omitted and paid:
                    raise ValueError("No puedes omitir un pago que tiene abonos.")
                connection.execute(
                    """
                    INSERT INTO commitment_exceptions
                        (commitment_id, occurrence_on, amount_cents, is_omitted, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(commitment_id, occurrence_on)
                    DO UPDATE SET amount_cents = excluded.amount_cents, is_omitted = excluded.is_omitted
                    """,
                    (commitment_id, occurrence_on, amount, omitted, _now()),
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Excepcion guardada.", "success")
        return redirect(url_for("index", month=request.form.get("month")))

    @app.post("/compromisos/<int:commitment_id>/pagar")
    def pay_commitment(commitment_id: int):
        try:
            amount = _money_to_cents(request.form.get("amount"))
            occurred_on = _parse_date(request.form.get("occurred_on") or _local_today(app.config["APP_TIMEZONE"]), "el pago")
            if date.fromisoformat(occurred_on) > _local_today(app.config["APP_TIMEZONE"]):
                raise ValueError("La fecha del abono no puede estar en el futuro.")
            occurrence_dates = request.form.getlist("occurrence_on")
            if not occurrence_dates:
                raise ValueError("Selecciona al menos una fecha por cubrir.")
            parsed_dates = [_parse_date(value, "la fecha cubierta") for value in occurrence_dates]
            with _db_connection(app) as connection:
                connection.execute("BEGIN IMMEDIATE")
                commitment = connection.execute("SELECT * FROM recurring_commitments WHERE id = ?", (commitment_id,)).fetchone()
                if not commitment:
                    raise ValueError("Deuda no encontrada.")
                occurrences = {
                    item["occurrence_on"]: item
                    for item in _commitment_occurrences(
                        connection, date.fromisoformat(min(parsed_dates)), date.fromisoformat(max(parsed_dates))
                    )
                    if item["commitment_id"] == commitment_id
                }
                selected = [occurrences[value] for value in parsed_dates if value in occurrences and occurrences[value]["remaining_cents"]]
                if len(selected) != len(set(parsed_dates)):
                    raise ValueError("Una fecha no corresponde a la deuda, fue omitida o ya está pagada.")
                if amount > sum(item["remaining_cents"] for item in selected):
                    if len(selected) != 1:
                        raise ValueError("Para registrar un monto mayor, selecciona una sola fecha.")
                    occurrence = selected[0]
                    connection.execute(
                        """
                        INSERT INTO commitment_exceptions
                            (commitment_id, occurrence_on, amount_cents, is_omitted, created_at)
                        VALUES (?, ?, ?, 0, ?)
                        ON CONFLICT(commitment_id, occurrence_on)
                        DO UPDATE SET amount_cents = excluded.amount_cents
                        """,
                        (
                            commitment_id, occurrence["source_occurrence_on"],
                            occurrence["paid_cents"] + amount, _now(),
                        ),
                    )
                    occurrence["remaining_cents"] = amount
                cursor = connection.execute(
                    """
                    INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at)
                    VALUES ('expense', ?, ?, ?, ?, ?)
                    """,
                    (commitment["category"], commitment["title"], amount, occurred_on, _now()),
                )
                available = amount
                allocations = []
                for occurrence in selected:
                    allocated = min(available, occurrence["remaining_cents"])
                    if allocated:
                        allocations.append((cursor.lastrowid, commitment_id, occurrence["source_occurrence_on"], allocated, _now()))
                        available -= allocated
                    if not available:
                        break
                connection.executemany(
                    """
                    INSERT INTO movement_allocations
                        (movement_id, recurring_commitment_id, occurrence_on, amount_cents, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    allocations,
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Pago registrado en el historial.", "success")
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("index", month=_selected_month(request.form.get("month"))))
        return redirect(url_for("calendar_page"))

    @app.route("/configuracion/compromisos-iniciales", methods=["GET", "POST"])
    def initial_commitments():
        current = _local_today(app.config["APP_TIMEZONE"])
        presets = (
            ("home", "Hogar", 100_00, "fortnightly", "Servicios"),
            ("transport", "Transporte", 100_00, "fortnightly", "Transporte"),
            ("subscription", "Suscripción", 100_00, "monthly", "Servicios"),
        )
        if request.method == "POST":
            try:
                with _db_connection(app) as connection:
                    for slug, title, default_amount, frequency, category in presets:
                        if not request.form.get(f"enable_{slug}"):
                            continue
                        amount = _money_to_cents(request.form.get(f"amount_{slug}") or Decimal(default_amount) / 100)
                        month_days = (None,)
                        weekday = None
                        if frequency == "monthly":
                            try:
                                month_day = int(request.form.get(f"day_{slug}") or 0)
                            except ValueError:
                                raise ValueError(f"Captura el dia mensual de {title}.") from None
                            if not 1 <= month_day <= 31:
                                raise ValueError(f"Captura el dia mensual de {title}.")
                            month_days = (month_day,)
                        elif frequency == "fortnightly":
                            month_days = (15, 30)
                        elif frequency == "weekly":
                            weekday = 5
                        stored_frequency = "monthly" if frequency == "fortnightly" else frequency
                        for month_day in month_days:
                            connection.execute(
                                """
                                INSERT INTO recurring_commitments
                                    (title, amount_cents, frequency, starts_on, weekday, month_day, category, created_at)
                                SELECT ?, ?, ?, ?, ?, ?, ?, ?
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM recurring_commitments
                                    WHERE title = ? AND frequency = ? AND COALESCE(month_day, 0) = COALESCE(?, 0)
                                )
                                """,
                                (
                                    title, amount, stored_frequency, current.isoformat(), weekday, month_day, category, _now(),
                                    title, stored_frequency, month_day,
                                ),
                            )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash("Deudas iniciales agregadas al calendario.", "success")
                return redirect(f"{url_for('debts_page')}#deudas-recurrentes")
        return render_template("commitments_setup.html", active_page="settings", presets=presets)

    @app.post("/configuracion/meta-deuda-urgente")
    def save_urgent_target():
        try:
            target = _money_to_cents(request.form.get("amount"))
            with _db_connection(app) as connection:
                connection.execute(
                    """
                    INSERT INTO settings (key, value, updated_at) VALUES ('urgent_debt_target_cents', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (str(target), _now()),
                )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Meta urgente actualizada.", "success")
        return redirect(url_for("settings_page"))

    @app.post("/configuracion/gmail")
    def save_gmail_settings():
        try:
            email, password = _gmail_form_credentials(request.form)
            enabled = "1" if request.form.get("reminders_enabled") else "0"
            xml_enabled = "1" if request.form.get("xml_imports_enabled") else "0"
            now = _now()
            settings = [
                ("gmail_email", email, now),
                ("reminders_enabled", enabled, now),
                ("xml_imports_enabled", xml_enabled, now),
            ]
            if password:
                settings.append(
                    ("gmail_app_password", _secret_cipher(app).encrypt(password.encode()).decode(), now)
                )
            with _db_connection(app) as connection:
                connection.executemany(
                    """
                    INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    settings,
                )
                gmail = _reminder_settings(connection, app)
            if request.form.get("action") == "test":
                if not gmail["password"]:
                    raise ValueError("Guarda primero una clave de aplicacion de Gmail.")
                from email.message import EmailMessage

                message = EmailMessage()
                message["Subject"] = "Prueba de recordatorios · Finanzas"
                message["From"] = gmail["email"]
                message["To"] = gmail["email"]
                message.set_content(
                    "Tu cuenta quedo conectada. Los recordatorios de deuda se enviaran a este correo."
                )
                _send_gmail(gmail, message)
        except ValueError as exc:
            flash(str(exc), "error")
        except (OSError, smtplib.SMTPException):
            flash("No se pudo conectar con Gmail. Revisa el correo y la clave de aplicacion.", "error")
        else:
            flash(
                "Correo de prueba enviado." if request.form.get("action") == "test" else "Configuracion guardada.",
                "success",
            )
        return redirect(url_for("settings_page"))


    @app.errorhandler(413)
    def file_too_large(_error):
        return "El archivo supera el limite permitido.", 413

    @app.get("/app.webmanifest")
    def web_manifest():
        return send_file(
            ROOT_DIR / "static" / "pwa" / "app.webmanifest",
            mimetype="application/manifest+json",
            conditional=True,
            max_age=3600,
        )

    @app.get("/service-worker.js")
    def service_worker():
        response = send_file(
            ROOT_DIR / "static" / "pwa" / "service-worker.js",
            mimetype="application/javascript",
            conditional=True,
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "service": PROJECT_KEY, "port": app.config["PORT"]})

    from debt_management import register_debt_management
    register_debt_management(app, _db_connection, _local_today, _parse_date,
                             _money_to_cents, _clean_text, _now, _debt_installments,
                             _trim_future_installments_to_balance, _sync_debt_due_on,
                             _commitment_occurrences)
    from minimal_debts import register
    import sys
    register(app, sys.modules[__name__])
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=app.config["PORT"], debug=False)
