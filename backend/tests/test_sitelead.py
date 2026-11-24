import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def test_lead_do_site():
    with TestClient(app, base_url='https://testserver') as c:
        H = {'x-forwarded-for': '9.9.9.9'}
        assert c.post('/api/site/lead', json={'name': 'Renata', 'phone': '1234-5678'}, headers=H).status_code == 400
        r = c.post('/api/site/lead', json={'name': 'Renata', 'phone': '(11) 91234-5678', 'segment': 'clinica', 'source': 'utm_source=ig'}, headers=H)
        assert r.json() == {'ok': True, 'guide': '/guia/7-erros-whatsapp/'}
        c.post('/api/site/lead', json={'name': 'Renata S', 'phone': '11 91234-5678'}, headers=H)  # mesmo número: atualiza, não duplica
        c.post('/api/site/lead', json={'name': 'Robô', 'phone': '11 90000-0000', 'website': 'http://spam'}, headers=H)  # campo escondido
        for _ in range(8): c.post('/api/site/lead', json={'name': 'Xu', 'phone': '11 98888-7777'}, headers={'x-forwarded-for': '8.8.8.8'})
        assert c.post('/api/site/lead', json={'name': 'Xu', 'phone': '11 98888-7777'}, headers={'x-forwarded-for': '8.8.8.8'}).status_code == 429
        # vem do site (outro endereço que o do sistema): aceito só nesta rota
        assert c.post('/api/site/lead', json={'name': 'Site', 'phone': '11 97777-6666'}, headers={'origin': 'https://oinanda.com.br', 'x-forwarded-for': '7.7.7.7'}).status_code == 200
        assert c.post('/api/site/lead', json={'name': 'Mal', 'phone': '11 97777-6666'}, headers={'origin': 'https://malicioso.com', 'x-forwarded-for': '7.7.7.7'}).status_code == 403
        assert c.post('/api/auth/logout', json={}, headers={'origin': 'https://oinanda.com.br'}).status_code == 403
        assert c.get('/api/admin/site-leads').status_code == 401
        c.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1', 'company': 'Oi Nanda', 'phone': '(11) 91234-5678'})
        rows = c.get('/api/admin/site-leads').json()
        r1 = next(x for x in rows if x['phone'] == '5511912345678')
        assert r1['name'] == 'Renata S' and r1['times'] == 2 and r1['signed_up'] is True
        assert not any(x['name'] == 'Robô' for x in rows)
        assert 'Renata S' in c.get('/api/admin/site-leads.csv').text
