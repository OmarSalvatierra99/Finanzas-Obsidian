from conftest import finance, post, add_debt


def test_bank_creation_controls_are_removed(environment):
    app, client = environment
    response = client.get('/deudas')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'new-banco-azteca-debt-dialog' not in html
    assert 'Agregar Banco Azteca' not in html
    fields = app.jinja_env.get_template('_debt_form.html').make_module({
        'commitment_frequencies': {'monthly': 'Mensual'}, 'today': '2026-10-05',
    }).new_debt_fields()
    assert 'banco_azteca' not in fields


def test_retired_bank_creation_is_rejected_without_changing_history(environment):
    app, client = environment
    debt, ids = add_debt(app, creditor_type='bank')
    for fields in (
        dict(debt_type='banco_azteca', principal='100', weekly_payment='20',
             installment_count='10', due_on='2026-10-08'),
        dict(debt_type='single', creditor_type='bank', creditor='Bank',
             amount='100', due_on='2026-10-08'),
    ):
        response = post(client, '/deudas', **fields)
        assert response.status_code == 302
        with finance._db_connection(app) as db:
            assert db.execute('SELECT COUNT(*) FROM debts').fetchone()[0] == 1
            assert db.execute('SELECT balance_cents FROM debts WHERE id = ?', (debt,)).fetchone()[0] == 10000
            assert db.execute('SELECT id FROM scheduled_debt_payments WHERE debt_id = ?', (debt,)).fetchone()[0] == ids[0]
    assert client.get('/deudas').status_code == 200
