"""Tour do primeiro acesso: guarda onde parou e se terminou, por pessoa."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def test_tour_pausa_e_termina():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'tour@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        assert 'tour' not in c.get('/api/auth/me').json()['user']  # primeiro acesso: o tour abre sozinho
        c.put('/api/me/tour', json={'step': 3})
        t = c.get('/api/auth/me').json()['user']['tour']; assert t['step'] == 3 and not t['done_at']
        c.put('/api/me/tour', json={'step': 8, 'done': True})
        assert c.get('/api/auth/me').json()['user']['tour']['done_at']
        c.put('/api/me/tour', json={'step': 2})  # viu de novo e pausou: continua marcado como visto
        assert c.get('/api/auth/me').json()['user']['tour']['done_at']
        assert c.put('/api/me/tour', json={'step': 99}).status_code == 422
