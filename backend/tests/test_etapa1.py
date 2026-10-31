import os
os.environ['MONGO_URL'] = 'mock'; os.environ['ADMIN_EMAIL'] = 'admin@oinanda.com.br'
from fastapi.testclient import TestClient
from main import app

def mk():
    return TestClient(app, base_url='https://testserver')

def test_fluxo_completo():
    with mk() as c:
        assert c.get('/api/health').json()['ok']
        assert c.get('/api/auth/me').status_code == 401
        r = c.post('/api/auth/signup', json={'name': 'Ana', 'email': 'ana@studio.com', 'password': 'senha-forte-1', 'company': 'Studio Forma', 'segment': 'studio'})
        assert r.status_code == 200, r.text
        me = c.get('/api/auth/me').json()
        assert me['user']['role'] == 'owner' and me['company']['name'] == 'Studio Forma' and me['trial_days_left'] == 7
        assert 'password_hash' not in me['user']
        assert c.post('/api/auth/signup', json={'name': 'Xavier', 'email': 'ana@studio.com', 'password': 'senha-forte-1', 'company': 'Outra'}).status_code == 409
        inv = c.post('/api/team', json={'name': 'Camila', 'email': 'camila@studio.com', 'role': 'agent'}).json()
        temp = inv['temp_password']
        assert len(c.get('/api/team').json()) == 2
        assert any('Convidou' in a['action'] for a in c.get('/api/audit').json())
        assert c.patch('/api/company', json={'phone': '11999990000'}).json()['phone'] == '11999990000'
        assert c.get('/api/admin/stats').status_code == 403
    with mk() as c2:  # atendente convidada
        assert c2.post('/api/auth/login', json={'email': 'camila@studio.com', 'password': 'errada'}).status_code == 401
        assert c2.post('/api/auth/login', json={'email': 'camila@studio.com', 'password': temp}).status_code == 200
        assert c2.get('/api/team').status_code == 403  # precisa trocar a senha
        assert c2.post('/api/auth/password', json={'current_password': temp, 'new_password': 'camila-nova-123'}).status_code == 200
        assert c2.get('/api/team').status_code == 200
        assert c2.post('/api/team', json={'name': 'Yara', 'email': 'y@y.com'}).status_code == 403  # atendente não convida
        assert c2.get('/api/audit').status_code == 403
    with mk() as a:  # admin
        a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1', 'company': 'Oi Nanda'})
        assert a.get('/api/auth/me').json()['user']['is_admin']
        st = a.get('/api/admin/stats').json(); assert st['companies'] == 2
        comps = a.get('/api/admin/companies').json()
        studio = next(x for x in comps if x['name'] == 'Studio Forma'); assert studio['users'] == 2
        assert a.post(f'/api/admin/companies/{studio["id"]}/enter').status_code == 200
        me = a.get('/api/auth/me').json(); assert me['company']['name'] == 'Studio Forma' and me['user']['impersonated_by'] == 'Paulo'
        assert a.post('/api/admin/exit').status_code == 200
        assert a.get('/api/auth/me').json()['company']['name'] == 'Oi Nanda'
        assert a.patch(f'/api/admin/companies/{studio["id"]}', json={'status': 'suspended'}).json()['status'] == 'suspended'
        assert any('suporte' in r['action'] for r in a.get('/api/admin/audit').json())
    with mk() as c3:
        assert c3.post('/api/auth/login', json={'email': 'ana@studio.com', 'password': 'senha-forte-1'}).status_code == 200
        assert c3.get('/api/auth/me').status_code == 403  # suspensa

def test_origem_estranha_bloqueada():
    with mk() as c:
        assert c.post('/api/auth/login', json={'email': 'a@a.com', 'password': 'x'}, headers={'origin': 'https://malicioso.com'}).status_code == 403
