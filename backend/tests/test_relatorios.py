import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from fastapi.testclient import TestClient
import whatsapp, ai
from main import app
from tests.test_origins import inbound

def test_relatorio_honesto(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    async def no_ai(*a, **k): return None
    monkeypatch.setattr(ai, 'maybe_reply', no_ai, raising=False)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona R', 'email': 'dr@x.com', 'password': 'senha-forte-1', 'company': 'Studio R', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511900000001', 'Ana Agora', 'Oi!')
        inbound(c, '5511900000002', 'Beto Ontem', 'Oi!')
        from core import db, now, uid
        cid = c.get('/api/auth/me').json()['company']['id']
        cs = {x['name']: x for x in c.get('/api/conversations').json()}
        lk = c.post('/api/track-links', json={'label': 'Bio do Instagram', 'origin': 'instagram'}).json()
        async def seed():
            await db.contacts.update_one({'id': cs['Ana Agora']['id']}, {'$set': {'last_in_at': now() - timedelta(hours=2), 'origin_link': lk['id']}})
            await db.contacts.update_one({'id': cs['Beto Ontem']['id']}, {'$set': {'last_in_at': now() - timedelta(hours=30)}})
            for i in range(30):
                await db.contacts.insert_one({'id': uid(), 'company_id': cid, 'wa_id': f'551196666{i:04d}', 'name': f'Agenda {i}', 'created_at': now(), 'source': 'whatsapp'})
        c.portal.call(seed)
        r = c.get('/api/reports/sales?days=30').json()
        txt = [a['text'] for a in r['alerts']]
        assert any(t.startswith('1 cliente esperando') for t in txt) and any('janela de 24 h fechou' in t for t in txt)
        assert any(o['name'] == 'Bio do Instagram' and o['kind'] == 'link' for o in r['origin'])
        assert not any('negocia' in t for t in txt)  # todo mundo ainda está na primeira etapa
        j = c.get('/api/reports/journey').json()
        assert j['total'] == 2  # quem só está na agenda do celular não entra na jornada
        # período escolhido e filtros
        from datetime import date
        hoje = date.today().isoformat()
        r = c.get(f'/api/reports/sales?start=2020-01-01&end=2020-01-31').json()
        assert r['days'] == 31 and r['totals']['new_contacts'] == 0
        r = c.get(f'/api/reports/sales?start={hoje}&end={hoje}').json()
        assert r['totals']['new_contacts'] == 2 and 'Bio do Instagram' in r['filters']['origins']
        assert c.get(f'/api/reports/sales?start={hoje}&end={hoje}&origin=Bio do Instagram').json()['totals']['new_contacts'] == 1
        assert c.get(f'/api/reports/sales?start={hoje}&end={hoje}&channel=instagram').json()['totals']['new_contacts'] == 0
        assert c.get(f'/api/reports/sales?start={hoje}&end={hoje}&owner=_none').json()['totals']['new_contacts'] == 2
        assert c.get('/api/reports/sales?start=2020-13-01').status_code in (400, 422)
