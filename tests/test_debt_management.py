from datetime import date

import pytest

from conftest import finance, post, add_debt, add_recurring


def balance(app, debt):
    with finance._db_connection(app) as db:
        return db.execute("SELECT balance_cents FROM debts WHERE id = ?", (debt,)).fetchone()[0]


def test_partial_single_payment_and_liquidation_keep_history(environment):
    app, client = environment
    debt, ids = add_debt(app)
    post(client, f"/deudas/{debt}/pagos", amount="30", occurred_on="2026-10-05")
    assert balance(app, debt) == 7000
    post(client, f"/deudas/{debt}/pagos", amount="70", occurred_on="2026-10-05")
    assert balance(app, debt) == 0
    finance._init_db(app)
    with finance._db_connection(app) as db:
        assert db.execute("SELECT COUNT(*) FROM movements WHERE debt_id = ?", (debt,)).fetchone()[0] == 2
        assert sum(p["remaining_cents"] for p in finance._debt_installments(db)) == 0
        assert finance._list_debts(db)[0]["paid_cents"] == 10000
    assert 'Completadas' not in client.get('/deudas').get_data(as_text=True)
    assert client.get('/?month=2026-10').status_code == 200


def test_move_partially_paid_installment_preserves_allocations(environment):
    app, client = environment
    debt, ids = add_debt(app, dates=("2026-10-01",))
    post(client, f"/deudas/{debt}/pagos", amount="25", occurred_on="2026-10-05")
    response = post(client, f"/deudas/{debt}/cuotas/{ids[0]}/mover", expected_on="2026-10-01", target_on="2026-11-03")
    assert response.json['ok']
    with finance._db_connection(app) as db:
        item = finance._debt_installments(db)[0]
        assert (item['id'], item['due_on'], item['paid_cents'], item['remaining_cents']) == (ids[0], '2026-11-03', 2500, 7500)
    assert balance(app, debt) == 7500
    assert client.get('/?month=2026-11').status_code == 200


@pytest.mark.parametrize('target,expected', [('2026-10-12','2026-10-05'), ('2026-10-04','2026-10-05'), ('2026-10-06','2026-10-03')])
def test_move_rejects_collision_past_and_stale(environment, target, expected):
    app, client = environment
    debt, ids = add_debt(app, (5000,5000), ('2026-10-05','2026-10-12'))
    response = post(client, f"/deudas/{debt}/cuotas/{ids[0]}/mover", expected_on=expected, target_on=target)
    assert response.status_code == 409
    with finance._db_connection(app) as db:
        assert finance._debt_installments(db)[0]['due_on'] == '2026-10-05'


def test_move_recurring_after_partial_payment_and_move_again(environment):
    app, client = environment
    recurring = add_recurring(app)
    post(client, f"/compromisos/{recurring}/pagar", amount="25", occurred_on='2026-10-05', occurrence_on='2026-10-05')
    for expected, target in [('2026-10-05','2026-11-03'), ('2026-11-03','2026-11-04')]:
        response = post(client, f"/compromisos/{recurring}/mover", source_occurrence_on='2026-10-05', expected_on=expected, target_on=target)
        assert response.json['ok']
    with finance._db_connection(app) as db:
        items = finance._commitment_occurrences(db, date(2026,10,1), date(2026,11,30))
        moved = next(item for item in items if item['source_occurrence_on'] == '2026-10-05')
        assert (moved['occurrence_on'],moved['paid_cents'],moved['remaining_cents']) == ('2026-11-04',2500,7500)
        assert any(item['occurrence_on'] == '2026-10-12' for item in items)
    stale = post(client,f"/compromisos/{recurring}/mover",source_occurrence_on='2026-10-05',expected_on='2026-10-05',target_on='2026-11-05')
    assert stale.status_code == 409
    collision = post(client,f"/compromisos/{recurring}/mover",source_occurrence_on='2026-10-05',expected_on='2026-11-04',target_on='2026-11-09')
    assert collision.status_code == 409


def test_adjust_balance_preserves_original_and_real_payments(environment):
    app, client = environment
    debt, ids = add_debt(app, (5000,5000), ('2026-10-05','2026-10-12'))
    post(client, f"/deudas/{debt}/pagos", amount='20', occurred_on='2026-10-05')
    for expected, target in [(8000,'120'),(12000,'25')]:
        response = post(client,f"/deudas/{debt}/ajustar-saldo",expected_balance=expected,balance=target,reason='Negotiated balance')
        assert response.json['ok']
    with finance._db_connection(app) as db:
        items = finance._debt_installments(db)
        assert sum(item['remaining_cents'] for item in items) == 2500
        assert items[0]['paid_cents'] == 2000
        debt_row = finance._list_debts(db)[0]
        assert debt_row['original_cents'] == 10000
        assert debt_row['paid_cents'] == 2000
        assert len(debt_row['adjustments']) == 2
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1


def test_zero_adjustment_and_reopening_need_date(environment):
    app, client = environment
    debt, ids = add_debt(app)
    assert post(client,f"/deudas/{debt}/ajustar-saldo",expected_balance=10000,balance='0',reason='Forgiven').json['ok']
    assert balance(app,debt) == 0
    failed = post(client,f"/deudas/{debt}/ajustar-saldo",expected_balance=0,balance='50',reason='New balance')
    assert failed.status_code == 409
    assert balance(app,debt) == 0
    assert post(client,f"/deudas/{debt}/ajustar-saldo",expected_balance=0,balance='50',reason='New balance',target_on='2026-10-15').json['ok']
    assert balance(app,debt) == 5000


@pytest.mark.parametrize('fields', [dict(balance='50',reason=''),dict(balance='-10',reason='Wrong'),dict(balance='50',reason='Changed',expected_balance=1)])
def test_adjustment_failure_is_atomic(environment, fields):
    app, client = environment
    debt, ids = add_debt(app)
    data = dict(expected_balance=10000, **{k:v for k,v in fields.items() if k != 'expected_balance'})
    data.update({k:v for k,v in fields.items() if k == 'expected_balance'})
    assert post(client,f"/deudas/{debt}/ajustar-saldo",**data).status_code == 409
    assert balance(app,debt) == 10000
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM debt_balance_adjustments').fetchone()[0] == 0


def test_capital_payment_liquidation_retains_history(environment):
    app, client = environment
    debt, ids = add_debt(app,(5000,5000),('2026-10-05','2026-10-12'))
    post(client,f"/deudas/{debt}/pagos",amount='100',occurred_on='2026-10-05',capital_payment='1')
    assert balance(app,debt) == 0
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM movements WHERE debt_id = ?', (debt,)).fetchone()[0] == 1
        assert sum(p['remaining_cents'] for p in finance._debt_installments(db)) == 0


def test_paid_card_cannot_be_moved_and_payment_cannot_be_deleted(environment):
    app, client = environment
    debt, ids = add_debt(app)
    post(client,f"/deudas/{debt}/pagos",amount='100',occurred_on='2026-10-05')
    assert post(client,f"/deudas/{debt}/cuotas/{ids[0]}/mover",expected_on='2026-10-05',target_on='2026-10-08').status_code == 409
    with finance._db_connection(app) as db:
        movement = db.execute('SELECT id FROM movements').fetchone()[0]
    post(client,f"/movimientos/{movement}/eliminar")
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1


def test_grouped_cards_have_individual_operations(environment):
    app, client = environment
    first, ids = add_debt(app,creditor_type='family')
    second, other_ids = add_debt(app,creditor_type='family')
    html = client.get('/?month=2026-10').get_data(as_text=True)
    assert 'payment-group-members' not in html
    for debt, item in [(first,ids[0]),(second,other_ids[0])]:
        assert f'id="payment-s-{debt}"' in html
        assert f'/pendientes/{debt}/editar' in html
        assert f'/deudas/{debt}/ajustar-saldo' not in html


def test_csrf_authentication_and_removed_page(environment):
    app, client = environment
    assert client.post('/deudas/1/ajustar-saldo',data={'balance':'0','reason':'X'}).status_code == 400
    app.config['AUTH_MODE'] = 'shared_login'
    with client.session_transaction() as session:
        session.pop('auth_user')
    assert post(client,'/deudas/1/ajustar-saldo',balance='0',reason='X').status_code == 302
    with client.session_transaction() as session:
        session['auth_user']='admin'
    assert client.get('/concientizacion').status_code == 404
    for route in ['/', '/deudas','/ingresos']:
        assert 'Concientización' not in client.get(route).get_data(as_text=True)


def test_edit_metadata_preserves_adjustment_and_schedule(environment):
    app, client = environment
    debt, ids = add_debt(app)
    assert post(client,f'/deudas/{debt}/ajustar-saldo',balance='150',expected_balance=10000,reason='Agreed balance').json['ok']
    post(client,f'/deudas/{debt}/editar',metadata_only='1',name='Updated name',creditor='Updated creditor',notes='Updated notes')
    with finance._db_connection(app) as db:
        row = db.execute('SELECT name,creditor,original_cents,balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()
        assert tuple(row) == ('Updated name','Updated name',10000,15000)
        assert finance._debt_installments(db)[0]['amount_cents'] == 15000
    post(client,f'/deudas/{debt}/editar',name='Legacy form',creditor='Updated creditor',amount='100',due_on='2026-10-15',schedule_type='single')
    assert balance(app,debt) == 15000
    assert client.get('/deudas').status_code == 200


def test_settling_legacy_overscheduled_debt_removes_pending_cards(environment):
    app, client = environment
    debt, ids = add_debt(app,(10000,10000),('2026-10-05','2026-10-12'))
    with finance._db_connection(app) as db:
        db.execute('UPDATE debts SET balance_cents = 10000 WHERE id = ?', (debt,))
    post(client,f'/deudas/{debt}/pagos',amount='100',occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        assert sum(p['remaining_cents'] for p in finance._debt_installments(db)) == 0
        assert db.execute('SELECT COUNT(*) FROM movements WHERE debt_id = ?', (debt,)).fetchone()[0] == 1
