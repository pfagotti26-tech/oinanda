import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai
from main import app
from tests.test_origins import inbound

def test_ficha_do_cliente(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    async def no_ai(*a, **k): return None
    monkeypatch.setattr(ai, 'maybe_reply', no_ai, raising=False)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona F', 'email': 'df@x.com', 'password': 'senha-forte-1', 'company': 'Studio F', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511944440000', 'Lia Ficha', 'Oi, quero saber dos planos')
        ct = next(x for x in c.get('/api/conversations').json() if x['name'] == 'Lia Ficha')
        c.post(f'/api/conversations/{ct["id"]}/note', json={'text': 'Prefere à tarde'})
        c.post('/api/sales', json={'contact_id': ct['id'], 'items': [{'name': 'Plano mensal', 'price': 300}]})
        c.post('/api/sales', json={'contact_id': ct['id'], 'items': [{'name': 'Plano mensal', 'price': 200}]})
        p = c.get(f'/api/contacts/{ct["id"]}/profile').json()
        assert p['kind'] == 'cliente' and p['stats']['spent'] == 500 and p['stats']['purchases'] == 2 and p['stats']['ticket'] == 250
        assert p['window_open'] is True and p['channel'] == 'whatsapp'
        t = c.get(f'/api/contacts/{ct["id"]}/timeline').json()
        kinds = [x['kind'] for x in t['items']]
        assert kinds.count('sale') == 2 and 'chat' in kinds and 'note' in kinds and kinds[-1] == 'start'
        chat = next(x for x in t['items'] if x['kind'] == 'chat')
        assert 'quero saber dos planos' in chat['sub'] and '1 do cliente' in chat['by']
        # abrir a ficha não marca a conversa como lida
        assert next(x for x in c.get('/api/conversations').json() if x['name'] == 'Lia Ficha').get('unread')
