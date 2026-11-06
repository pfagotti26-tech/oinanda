import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def test_email_ja_cadastrado():
    with TestClient(app, base_url='https://testserver') as c:
        assert c.get('/api/auth/email-check?email=nova@cad.com').json() == {'exists': False}
        assert c.post('/api/auth/signup', json={'name': 'Nova', 'email': 'nova@cad.com', 'password': 'senha-forte-1', 'company': 'Cad', 'segment': 'studio'}).status_code == 200
        c.cookies.clear()
        assert c.get('/api/auth/email-check?email=NOVA@cad.com ').json() == {'exists': True}
        r = c.post('/api/auth/signup', json={'name': 'Nova', 'email': 'nova@cad.com', 'password': 'senha-forte-1', 'company': 'Cad', 'segment': 'studio'})
        assert r.status_code == 409 and 'Entre com ele' in r.json()['detail']
