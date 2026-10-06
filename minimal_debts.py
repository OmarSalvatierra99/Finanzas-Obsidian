"""Reminder state and compact views, independent of financial payment records."""
from datetime import date, timedelta
from pathlib import Path
import calendar
import sqlite3
import secrets
import os
from flask import flash, jsonify, redirect, request, url_for

INCOME = max(0, int(os.getenv("QUINCENAL_INCOME_CENTS", "0")))


def backup_before_migration(path):
    path = Path(path)
    if not path.exists():
        return
    with sqlite3.connect(path) as source:
        columns = {r[1] for r in source.execute('PRAGMA table_info(debts)')}
        if not columns or 'todo_completed' in columns:
            return
        from datetime import datetime
        folder = path.parent / 'backups'
        folder.mkdir(exist_ok=True)
        with sqlite3.connect(folder / ('before-minimal-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')) as target:
            source.backup(target)


def initialize(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(debts)')}
    if 'todo_completed' not in columns:
        db.execute('ALTER TABLE debts ADD COLUMN todo_completed INTEGER NOT NULL DEFAULT 0 CHECK(todo_completed IN (0,1))')
        db.execute("UPDATE debts SET todo_completed = 1 WHERE schedule_type = 'single' AND balance_cents = 0")
    if 'todo_amount_cents' not in columns:
        db.execute('ALTER TABLE debts ADD COLUMN todo_amount_cents INTEGER NOT NULL DEFAULT 0')
        db.execute("UPDATE debts SET todo_amount_cents = CASE WHEN balance_cents > 0 THEN balance_cents ELSE original_cents END WHERE schedule_type = 'single'")
    if 'todo_due_on' not in columns:
        db.execute("ALTER TABLE debts ADD COLUMN todo_due_on TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE debts SET todo_due_on = COALESCE((SELECT MIN(due_on) FROM scheduled_debt_payments p WHERE p.debt_id = debts.id), due_on) WHERE schedule_type = 'single'")
    db.execute('''CREATE TABLE IF NOT EXISTS reminder_completions (
        commitment_id INTEGER NOT NULL REFERENCES recurring_commitments(id) ON DELETE CASCADE,
        occurrence_on TEXT NOT NULL, completed INTEGER NOT NULL CHECK(completed IN (0,1)),
        PRIMARY KEY(commitment_id, occurrence_on))''')
    db.execute('UPDATE recurring_commitments SET auto_record = 0 WHERE auto_record != 0')
    db.execute('''CREATE TABLE IF NOT EXISTS debt_payment_requests (
        token TEXT PRIMARY KEY, debt_id INTEGER NOT NULL REFERENCES debts(id) ON DELETE CASCADE,
        amount_cents INTEGER NOT NULL, occurred_on TEXT NOT NULL)''')


def view(db, f, current, month):
    start, end = map(date.fromisoformat, f._month_bounds(month))
    singles = []
    for r in db.execute("SELECT * FROM debts WHERE schedule_type = 'single' AND deleted_at = '' ORDER BY id DESC"):
        item = dict(r)
        item.update(amount_cents=r['todo_amount_cents'] or r['balance_cents'] or r['original_cents'],
                    due_on=r['todo_due_on'] or r['due_on'], completed=bool(r['todo_completed']))
        item['amount'] = f._format_money(item['amount_cents'])
        singles.append(item)
    completions = {(r['commitment_id'], r['occurrence_on']): bool(r['completed']) for r in db.execute('SELECT * FROM reminder_completions')}
    occurrences = f._commitment_occurrences(db, date(2000, 1, 1), end)
    # Legacy one-off commitments can be scheduled beyond the selected month.
    for row in db.execute("SELECT * FROM recurring_commitments WHERE frequency = 'once' AND is_active = 1 AND deleted_at = ''"):
        if any(p['commitment_id'] == row['id'] for p in occurrences):
            continue
        exception = db.execute('SELECT * FROM commitment_exceptions WHERE commitment_id = ? AND occurrence_on = ?', (row['id'], row['starts_on'])).fetchone()
        amount = (exception['amount_cents'] if exception else None) or row['amount_cents']
        paid = db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM movement_allocations WHERE recurring_commitment_id = ? AND occurrence_on = ?', (row['id'], row['starts_on'])).fetchone()[0]
        occurrences.append({**dict(row), 'commitment_id':row['id'], 'source_occurrence_on':row['starts_on'],
                            'occurrence_on':(exception['moved_to'] if exception else '') or row['starts_on'],
                            'is_omitted':bool(exception and exception['is_omitted']), 'amount_cents':amount,
                            'remaining_cents':max(0,amount-paid)})
    payments = []
    for p in occurrences:
        if p['is_omitted']:
            continue
        key = (p['commitment_id'], p['source_occurrence_on'])
        done = completions.get(key, p['remaining_cents'] == 0)
        payments.append({**p, 'kind': 'recurring', 'source_id': p['commitment_id'], 'label': p['title'], 'completed': done,
                         'date': p['occurrence_on'], 'key': f"r-{p['commitment_id']}-{p['source_occurrence_on']}"})
    for p in singles:
        if p['due_on']:
            payments.append({**p, 'kind': 'single', 'source_id': p['id'], 'label': p['name'], 'date': p['due_on'],
                             'key': f"s-{p['id']}", 'source_occurrence_on': p['due_on']})
    for p in f._debt_installments(db):
        if p['schedule_type'] == 'single':
            continue
        payments.append({**p, 'kind': 'installment', 'source_id': p['debt_id'], 'label': p['creditor'],
                         'date': p['due_on'], 'source_occurrence_on': p['due_on'], 'completed': not p['remaining_cents'],
                         'key': f"i-{p['id']}"})
    for p in payments:
        p['amount'] = f._format_money(p['amount_cents'])
    selected = [p for p in payments if start.isoformat() <= p['date'] <= end.isoformat()]
    totals = [sum(p['amount_cents'] for p in selected if int(p['date'][-2:]) <= 15),
              sum(p['amount_cents'] for p in selected if int(p['date'][-2:]) >= 16)]
    stats = []
    for label, amount, limit in [('Mes', sum(totals), INCOME * 2), ('1–15', totals[0], INCOME), ('16–fin de mes', totals[1], INCOME)]:
        stats.append(dict(label=label, amount=f._format_money(amount), cents=amount, limit=f._format_money(limit),
                          exceeded=bool(limit and amount > limit), excess=f._format_money(max(0, amount-limit))))
    weeks = [[dict(date=d.isoformat(), number=d.day, in_month=d.month == start.month, today=d == current,
                   payments=[p for p in selected if p['date'] == d.isoformat() and not p['completed']]) for d in week]
             for week in calendar.Calendar(firstweekday=0).monthdatescalendar(start.year, start.month)]
    debts = f._list_debts(db, current)
    recurring = f._list_recurring_debts(db)
    for item in recurring:
        begins = date.fromisoformat(item['starts_on'])
        rows = db.execute('SELECT * FROM recurring_commitments WHERE id IN (?, ?)', (item['id'], item['pair_id'] or item['id'])).fetchall()
        first_dates = [day for row in rows for day in f._commitment_dates(row, begins, begins + timedelta(days=65))]
        item['first_on'] = min(first_dates).isoformat() if first_dates else item['starts_on']
    visible_payments = [p for p in payments if not p['completed'] or p['kind'] == 'installment']
    pending_singles = [p for p in singles if not p['completed']]
    once_pending = [p for p in payments if p['kind'] == 'recurring' and p['frequency'] == 'once' and not p['completed']]
    single_total = sum(p['amount_cents'] for p in pending_singles + once_pending)
    return dict(singles=pending_singles, once_pending=once_pending, single_total=f._format_money(single_total),
                installment_debts=[p for p in debts if p['schedule_type'] != 'single' and p['balance_cents']],
                settled_installments=[p for p in debts if p['schedule_type'] != 'single' and not p['balance_cents']],
                recurring_debts=[p for p in recurring if p['frequency'] != 'once'], stats=stats, weeks=weeks,
                payments=[p for p in selected if not p['completed']], all_payments=visible_payments,
                overdue=[p for p in payments if p['date'] < current.isoformat() and not p['completed']],
                month_label=f"{f.MONTH_NAMES[start.month]} {start.year}", selected_month=month, today=current.isoformat(),
                previous_month=f._add_months(start, -1).strftime('%Y-%m'), next_month=f._add_months(start, 1).strftime('%Y-%m'),
                current_month=current.strftime('%Y-%m'), payment_tokens={p['id']: secrets.token_urlsafe(24) for p in debts})


def register(app, f):
    def finish(message, error=False):
        if request.headers.get('Accept') == 'application/json':
            return jsonify(ok=not error, message=message), 409 if error else 200
        flash(message, 'error' if error else 'success')
        return redirect(url_for('index', month=request.form.get('month')) if request.form.get('from_calendar') == '1' else url_for('debts_page'))

    @app.post('/pendientes/<int:debt_id>/completar')
    def complete_todo(debt_id):
        try:
            completed = int(request.form.get('completed', '1'))
            if completed != 1:
                raise ValueError('Estado inválido.')
            with f._db_connection(app) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM debts WHERE id = ? AND schedule_type = 'single' AND deleted_at = ''", (debt_id,)).fetchone()
                if not row:
                    raise ValueError('Pendiente no encontrado.')
                history = db.execute(
                    'SELECT 1 FROM movements WHERE debt_id = ? '
                    'UNION ALL SELECT 1 FROM debt_balance_adjustments WHERE debt_id = ? '
                    'UNION ALL SELECT 1 FROM movement_allocations a '
                    'JOIN scheduled_debt_payments p ON p.id = a.scheduled_debt_payment_id '
                    'WHERE p.debt_id = ? LIMIT 1', (debt_id, debt_id, debt_id)).fetchone()
                if history:
                    db.execute('UPDATE debts SET deleted_at = ? WHERE id = ?', (f._now(), debt_id))
                else:
                    db.execute('DELETE FROM debts WHERE id = ?', (debt_id,))
        except (ValueError, TypeError) as exc:
            return finish(str(exc), True)
        return finish('Pendiente eliminado.')

    @app.post('/recordatorios/<int:commitment_id>/completar')
    def complete_reminder(commitment_id):
        try:
            source = f._parse_date(request.form.get('occurrence_on'), 'la fecha')
            completed = int(request.form.get('completed', '1'))
            if completed != 1:
                raise ValueError('Estado inválido.')
            with f._db_connection(app) as db:
                row = db.execute("SELECT * FROM recurring_commitments WHERE id = ? AND is_active = 1 AND deleted_at = ''", (commitment_id,)).fetchone()
                if not row or date.fromisoformat(source) not in f._commitment_dates(row, date.fromisoformat(source), date.fromisoformat(source)):
                    raise ValueError('Recordatorio no encontrado.')
                db.execute('''INSERT INTO reminder_completions VALUES (?, ?, ?) ON CONFLICT(commitment_id, occurrence_on)
                              DO UPDATE SET completed = excluded.completed''', (commitment_id, source, completed))
        except (ValueError, TypeError) as exc:
            return finish(str(exc), True)
        return finish('Fecha eliminada.')

    @app.post('/pendientes/<int:debt_id>/editar')
    def edit_todo(debt_id):
        try:
            name = f._clean_text(request.form.get('name'), 'a quién le debes', limit=100)
            amount = f._money_to_cents(request.form.get('amount'))
            due = f._parse_date(request.form.get('due_on'), 'la fecha') if request.form.get('due_on') else ''
            with f._db_connection(app) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM debts WHERE id = ? AND schedule_type = 'single' AND deleted_at = ''", (debt_id,)).fetchone()
                if not row:
                    raise ValueError('Pendiente no encontrado.')
                expected = int(request.form.get('expected_amount'))
                if expected != (row['todo_amount_cents'] or row['balance_cents'] or row['original_cents']):
                    raise ValueError('El monto cambió. Recarga antes de editar.')
                if 'expected_on' in request.form and request.form['expected_on'] != (row['todo_due_on'] or row['due_on']):
                    raise ValueError('La fecha cambió. Recarga antes de editar.')
                db.execute('UPDATE debts SET name = ?, creditor = ?, balance_cents = ?, todo_amount_cents = ?, todo_due_on = ?, due_on = ? WHERE id = ?',
                           (name, name, amount, amount, due, due, debt_id))
                # Keep allocated historical rows intact. Undated tasks have no outstanding schedule.
                db.execute('DELETE FROM scheduled_debt_payments WHERE debt_id = ? AND NOT EXISTS (SELECT 1 FROM movement_allocations a WHERE a.scheduled_debt_payment_id = scheduled_debt_payments.id)', (debt_id,))
                if due and not db.execute('SELECT 1 FROM scheduled_debt_payments WHERE debt_id = ? AND due_on = ?', (debt_id, due)).fetchone():
                    db.execute('INSERT INTO scheduled_debt_payments (debt_id, due_on, amount_cents, created_at) VALUES (?, ?, ?, ?)', (debt_id, due, amount, f._now()))
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return finish(str(exc), True)
        return finish('Pendiente actualizado.')

    @app.post('/recordatorios/<int:commitment_id>/editar-fecha')
    def edit_reminder_occurrence(commitment_id):
        try:
            source = f._parse_date(request.form.get('occurrence_on'), 'la fecha original')
            target = f._parse_date(request.form.get('target_on'), 'la nueva fecha')
            amount = f._money_to_cents(request.form.get('amount'))
            with f._db_connection(app) as db:
                db.execute('BEGIN IMMEDIATE')
                expected = request.form.get('expected_on')
                item = next((p for p in f._commitment_occurrences(db, date.fromisoformat(expected), date.fromisoformat(expected))
                             if p['commitment_id'] == commitment_id and p['source_occurrence_on'] == source), None)
                if not item or item['is_omitted'] or item['amount_cents'] != int(request.form.get('expected_amount')):
                    raise ValueError('El recordatorio cambió. Recarga el calendario.')
                if any(p['commitment_id'] == commitment_id and p['source_occurrence_on'] != source
                       for p in f._commitment_occurrences(db, date.fromisoformat(target), date.fromisoformat(target))):
                    raise ValueError('Esta repetición ya tiene un recordatorio en esa fecha.')
                db.execute('''INSERT INTO commitment_exceptions
                    (commitment_id, occurrence_on, amount_cents, moved_to, created_at) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(commitment_id, occurrence_on) DO UPDATE SET amount_cents = excluded.amount_cents, moved_to = excluded.moved_to''',
                           (commitment_id, source, amount, '' if source == target else target, f._now()))
                if item['frequency'] == 'once' and 'title' in request.form:
                    title = f._clean_text(request.form.get('title'), 'a quién le debes', limit=120)
                    db.execute('UPDATE recurring_commitments SET title = ? WHERE id = ?', (title, commitment_id))
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return finish(str(exc), True)
        return finish('Recordatorio actualizado.')

    @app.post('/calendario/cuotas/<int:installment_id>/editar')
    def edit_calendar_installment(installment_id):
        try:
            amount = f._money_to_cents(request.form.get('amount'))
            target = f._parse_date(request.form.get('target_on'), 'la fecha')
            with f._db_connection(app) as db:
                db.execute('BEGIN IMMEDIATE')
                p = next((p for p in f._debt_installments(db) if p['id'] == installment_id and p['schedule_type'] != 'single'), None)
                if not p or not p['remaining_cents'] or p['due_on'] != request.form.get('expected_on') or p['amount_cents'] != int(request.form.get('expected_amount')):
                    raise ValueError('La cuota cambió o ya está pagada. Recarga el calendario.')
                if amount < p['paid_cents']:
                    raise ValueError('El monto no puede ser menor que lo ya abonado.')
                pending = [v for v in f._debt_installments(db) if v['debt_id'] == p['debt_id'] and v['remaining_cents']]
                delta = amount - p['amount_cents']
                others = [v for v in pending if v['id'] != installment_id]
                if delta > sum(v['remaining_cents'] for v in others):
                    raise ValueError('El monto no puede superar lo que falta por pagar.')
                if delta < 0:
                    if not others:
                        raise ValueError('La última cuota debe cubrir el saldo pendiente.')
                    db.execute('UPDATE scheduled_debt_payments SET amount_cents = amount_cents - ? WHERE id = ?', (delta, others[-1]['id']))
                elif delta > 0:
                    for other in reversed(others):
                        reduction = min(delta, other['remaining_cents'])
                        if other['amount_cents'] == reduction:
                            db.execute('DELETE FROM scheduled_debt_payments WHERE id = ?', (other['id'],))
                        else:
                            db.execute('UPDATE scheduled_debt_payments SET amount_cents = amount_cents - ? WHERE id = ?', (reduction, other['id']))
                        delta -= reduction
                        if not delta:
                            break
                db.execute('UPDATE scheduled_debt_payments SET amount_cents = ?, due_on = ? WHERE id = ?', (amount, target, installment_id))
                f._sync_debt_due_on(db, p['debt_id'])
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            return finish(str(exc) if isinstance(exc, ValueError) else 'La fecha ya está ocupada.', True)
        return finish('Cuota actualizada.')
