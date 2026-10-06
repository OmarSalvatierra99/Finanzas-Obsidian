"""Operaciones de calendario y ajustes que conservan el historial financiero."""

from datetime import date
import sqlite3

from flask import flash, jsonify, redirect, request, url_for


def register_debt_management(app, connect, today, parse_date, money, clean, now,
                             installments, trim, sync_due, occurrences):
    def finish(message, target="", error=False):
        if request.headers.get("Accept") == "application/json":
            return jsonify(ok=not error, message=message, target_on=target), 409 if error else 200
        flash(message, "error" if error else "success")
        if request.form.get("from_calendar") == "1":
            return redirect(url_for("calendar_page" if request.form.get("calendar_view") == "calendar" else "index",
                                    month=target[:7] or request.form.get("month")))
        return redirect(url_for("debts_page"))

    def future_target():
        target = parse_date(request.form.get("target_on"), "la nueva fecha")
        if date.fromisoformat(target) < today(app.config["APP_TIMEZONE"]):
            raise ValueError("Mueve el pago a hoy o a una fecha futura.")
        return target

    @app.post("/deudas/<int:debt_id>/cuotas/<int:installment_id>/mover")
    def move_debt_installment(debt_id, installment_id):
        try:
            target = future_target()
            expected = parse_date(request.form.get("expected_on"), "la fecha actual")
            with connect(app) as db:
                db.execute("BEGIN IMMEDIATE")
                item = next((p for p in installments(db) if p["id"] == installment_id
                             and p["debt_id"] == debt_id), None)
                if not item or not item["remaining_cents"] or not item["balance_cents"]:
                    raise ValueError("El pago ya fue liquidado o no existe. Recarga el calendario.")
                if item["due_on"] != expected:
                    raise ValueError("La fecha cambió en otra sesión. Recarga el calendario.")
                if db.execute("SELECT 1 FROM scheduled_debt_payments WHERE debt_id = ? AND due_on = ? AND id != ?",
                              (debt_id, target, installment_id)).fetchone():
                    raise ValueError("Esta deuda ya tiene una cuota en esa fecha.")
                db.execute("UPDATE scheduled_debt_payments SET due_on = ? WHERE id = ?", (target, installment_id))
                sync_due(db, debt_id)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return finish(str(exc) if isinstance(exc, ValueError) else "La fecha ya está ocupada.", error=True)
        return finish("Pago movido en el calendario.", target)

    @app.post("/compromisos/<int:commitment_id>/mover")
    def move_commitment_occurrence(commitment_id):
        try:
            target = future_target()
            source = parse_date(request.form.get("source_occurrence_on"), "la fecha original")
            expected = parse_date(request.form.get("expected_on"), "la fecha actual")
            with connect(app) as db:
                db.execute("BEGIN IMMEDIATE")
                exception = db.execute("SELECT moved_to FROM commitment_exceptions WHERE commitment_id = ? AND occurrence_on = ?",
                                       (commitment_id, source)).fetchone()
                current = exception["moved_to"] if exception and exception["moved_to"] else source
                if current != expected:
                    raise ValueError("La fecha cambió en otra sesión. Recarga el calendario.")
                item = next((p for p in occurrences(db, date.fromisoformat(current), date.fromisoformat(current))
                             if p["commitment_id"] == commitment_id and p["source_occurrence_on"] == source), None)
                if not item or not item["remaining_cents"]:
                    raise ValueError("El pago ya fue liquidado, omitido o no existe.")
                occupied = occurrences(db, date.fromisoformat(target), date.fromisoformat(target))
                if any(p["commitment_id"] == commitment_id and p["source_occurrence_on"] != source for p in occupied):
                    raise ValueError("Esta deuda ya tiene un pago en esa fecha.")
                db.execute("""INSERT INTO commitment_exceptions
                    (commitment_id, occurrence_on, amount_cents, is_omitted, moved_to, created_at)
                    VALUES (?, ?, NULL, 0, ?, ?) ON CONFLICT(commitment_id, occurrence_on)
                    DO UPDATE SET moved_to = excluded.moved_to, is_omitted = 0""",
                    (commitment_id, source, "" if target == source else target, now()))
        except (ValueError, sqlite3.IntegrityError) as exc:
            return finish(str(exc) if isinstance(exc, ValueError) else "La fecha ya está ocupada.", error=True)
        return finish("Pago movido en el calendario.", target)

    def set_balance(db, debt_id, previous, balance, reason, target):
        pending = [p for p in installments(db) if p["debt_id"] == debt_id and p["remaining_cents"]]
        scheduled = sum(p["remaining_cents"] for p in pending)
        if balance < scheduled:
            trim(db, debt_id, balance)
        elif balance > scheduled:
            extra = balance - scheduled
            if pending:
                db.execute("UPDATE scheduled_debt_payments SET amount_cents = amount_cents + ? WHERE id = ?",
                           (extra, pending[-1]["id"]))
            else:
                target = target()
                db.execute("""INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at)
                    VALUES (?, ?, ?, ?) ON CONFLICT(debt_id, due_on)
                    DO UPDATE SET amount_cents = amount_cents + excluded.amount_cents""",
                    (debt_id, target, extra, now()))
        db.execute("UPDATE debts SET balance_cents = ?, reminded_due_on = '' WHERE id = ?", (balance, debt_id))
        db.execute("""INSERT INTO debt_balance_adjustments
            (debt_id, previous_cents, balance_cents, reason, created_at) VALUES (?, ?, ?, ?, ?)""",
            (debt_id, previous, balance, reason, now()))
        sync_due(db, debt_id)
        if not balance:
            db.execute("UPDATE debts SET due_on = '' WHERE id = ?", (debt_id,))

    @app.post("/deudas/<int:debt_id>/editar-simple")
    def edit_single_debt(debt_id):
        try:
            name = clean(request.form.get("name"), "a quien le debes", limit=100)
            raw = str(request.form.get("amount") or "").strip()
            balance = 0 if raw in {"0", "0.0", "0.00"} else money(raw)
            expected = int(request.form.get("expected_balance"))
            with connect(app) as db:
                db.execute("BEGIN IMMEDIATE")
                debt = db.execute("SELECT balance_cents, schedule_type FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,)).fetchone()
                if not debt or debt["schedule_type"] != "single":
                    raise ValueError("Deuda de un solo pago no encontrada.")
                previous = debt["balance_cents"]
                if previous != expected:
                    raise ValueError("El monto cambió. Recarga la deuda antes de guardarla.")
                if balance != previous:
                    set_balance(db, debt_id, previous, balance, "Edición de deuda de un solo pago", lambda: today(app.config["APP_TIMEZONE"]).isoformat())
                db.execute("UPDATE debts SET name = ?, creditor = ? WHERE id = ?", (name, name, debt_id))
        except (TypeError, ValueError, sqlite3.IntegrityError) as exc:
            return finish(str(exc) if isinstance(exc, ValueError) else "No se pudo guardar la deuda.", error=True)
        return finish("Deuda actualizada.")

    @app.post("/deudas/<int:debt_id>/ajustar-saldo")
    def adjust_debt_balance(debt_id):
        try:
            raw = str(request.form.get("balance") or "").strip()
            balance = 0 if raw in {"0", "0.0", "0.00"} else money(raw)
            reason = clean(request.form.get("reason"), "el motivo del ajuste", limit=300)
            expected = int(request.form.get("expected_balance"))
            with connect(app) as db:
                db.execute("BEGIN IMMEDIATE")
                debt = db.execute("SELECT balance_cents FROM debts WHERE id = ? AND deleted_at = ''", (debt_id,)).fetchone()
                if not debt:
                    raise ValueError("Deuda no encontrada.")
                previous = debt["balance_cents"]
                if expected != previous:
                    raise ValueError("El saldo cambió. Recarga la deuda antes de ajustarla.")
                if balance == previous:
                    raise ValueError("El saldo nuevo es igual al actual.")
                set_balance(db, debt_id, previous, balance, reason, future_target)
        except (TypeError, ValueError, sqlite3.IntegrityError) as exc:
            return finish(str(exc) if isinstance(exc, ValueError) else "No se pudo ajustar el saldo. Recarga la deuda.", error=True)
        return finish("Saldo ajustado; los abonos anteriores se conservaron.")
