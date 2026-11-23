import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def _client(): return TestClient(app, base_url='https://testserver')

def test_setup_status_skip_hide():
    import uuid
    c = _client()
    r = c.post('/api/auth/signup', json={'name': 'Ana Dona', 'email': f'a{uuid.uuid4().hex[:8]}@x.com', 'password': 'Senha-forte-123',
                                         'company': 'Studio X', 'segment': 'salao', 'phone': '11987654321'})
    assert r.status_code == 200, r.text
    s = c.get('/api/setup').json()
    assert s['total'] == 15 and s['done'] == 0 and not s['ready']
    assert [x['n'] for x in s['stages']] == [1, 2, 3, 4]
    s = c.post('/api/setup/skip', json={'key': 'instagram'}).json()
    assert s['total'] == 14 and next(x for x in s['steps'] if x['key'] == 'instagram')['skipped']
    s = c.post('/api/setup/skip', json={'key': 'instagram', 'skip': False}).json()
    assert s['total'] == 15
    assert c.post('/api/setup/skip', json={'key': 'xx'}).status_code == 400
    c.put('/api/ai/settings', json={'about': 'Salão no centro', 'default_mode': 'auto'})
    s = c.get('/api/setup').json()
    d = {x['key']: x['done'] for x in s['steps']}
    assert d['sobre'] and d['modo'] and not d['whatsapp']
    assert c.post('/api/setup/hide', json={'hidden': True}).json()['hidden']
