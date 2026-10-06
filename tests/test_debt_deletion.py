from datetime import date

import pytest

from conftest import finance, post, add_debt
from send_reminders import due_debts


@pytest.mark.parametrize('history', ['partial', 'adjustment', 'settled'])
@pytest.mark.parametrize('schedule', ['single', 'installments'])
def test_delete_debt_with_history_preserves_records_and_removes_all_pending_views(environment, history, schedule):
    app, client = environment
    if schedule == 'single':
        debt, ids = add_debt(app)
    else:
        debt, ids = add_debt(app, (5000, 5000), ('2026-10-05', '2026-10-15'))
    if history == 'adjustment':
        assert post(client, f'/deudas/{debt}/ajustar-saldo', balance='150', expected_balance='10000', reason='Acuerdo').json['ok']
    else:
        post(client, f'/deudas/{debt}/pagos', amount='100' if history == 'settled' else '25', occurred_on='2026-10-05')
    html = client.get('/deudas').get_data(as_text=True)
    if history == 'settled' and schedule == 'single':
        assert 'Completadas' not in html
    else:
        assert f'action="/deudas/{debt}/eliminar"' in html
    with finance._db_connection(app) as db:
        snapshots = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
                     for table in ('movements', 'movement_allocations', 'scheduled_debt_payments', 'debt_balance_adjustments')}
        balance = db.execute('SELECT balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()[0]
    assert post(client, f'/deudas/{debt}/eliminar').status_code == 302
    # Stale forms must not change a removed debt or its existing installments.
    post(client, f'/deudas/{debt}/editar', metadata_only='1', name='Stale', creditor='Stale')
    post(client, f'/deudas/{debt}/pagos', amount='10', occurred_on='2026-10-05')
    assert post(client, f'/deudas/{debt}/ajustar-saldo', balance='200', expected_balance=str(balance), reason='Stale').status_code == 409
    assert post(client, f'/deudas/{debt}/cuotas/{ids[0]}/mover', expected_on='2026-10-05', target_on='2026-10-07').status_code == 409
    post(client, f'/deudas/{debt}/cuotas', due_on='2026-10-08', amount='10')
    with finance._db_connection(app) as db:
        row = db.execute('SELECT deleted_at, balance_cents, name FROM debts WHERE id = ?', (debt,)).fetchone()
        assert row['deleted_at'] and row['balance_cents'] == balance and row['name'] == 'Test'
        for table, snapshot in snapshots.items():
            assert [tuple(row) for row in db.execute(f'SELECT * FROM {table}')] == snapshot
        assert not finance._list_debts(db)
        assert not finance._debt_installments(db)
        assert finance._financial_snapshot(db, date(2026, 10, 5))['pending_debts_cents'] == 0
        assert not due_debts(db, date(2026, 10, 5))
    finance._init_db(app)
    with finance._db_connection(app) as db:
        assert not finance._list_debts(db) and not finance._debt_installments(db)
    assert f'id="deuda-{debt}"' not in client.get('/deudas').get_data(as_text=True)
    assert client.get('/?month=2026-10').status_code == 200


def test_delete_unpaid_debt_keeps_unrelated_debts_and_requires_csrf(environment):
    app, client = environment
    debt, _ = add_debt(app)
    unrelated, _ = add_debt(app)
    assert client.post(f'/deudas/{debt}/eliminar').status_code == 400
    post(client, f'/deudas/{debt}/eliminar')
    with finance._db_connection(app) as db:
        assert not db.execute('SELECT 1 FROM debts WHERE id = ?', (debt,)).fetchone()
        assert [item['id'] for item in finance._list_debts(db)] == [unrelated]
