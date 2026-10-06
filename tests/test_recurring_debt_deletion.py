from datetime import date

from conftest import finance, post, add_recurring


def test_debts_page_removes_calendar_shortcut_and_budget_adjustment(environment):
    app, client = environment
    debt = add_recurring(app)
    response = client.get('/deudas')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Abrir calendario y mover pagos' not in html
    assert 'Ajustar presupuesto' not in html
    assert 'debt-budget-dialog' not in html
    assert f'action="/compromisos/{debt}/eliminar"' in html
    assert post(client, '/deudas/ajustar-presupuesto').status_code == 404
    with finance._db_connection(app) as db:
        assert db.execute('SELECT amount_cents FROM recurring_commitments WHERE id = ?', (debt,)).fetchone()[0] == 10000


def test_delete_unpaid_recurring_debt_removes_exceptions(environment):
    app, client = environment
    debt = add_recurring(app)
    post(client, f'/compromisos/{debt}/mover', source_occurrence_on='2026-10-05',
         expected_on='2026-10-05', target_on='2026-10-06')
    html = client.get('/deudas').get_data(as_text=True)
    assert f'action="/compromisos/{debt}/eliminar"' in html
    assert post(client, f'/compromisos/{debt}/eliminar').status_code == 302
    with finance._db_connection(app) as db:
        assert not db.execute('SELECT 1 FROM recurring_commitments WHERE id = ?', (debt,)).fetchone()
        assert not db.execute('SELECT 1 FROM commitment_exceptions WHERE commitment_id = ?', (debt,)).fetchone()


def test_delete_paid_recurring_debt_preserves_history_and_cannot_resume(environment):
    app, client = environment
    debt = add_recurring(app)
    post(client, f'/compromisos/{debt}/pagar', amount='25', occurred_on='2026-10-05', occurrence_on='2026-10-05')
    with finance._db_connection(app) as db:
        movements = [tuple(row) for row in db.execute('SELECT * FROM movements')]
        allocations = [tuple(row) for row in db.execute('SELECT * FROM movement_allocations')]
    post(client, f'/compromisos/{debt}/eliminar')
    post(client, f'/compromisos/{debt}/estado', active='1', starts_on='2026-10-06')
    with finance._db_connection(app) as db:
        row = db.execute('SELECT * FROM recurring_commitments WHERE id = ?', (debt,)).fetchone()
        assert row['deleted_at'] and not row['is_active']
        assert [tuple(row) for row in db.execute('SELECT * FROM movements')] == movements
        assert [tuple(row) for row in db.execute('SELECT * FROM movement_allocations')] == allocations
        assert not finance._list_recurring_debts(db)
        assert not finance._commitment_occurrences(db, date(2026, 10, 1), date(2026, 11, 30))
    assert f'deuda-recurrente-{debt}"' not in client.get('/deudas').get_data(as_text=True)


def test_delete_fortnightly_pair_preserves_payments_from_either_member(environment):
    app, client = environment
    first = add_recurring(app)
    second = add_recurring(app)
    unrelated = add_recurring(app)
    with finance._db_connection(app) as db:
        for debt, day in ((first, 15), (second, 30)):
            db.execute("UPDATE recurring_commitments SET frequency = 'monthly', month_day = ?, weekday = NULL WHERE id = ?", (day, debt))
    post(client, f'/compromisos/{second}/pagar', amount='25', occurred_on='2026-10-05', occurrence_on='2026-10-30')
    post(client, f'/compromisos/{first}/eliminar', pair_id=str(second))
    with finance._db_connection(app) as db:
        assert db.execute("SELECT COUNT(*) FROM recurring_commitments WHERE id IN (?, ?) AND deleted_at != '' AND is_active = 0", (first, second)).fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM movement_allocations').fetchone()[0] == 1
        assert [item['id'] for item in finance._list_recurring_debts(db)] == [unrelated]


def test_delete_rejects_unrelated_pair_and_missing_csrf(environment):
    app, client = environment
    first, second = add_recurring(app), add_recurring(app)
    post(client, f'/compromisos/{first}/eliminar', pair_id=str(second))
    assert client.post(f'/compromisos/{first}/eliminar').status_code == 400
    with finance._db_connection(app) as db:
        assert len(finance._list_recurring_debts(db)) == 2
