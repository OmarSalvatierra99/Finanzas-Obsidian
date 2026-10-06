#!/usr/bin/env python3
"""Envia un resumen por Gmail cuando una deuda se acerca a su vencimiento."""

from __future__ import annotations

from datetime import date, timedelta
from email.message import EmailMessage

from app import (
    _connect_db,
    _format_money,
    _local_today,
    _record_due_automatic_expenses,
    _reminder_settings,
    _send_gmail,
    app,
)


def due_debts(connection, today: date) -> list[dict]:
    debts = []
    for row in connection.execute(
        """
        SELECT d.id, d.name, d.creditor, d.balance_cents, d.due_on, d.reminder_days,
               COALESCE((
                   SELECT p.amount_cents - COALESCE(SUM(a.amount_cents), 0)
                   FROM scheduled_debt_payments AS p
                   LEFT JOIN movement_allocations AS a ON a.scheduled_debt_payment_id = p.id
                   WHERE p.debt_id = d.id AND p.due_on = d.due_on
                   GROUP BY p.id ORDER BY p.id LIMIT 1
               ), d.balance_cents) AS next_payment_cents
        FROM debts AS d
        WHERE d.balance_cents > 0 AND d.deleted_at = '' AND d.due_on != '' AND (d.schedule_type != 'single' OR d.todo_completed = 0) AND d.reminded_due_on != d.due_on
        ORDER BY d.due_on, d.id
        """
    ):
        if date.fromisoformat(row["due_on"]) <= today + timedelta(days=row["reminder_days"]):
            debts.append(dict(row))
    return debts


def build_message(debts: list[dict], sender: str, recipient: str, today: date) -> EmailMessage:
    lines = ["Estas deudas necesitan tu atención:", ""]
    for debt in debts:
        days = (date.fromisoformat(debt["due_on"]) - today).days
        timing = (
            f"venció hace {abs(days)} día(s)"
            if days < 0
            else "vence hoy" if days == 0 else f"vence en {days} día(s)"
        )
        lines.append(
            f"- {debt['creditor']}: {debt['name']}, próximo pago "
            f"{_format_money(debt.get('next_payment_cents', debt['balance_cents']))}, "
            f"{timing} ({debt['due_on']})."
        )
    lines.extend(["", "Abre tu agenda: https://finanzas.omar-xyz.shop/deudas"])

    message = EmailMessage()
    message["Subject"] = f"Recordatorio de deudas · {len(debts)} pendiente(s)"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(lines))
    return message


def main() -> int:
    today = _local_today(app.config["APP_TIMEZONE"])
    connection = _connect_db(app)
    try:
        with connection:
            automatic_count = _record_due_automatic_expenses(connection, today)
        if automatic_count:
            print(f"Gastos automaticos registrados: {automatic_count}.")
        settings = _reminder_settings(connection, app)
        if not settings["enabled"]:
            print("Recordatorios desactivados desde Configuracion.")
            return 0
        if not settings["email"] or not settings["password"]:
            print("Recordatorios omitidos: configura Gmail desde la aplicacion.")
            return 0
        debts = due_debts(connection, today)
        if not debts:
            print("No hay recordatorios de deuda pendientes.")
            return 0
        message = build_message(debts, settings["email"], settings["email"], today)
        _send_gmail(settings, message)
        with connection:
            connection.executemany(
                "UPDATE debts SET reminded_due_on = due_on WHERE id = ?",
                [(debt["id"],) for debt in debts],
            )
    finally:
        connection.close()

    print(f"Recordatorio enviado a {settings['email']}: {len(debts)} deuda(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
