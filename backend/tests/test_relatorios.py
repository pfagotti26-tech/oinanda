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

def test_card_anda_respondidos_e_interno(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    async def no_ai(*a, **k): return None
    monkeypatch.setattr(ai, 'maybe_reply', no_ai, raising=False)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona S', 'email': 'ds@x.com', 'password': 'senha-forte-1', 'company': 'Agência S', 'segment': 'outro'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        for i, n in enumerate(['Cli Um', 'Cli Dois', 'Cli Tres', 'Eu Testando']): inbound(c, f'551190000100{i}', n, 'Oi!')
        cs = {x['name']: x for x in c.get('/api/conversations').json()}
        st = c.get('/api/funnel/stages').json()
        assert c.get('/api/funnel/auto-stage').json()['stage'] == st[1]['id']  # "Em conversa"
        c.post(f'/api/conversations/{cs["Cli Um"]["id"]}/send', json={'text': 'Oi! Como posso ajudar?'})
        f = {x['id']: x for x in c.get('/api/funnel').json()['cards']}
        assert f[cs['Cli Um']['id']]['stage'] == st[1]['id'] and f[cs['Cli Dois']['id']].get('stage') in (None, st[0]['id'])
        msgs = c.get(f'/api/conversations/{cs["Cli Um"]["id"]}').json()['messages']
        assert msgs[-1]['dir'] == 'out'  # mover sozinho não polui a conversa
        c.patch(f'/api/contacts/{cs["Eu Testando"]["id"]}', json={'internal': True})
        r = c.get('/api/reports/sales?days=30').json()
        assert r['totals']['new_contacts'] == 3 and r['totals']['wrote'] == 3 and r['totals']['answered'] == 1
        assert 'Eu Testando' not in {x['name'] for x in c.post('/api/segments/preview', json={'rules': []}).json()['items']}
        # desligar: o card não anda mais
        c.put('/api/funnel/stages', json={'stages': [{'id': s['id'], 'name': s['name'], 'kind': s['kind']} for s in st], 'auto_stage': ''})
        c.post(f'/api/conversations/{cs["Cli Dois"]["id"]}/send', json={'text': 'Oi!'})
        f = {x['id']: x for x in c.get('/api/funnel').json()['cards']}
        assert f[cs['Cli Dois']['id']].get('stage') in (None, st[0]['id'])
        # venda fechada no funil sem responsável conta para quem fechou
        won = next(s for s in st if s['kind'] == 'won')
        c.patch(f'/api/contacts/{cs["Cli Tres"]["id"]}', json={'stage': won['id'], 'value': 200})
        r = c.get('/api/reports/sales?days=30').json()
        assert r['unassigned']['won'] == 0 and next(p for p in r['people'] if p['name'] == 'Dona S')['won'] == 1

def test_mensagem_automatica_e_nao_cliente(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    async def no_ai(*a, **k): return None
    monkeypatch.setattr(ai, 'maybe_reply', no_ai, raising=False)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona T', 'email': 'dt@x.com', 'password': 'senha-forte-1', 'company': 'Agência T', 'segment': 'outro'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511900002001', 'Robo Divulga', 'Aqui está o seu material: https://exemplo.com/mapa')
        inbound(c, '5511900002002', 'Cliente Real', 'Oi, quanto custa?')
        cs = {x['name']: x for x in c.get('/api/conversations').json()}
        assert cs['Robo Divulga']['maybe_auto'] and not cs['Cliente Real']['maybe_auto']
        assert {x['name'] for x in c.get('/api/conversations?filter=waiting').json()} == {'Robo Divulga', 'Cliente Real'}
        assert c.post('/api/contacts/not-client', json={'ids': [cs['Robo Divulga']['id']]}).json()['updated'] == 1
        cs = {x['name']: x for x in c.get('/api/conversations').json()}
        assert cs['Robo Divulga']['internal'] and not cs['Robo Divulga']['maybe_auto']
        assert {x['name'] for x in c.get('/api/conversations?filter=waiting').json()} == {'Cliente Real'}
        assert c.get('/api/reports/sales?days=30').json()['totals']['new_contacts'] == 1
        c.post('/api/contacts/not-client', json={'ids': [cs['Robo Divulga']['id']], 'value': False})
        assert c.get('/api/reports/sales?days=30').json()['totals']['new_contacts'] == 2
