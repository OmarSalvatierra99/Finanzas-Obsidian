from datetime import date

from conftest import finance, post, add_debt, add_recurring


def test_simple_single_edit_changes_remaining_amount_without_rewriting_payments(environment):
    app, client = environment
    debt, _ = add_debt(app)
    post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        history = [tuple(row) for row in db.execute('SELECT * FROM movements')]
        allocations = [tuple(row) for row in db.execute('SELECT * FROM movement_allocations')]
    response = post(client, f'/deudas/{debt}/editar-simple', name='Persona', amount='120', expected_balance='7500')
    assert response.json['ok']
    with finance._db_connection(app) as db:
        row = db.execute('SELECT name, creditor, original_cents, balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()
        assert tuple(row) == ('Persona', 'Persona', 10000, 12000)
        assert [tuple(row) for row in db.execute('SELECT * FROM movements')] == history
        assert [tuple(row) for row in db.execute('SELECT * FROM movement_allocations')] == allocations
        assert sum(item['remaining_cents'] for item in finance._debt_installments(db)) == 12000
    assert post(client, f'/deudas/{debt}/editar-simple', name='Stale', amount='200', expected_balance='7500').status_code == 409


def test_single_pay_button_pays_full_balance_once(environment):
    app, client = environment
    debt, _ = add_debt(app)
    for _ in range(2):
        assert post(client, f'/deudas/{debt}/pagar-completo', expected_balance='10000').status_code == 302
    with finance._db_connection(app) as db:
        assert db.execute('SELECT balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM movements WHERE debt_id = ?', (debt,)).fetchone()[0] == 1
        assert db.execute('SELECT SUM(amount_cents) FROM movements WHERE debt_id = ?', (debt,)).fetchone()[0] == 10000
    assert client.post(f'/deudas/{debt}/pagar-completo', data={'expected_balance': '0'}).status_code == 400


def test_single_actions_reject_installment_debts(environment):
    app, client = environment
    debt, _ = add_debt(app, (5000, 5000), ('2026-10-05', '2026-10-15'))
    assert post(client, f'/deudas/{debt}/editar-simple', name='X', amount='200', expected_balance='10000').status_code == 409
    post(client, f'/deudas/{debt}/pagar-completo', expected_balance='10000')
    with finance._db_connection(app) as db:
        assert db.execute('SELECT balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()[0] == 10000
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 0


def test_edit_recurring_calendar_changes_weekday_and_keeps_payments(environment):
    app, client = environment
    debt = add_recurring(app)
    post(client, f'/compromisos/{debt}/pagar', amount='25', occurred_on='2026-10-05', occurrence_on='2026-10-05')
    with finance._db_connection(app) as db:
        payments = [tuple(row) for row in db.execute('SELECT * FROM movement_allocations')]
    post(client, f'/compromisos/{debt}/editar', calendar_edit='1', title='Semanal', amount='100',
         frequency='weekly', weekday='2', starts_on='2026-10-06', category='Deudas')
    with finance._db_connection(app) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM movement_allocations')] == payments
        assert [item['occurrence_on'] for item in finance._commitment_occurrences(db, date(2026, 10, 6), date(2026, 10, 15))] == ['2026-10-07', '2026-10-14']


def test_recurring_calendar_can_change_to_and_from_fortnightly(environment):
    app, client = environment
    debt = add_recurring(app)
    fields = dict(calendar_edit='1', title='Quincenal', amount='100', starts_on='2026-10-05', category='Deudas')
    post(client, f'/compromisos/{debt}/editar', **fields, frequency='fortnightly')
    with finance._db_connection(app) as db:
        grouped = finance._list_recurring_debts(db)[0]
        pair = grouped['pair_id']
        assert pair
        assert [item['occurrence_on'] for item in finance._commitment_occurrences(db, date(2026, 10, 5), date(2026, 10, 31))] == ['2026-10-15', '2026-10-30']
    post(client, f'/compromisos/{pair}/pagar', amount='25', occurred_on='2026-10-05', occurrence_on='2026-10-30')
    post(client, f'/compromisos/{debt}/editar', **fields, pair_id=str(pair), frequency='monthly', month_day='20')
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM movement_allocations').fetchone()[0] == 1
        assert db.execute('SELECT deleted_at FROM recurring_commitments WHERE id = ?', (pair,)).fetchone()[0]
        assert len(finance._list_recurring_debts(db)) == 1
        assert [item['occurrence_on'] for item in finance._commitment_occurrences(db, date(2026, 10, 5), date(2026, 10, 31))] == ['2026-10-20']


def test_invalid_recurring_calendar_does_not_modify_debt(environment):
    app, client = environment
    debt = add_recurring(app)
    post(client, f'/compromisos/{debt}/editar', calendar_edit='1', title='Invalid', amount='100',
         frequency='monthly', month_day='32', starts_on='2026-10-05', category='Deudas')
    with finance._db_connection(app) as db:
        row = db.execute('SELECT title, frequency FROM recurring_commitments WHERE id = ?', (debt,)).fetchone()
        assert tuple(row) == ('Weekly test', 'weekly')
