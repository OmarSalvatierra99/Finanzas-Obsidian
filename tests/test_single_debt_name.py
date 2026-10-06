from conftest import finance, post, add_debt


def test_create_single_debt_with_only_person_name(environment):
    app, client = environment
    response = post(client, '/deudas', debt_type='single', name='Persona de prueba',
                    amount='100', due_on='2026-10-06')
    assert response.status_code == 302
    with finance._db_connection(app) as db:
        row = db.execute('SELECT name, creditor, original_cents FROM debts').fetchone()
        assert tuple(row) == ('Persona de prueba', 'Persona de prueba', 10000)


def test_edit_single_debt_without_creditor_preserves_payments_and_schedule(environment):
    app, client = environment
    debt, _ = add_debt(app)
    post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        snapshots = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
                     for table in ('movements', 'movement_allocations', 'scheduled_debt_payments')}
    response = post(client, f'/deudas/{debt}/editar', metadata_only='1',
                    name='Nuevo nombre', notes='Una nota')
    assert response.status_code == 302
    with finance._db_connection(app) as db:
        row = db.execute('SELECT name, creditor, original_cents, balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()
        assert tuple(row) == ('Nuevo nombre', 'Nuevo nombre', 10000, 7500)
        for table, snapshot in snapshots.items():
            assert [tuple(row) for row in db.execute(f'SELECT * FROM {table}')] == snapshot


def test_single_debt_editor_has_one_person_field_in_debts_and_calendar(environment):
    app, client = environment
    single, _ = add_debt(app)
    installments, _ = add_debt(app, amounts=(5000, 5000), dates=('2026-10-05', '2026-10-15'))
    with app.test_request_context():
        macro = app.jinja_env.get_template('_debt_actions.html').make_module({
            'csrf_token': lambda: 'test-token',
        }).edit_form
        single_html = macro(single, 'Old label', 'Person', '', single_payment=True)
        assert 'name="name"' in single_html and 'value="Person"' in single_html
        assert 'name="creditor"' not in single_html and '<label>Acreedor' not in single_html
        assert 'name="creditor"' in macro(installments, 'Debt', 'Person', '')
    assert client.get('/deudas').status_code == 200
    assert client.get('/?month=2026-10').status_code == 200
