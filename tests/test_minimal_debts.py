from datetime import date
import pytest
from conftest import finance, post, add_debt, add_recurring
from minimal_debts import view, INCOME


def data(app, month='2026-10'):
    with finance._db_connection(app) as db:
        return view(db, finance, date(2026, 10, 5), month)


def test_undated_todo_stays_undated_after_initialization(environment):
    app, client = environment
    post(client, '/deudas', debt_type='single', name='Persona', amount='500')
    result = data(app)
    assert len(result['singles']) == 1 and not result['singles'][0]['due_on']
    assert result['stats'][0]['cents'] == 0 and not result['all_payments']
    finance._init_db(app)
    with finance._db_connection(app) as db:
        assert not db.execute('SELECT 1 FROM scheduled_debt_payments').fetchone()


def test_complete_single_deletes_without_history_or_restore(environment):
    app, client = environment
    debt, _ = add_debt(app)
    assert post(client, f'/pendientes/{debt}/completar', completed='1').json['ok']
    assert not data(app)['singles'] and not data(app)['all_payments']
    assert data(app)['stats'][0]['cents'] == 0
    assert 'Completadas' not in client.get('/deudas').get_data(as_text=True)
    assert post(client, f'/pendientes/{debt}/completar', completed='0').status_code == 409
    assert post(client, f'/pendientes/{debt}/completar').status_code == 409
    with finance._db_connection(app) as db:
        assert not db.execute('SELECT 1 FROM debts WHERE id = ?', (debt,)).fetchone()
        assert not db.execute('SELECT 1 FROM scheduled_debt_payments').fetchone()
        assert not db.execute('SELECT 1 FROM movements').fetchone()
        assert not db.execute('SELECT 1 FROM movement_allocations').fetchone()
    assert client.post(f'/pendientes/{debt}/completar').status_code == 400


def test_complete_recurring_removes_only_one_date_without_completed_history(environment):
    app, client = environment
    reminder = add_recurring(app)
    original = data(app)
    for _ in range(2):
        assert post(client, f'/recordatorios/{reminder}/completar', occurrence_on='2026-10-05').json['ok']
    result = data(app)
    assert len(result['payments']) == 3
    assert len(result['recurring_debts']) == 1
    assert not any(p['completed'] for p in result['all_payments'])
    assert result['stats'] == original['stats']
    assert post(client, f'/recordatorios/{reminder}/completar', occurrence_on='2026-10-05', completed='0').status_code == 409
    assert len(data(app)['payments']) == 3
    assert post(client, f'/recordatorios/{reminder}/completar', occurrence_on='2026-10-06').status_code == 409
    html = client.get('/?month=2026-10').get_data(as_text=True)
    assert f'id="payment-r-{reminder}-2026-10-05"' not in html
    assert f'id="payment-r-{reminder}-2026-10-12"' in html
    with finance._db_connection(app) as db:
        assert not db.execute('SELECT 1 FROM movements').fetchone()


def test_completing_single_with_existing_payments_preserves_financial_history(environment):
    app, client = environment
    debt, _ = add_debt(app)
    post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        history = {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table}')]
                   for table in ('movements', 'movement_allocations')}
    assert post(client, f'/pendientes/{debt}/completar').json['ok']
    assert not data(app)['singles'] and not data(app)['all_payments']
    with finance._db_connection(app) as db:
        for table, rows in history.items():
            assert [tuple(r) for r in db.execute(f'SELECT * FROM {table}')] == rows
        assert db.execute('SELECT deleted_at FROM debts WHERE id = ?', (debt,)).fetchone()[0]


@pytest.mark.parametrize('amount,exceeded', [(INCOME-1, False), (INCOME, False), (INCOME+1, True)])
def test_income_threshold_and_fortnight_boundary(environment, amount, exceeded):
    app, _ = environment
    add_debt(app, (amount,), ('2026-10-15',))
    add_debt(app, (100,), ('2026-10-16',))
    stats = data(app)['stats']
    assert stats[1]['cents'] == amount and stats[1]['exceeded'] == exceeded
    assert stats[2]['cents'] == 100
    assert stats[0]['cents'] == amount+100


def test_moving_reminder_across_months_counts_once_preserves_completion_identity(environment):
    app, client = environment
    reminder = add_recurring(app)
    assert post(client, f'/recordatorios/{reminder}/editar-fecha', occurrence_on='2026-10-05',
                expected_on='2026-10-05', expected_amount='10000', target_on='2026-11-01', amount='150').json['ok']
    assert data(app)['stats'][0]['cents'] == 30000
    november = data(app, '2026-11')
    moved = [p for p in november['payments'] if p['source_occurrence_on'] == '2026-10-05']
    assert len(moved) == 1 and moved[0]['amount_cents'] == 15000
    assert post(client, f'/recordatorios/{reminder}/completar', occurrence_on='2026-10-05').json['ok']
    assert not [p for p in data(app, '2026-11')['payments'] if p['source_occurrence_on'] == '2026-10-05']


def test_automatic_installments_last_payment_and_progress(environment):
    app, client = environment
    post(client, '/deudas', debt_type='installments', creditor='Persona', amount='120', min_payment='50',
         schedule_type='monthly', due_on='2026-10-15')
    with finance._db_connection(app) as db:
        installments = finance._debt_installments(db)
        assert [p['amount_cents'] for p in installments] == [5000, 5000, 2000]
        debt = installments[0]['debt_id']
    before = data(app)['stats']
    post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05')
    result = data(app)
    assert result['stats'] == before
    assert result['installment_debts'][0]['paid_percent'] == 21
    assert len(result['installment_debts'][0]['payments']) == 1


def test_edit_todo_to_undated_keeps_allocated_history(environment):
    app, client = environment
    debt, _ = add_debt(app)
    post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        history = [tuple(r) for r in db.execute('SELECT * FROM movement_allocations')]
    assert post(client, f'/pendientes/{debt}/editar', name='Persona', amount='75', expected_amount='10000').json['ok']
    assert not data(app)['singles'][0]['due_on'] and data(app)['stats'][0]['cents'] == 0
    finance._init_db(app)
    with finance._db_connection(app) as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM movement_allocations')] == history
        assert db.execute('SELECT due_on FROM debts WHERE id = ?', (debt,)).fetchone()[0] == ''


def test_february_and_overdue_remain_in_original_month(environment):
    app, _ = environment
    add_debt(app, (20000,), ('2026-02-28',))
    result = data(app, '2026-02')
    assert result['stats'][2]['cents'] == 20000
    assert result['next_month'] == '2026-03'
    assert data(app)['stats'][0]['cents'] == 0
    assert len(data(app)['overdue']) == 1


def test_home_and_debts_render_no_csv_or_nested_payment_menus(environment):
    app, client = environment
    add_debt(app)
    add_recurring(app)
    for route in ('/', '/deudas'):
        html = client.get(route).get_data(as_text=True)
        assert 'payment-options' not in html and 'Ajustar saldo' not in html
        assert 'custom_schedule' not in html and 'textarea' not in html
        assert 'minimal.css' in html and 'minimal.js' in html


def test_installment_edit_redistributes_amount_and_preserves_balance(environment):
    app, client = environment
    debt, ids = add_debt(app, (5000, 5000), ('2026-10-15', '2026-10-30'))
    assert post(client, f'/calendario/cuotas/{ids[0]}/editar', amount='60', target_on='2026-10-16',
                expected_on='2026-10-15', expected_amount='5000').json['ok']
    with finance._db_connection(app) as db:
        assert [p['amount_cents'] for p in finance._debt_installments(db)] == [6000, 4000]
        assert db.execute('SELECT balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()[0] == 10000
        assert not db.execute('SELECT 1 FROM movements').fetchone()
    assert data(app)['stats'][2]['cents'] == 10000


def test_abono_submission_is_idempotent(environment):
    app, client = environment
    debt, _ = add_debt(app, (5000, 5000), ('2026-10-15', '2026-10-30'))
    token = 'payment-token-for-test'
    for _ in range(2):
        post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05', idempotency_token=token)
    with finance._db_connection(app) as db:
        assert db.execute('SELECT balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()[0] == 7500
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1


def test_stale_date_and_deleted_reminder_actions_are_rejected(environment):
    app, client = environment
    debt, _ = add_debt(app)
    assert post(client, f'/pendientes/{debt}/editar', name='Persona', amount='100', due_on='2026-10-16',
                expected_amount='10000', expected_on='2026-10-04').status_code == 409
    recurring = add_recurring(app)
    post(client, f'/compromisos/{recurring}/eliminar')
    assert post(client, f'/recordatorios/{recurring}/completar', occurrence_on='2026-10-05').status_code == 409


def test_legacy_one_off_commitment_in_future_is_visible_and_completable(environment):
    app, client = environment
    post(client, '/compromisos', title='Legacy', amount='100', frequency='once', starts_on='2027-01-05', category='Deudas')
    result = data(app)
    assert len(result['once_pending']) == 1 and result['stats'][0]['cents'] == 0
    item = result['once_pending'][0]
    assert 'Legacy' in client.get('/deudas').get_data(as_text=True)
    assert post(client, f"/recordatorios/{item['source_id']}/completar", occurrence_on='2027-01-05').json['ok']
    assert not data(app)['once_pending'] and not data(app)['all_payments']
    assert 'Completadas' not in client.get('/deudas').get_data(as_text=True)


def test_migration_backs_up_and_preserves_legacy_history(environment):
    app, client = environment
    debt, _ = add_debt(app)
    post(client, f'/deudas/{debt}/pagos', amount='100', occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        history = [tuple(r) for r in db.execute('SELECT * FROM movements')]
        for column in ('todo_completed', 'todo_amount_cents', 'todo_due_on'):
            db.execute(f'ALTER TABLE debts DROP COLUMN {column}')
    finance._init_db(app)
    result = data(app)
    assert not result['singles']
    assert 'Completadas' not in client.get('/deudas').get_data(as_text=True)
    assert result['stats'][0]['cents'] == 10000
    with finance._db_connection(app) as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM movements')] == history
    assert list(app.config['DATABASE_PATH'].parent.glob('backups/before-minimal-*.sqlite3'))


def test_omissions_and_unallocated_expenses_do_not_count_twice(environment):
    app, client = environment
    recurring = add_recurring(app)
    with finance._db_connection(app) as db:
        db.execute('INSERT INTO commitment_exceptions (commitment_id, occurrence_on, is_omitted, created_at) VALUES (?, ?, 1, ?)',
                   (recurring, '2026-10-05', '2026-10-05'))
        db.execute("INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at) VALUES ('expense','Deudas','Legacy expense',99900,'2026-10-05','2026-10-05')")
    assert data(app)['stats'][0]['cents'] == 30000


def test_series_editor_uses_first_occurrence_without_duplicate_day_fields(environment):
    app, client = environment
    recurring = add_recurring(app)
    with finance._db_connection(app) as db:
        db.execute('UPDATE recurring_commitments SET starts_on = ?, weekday = 2 WHERE id = ?', ('2026-10-06', recurring))
    assert data(app)['recurring_debts'][0]['first_on'] == '2026-10-07'
    html = client.get('/deudas').get_data(as_text=True)
    assert 'name="weekday"' not in html and 'name="month_day"' not in html


@pytest.mark.parametrize('extra,exceeded', [(0,False), (1,True)])
def test_monthly_income_comparison(environment, extra, exceeded):
    app, _ = environment
    add_debt(app, (INCOME,), ('2026-10-15',))
    add_debt(app, (INCOME+extra,), ('2026-10-16',))
    result = data(app)
    assert result['stats'][0]['exceeded'] == exceeded
    assert result['stats'][0]['cents'] == 2*INCOME+extra


def test_legacy_recurring_payment_cannot_be_restored_and_preserves_expense(environment):
    app, client = environment
    recurring = add_recurring(app)
    post(client, f'/compromisos/{recurring}/pagar', amount='100', occurred_on='2026-10-05', occurrence_on='2026-10-05')
    assert post(client, f'/recordatorios/{recurring}/completar', occurrence_on='2026-10-05', completed='0').status_code == 409
    assert not [p for p in data(app)['payments'] if p['source_occurrence_on'] == '2026-10-05']
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1


def test_home_has_no_overdue_block_and_keeps_original_month_access(environment):
    app, client = environment
    debt, _ = add_debt(app, (20000,), ('2026-02-28',))
    html = client.get('/?month=2026-10').get_data(as_text=True)
    assert 'Vencidos' not in html and 'minimal-overdue' not in html
    assert f'id="payment-s-{debt}"' not in html
    february = client.get('/?month=2026-02').get_data(as_text=True)
    assert f'id="payment-s-{debt}"' in february
    assert f'data-open="payment-s-{debt}"' in february
