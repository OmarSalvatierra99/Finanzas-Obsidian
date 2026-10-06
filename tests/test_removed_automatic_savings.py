from datetime import date

from conftest import finance, post, add_debt


def test_removed_savings_cannot_be_configured_and_does_not_reduce_available_income(environment):
    app, client = environment
    debt, _ = add_debt(app)
    with finance._db_connection(app) as db:
        db.executemany(
            'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)',
            [('automatic_savings_percent', '50', '2026-10-05'),
             ('automatic_savings_starts_on', '2026-10-01', '2026-10-05')],
        )
        db.execute("INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at) "
                   "VALUES ('income', 'Nómina', 'Nómina', 100000, '2026-10-05', '2026-10-05')")
    post(client, f'/deudas/{debt}/pagos', amount='25', occurred_on='2026-10-05')
    with finance._db_connection(app) as db:
        snapshot = finance._financial_snapshot(db, date(2026, 10, 5))
        assert snapshot['income_cents'] == 100000
        assert snapshot['balance_cents'] == 97500
        assert snapshot['free_cents'] == 90000
        summary = finance._summary(db, '2026-10')
        assert summary['income'] == '$1,000.00' and summary['balance_cents'] == 97500
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 2
    response = client.get('/configuracion')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Ahorro automático' not in html and 'Guardar ahorro automático' not in html
    assert 'ahorro-automatico' not in html
    assert 'Cuenta de Gmail' in html
    assert post(client, '/configuracion/ahorro-automatico', percent='10').status_code == 404
    assert client.get('/ahorros').location.endswith('/configuracion')
