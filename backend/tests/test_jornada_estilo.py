"""Jornada pelo jeito de vender: o ramo sugere, o cliente escolhe, os nomes vêm dos serviços que ele cadastrou."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def test_jeito_de_vender():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona L', 'email': 'l@x.com', 'password': 'senha-forte-1', 'company': 'Loja L', 'segment': 'loja'})
        d = c.get('/api/journey/settings').json()
        assert d['style'] is None and d['suggested'] == 'direto' and len(d['styles']) == 4
        assert 'Comprou de novo' in next(x for x in d['styles'] if x['key'] == 'direto')['preview']
        r = c.post('/api/journey/settings/style', json={'style': 'direto'}).json()
        names = [m['name'] for m in r['milestones']]
        assert names == ['Entrou', 'Perguntou sobre produto', 'Comprou', 'Comprou de novo'] and not any('avalia' in n.lower() for n in names)
        d = c.get('/api/journey/settings').json()
        assert d['style'] == 'direto' and d['custom']
        # agenda: o nome vem do serviço cadastrado
        r = c.post('/api/journey/settings/style', json={'style': 'agenda'}).json()
        assert r['milestones'][1]['name'].startswith('Agendou ') and r['milestones'][1]['rule']['type'] == 'appt_booked'
        assert c.post('/api/journey/settings/style', json={'style': 'xx'}).status_code == 422
