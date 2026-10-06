from werkzeug.security import generate_password_hash

from conftest import finance


def test_configured_login_uses_password_hash_and_protects_private_pages(environment, monkeypatch):
    app, client = environment
    monkeypatch.setenv('AUTH_USERNAME', 'demo')
    monkeypatch.setenv('AUTH_PASSWORD_HASH', generate_password_hash('synthetic-test-password'))
    app.config['AUTH_USER'] = finance._build_auth_user()
    app.config['AUTH_MODE'] = 'shared_login'
    with client.session_transaction() as session:
        session.pop('auth_user')
    assert client.get('/deudas').status_code == 302
    wrong = client.post('/login', data={'csrf_token': 'test-token', 'username': 'demo', 'password': 'wrong'})
    assert wrong.status_code == 200
    assert client.get('/deudas').status_code == 302
    correct = client.post('/login', data={'csrf_token': 'test-token', 'username': 'demo', 'password': 'synthetic-test-password'})
    assert correct.status_code == 302
    assert client.get('/deudas').status_code == 200
    assert client.get('/descargas/local.zip').status_code == 404


def test_missing_credentials_cannot_authenticate(environment, monkeypatch):
    app, client = environment
    monkeypatch.delenv('AUTH_PASSWORD_HASH', raising=False)
    app.config['AUTH_USER'] = finance._build_auth_user()
    app.config['AUTH_MODE'] = 'shared_login'
    assert app.config['AUTH_USER'] is None
    assert client.get('/deudas').status_code == 302
    response = client.post('/login', data={'csrf_token': 'test-token', 'username': 'admin', 'password': 'anything'})
    assert response.status_code == 200
    assert client.get('/deudas').status_code == 302
