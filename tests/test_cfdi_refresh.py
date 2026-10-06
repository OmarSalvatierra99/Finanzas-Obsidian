import imaplib

from conftest import finance, post
import import_gmail_xml as importer


XML = b'<Comprobante TipoDeComprobante="N" Moneda="MXN" Total="1200.00" Fecha="2026-10-05T10:00:00"><Emisor Nombre="Test issuer"/><TimbreFiscalDigital UUID="TEST-UUID-001"/></Comprobante>'


def test_cfdi_refresh_imports_deduplicates_and_rejects_invalid(environment, monkeypatch):
    app, client = environment
    monkeypatch.setattr(finance, '_xml_gmail_settings', lambda db, app: {'password':'test', 'email':'test'})
    monkeypatch.setattr(importer, 'gmail_xml_attachments', lambda *args: iter([('new.xml',XML),('duplicate.xml',XML),('invalid.xml',b'invalid')]))
    for _ in range(2):
        response = post(client,'/ingresos/importar-xml')
        assert response.status_code == 302
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM xml_imports').fetchone()[0] == 1
        assert db.execute("SELECT SUM(amount_cents) FROM movements WHERE kind = 'income'").fetchone()[0] == 120000
    html = client.get('/ingresos').get_data(as_text=True)
    assert 'Actualizar CFDI' in html and 'duplicado(s)' in html and 'invalido(s)' in html


def test_missing_credentials_and_gmail_error_leave_income_unchanged(environment, monkeypatch):
    app, client = environment
    monkeypatch.setattr(finance, '_xml_gmail_settings', lambda db, app: {'password':''})
    post(client,'/ingresos/importar-xml')
    assert 'Configura primero' in client.get('/ingresos').get_data(as_text=True)
    monkeypatch.setattr(finance, '_xml_gmail_settings', lambda db, app: {'password':'test'})
    def fail(*args, **kwargs):
        raise imaplib.IMAP4.error('simulated failure')
    monkeypatch.setattr(importer,'sync_cfdi',fail)
    post(client,'/ingresos/importar-xml')
    assert 'No se pudo leer Gmail' in client.get('/ingresos').get_data(as_text=True)
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 0


def test_same_uuid_different_xml_and_dry_run(environment):
    app, _ = environment
    first = importer.parse_cfdi_xml(XML, 'original.xml')
    variant = importer.parse_cfdi_xml(XML.replace(b'Test issuer', b'Another issuer'), 'changed.xml')
    with finance._db_connection(app) as db:
        assert importer.import_cfdi(db, first, dry_run=True)
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 0
        assert importer.import_cfdi(db, first)
        assert not importer.import_cfdi(db, variant)
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 1


def test_payroll_income_total_and_history_exclude_manual_income(environment):
    app, client = environment
    with finance._db_connection(app) as db:
        for index in range(105):
            xml = XML.replace(b'TEST-UUID-001', f'PAYROLL-{index:03d}'.encode())
            assert importer.import_cfdi(db, importer.parse_cfdi_xml(xml, f'{index}.xml'))
        db.executemany(
            "INSERT INTO movements (kind, category, description, amount_cents, occurred_on, created_at) "
            "VALUES ('income', 'Otros', 'Extra fuera de Nómina', 999000, '2026-10-05', '2026-10-05')",
            [() for _ in range(101)],
        )
    response = client.get('/ingresos')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Total pagado por Nómina' in html and '$126,000.00' in html
    assert html.count('class="date-cell"') == 100
    assert 'Extra fuera de Nómina' not in html
    assert 'Agregar ingreso extra' not in html and 'extra-income-title' not in html
    assert 'Nómina automática' not in html
    assert 'Actualizar CFDI' in html
    with finance._db_connection(app) as db:
        assert db.execute('SELECT COUNT(*) FROM movements').fetchone()[0] == 206
