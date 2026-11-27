"""Devolver vendas para a Meta: venda de quem veio de anúncio vira Purchase, uma vez só."""
import os, asyncio
os.environ.update(MONGO_URL='mock', CAPI_OPEN='1', IG_APP_ID='111', IG_APP_SECRET='igsecreto', IG_CONFIG_ID='777')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import instagram, whatsapp, capi
from core import db, now, uid
from main import app

def test_devolve_vendas(monkeypatch):
    calls = []
    async def fake_graph(method, path, token, payload=None, params=None):
        calls.append(('wa', path, payload)); return {'id': 'DSWA'} if path.endswith('/dataset') else {'events_received': 1}
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        calls.append(('ig', path, payload)); return {'id': 'DSPG'} if path.endswith('/dataset') else {'events_received': 1}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(instagram, 'ig', fake_ig)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona C', 'email': 'dc@x.com', 'password': 'senha-forte-1', 'company': 'Clínica C', 'segment': 'clinica'})
        cid = c.get('/api/auth/me').json()['company']['id']
        assert c.put('/api/capi', json={'on': True}).status_code == 400  # nada conectado
        async def prep():
            from whatsapp import encrypt
            await db.wa_numbers.insert_one({'company_id': cid, 'waba_id': 'W1', 'phone_number_id': 'P1', 'token_enc': encrypt('t'), 'status': 'connected'})
            await db.ig_accounts.insert_one({'company_id': cid, 'ig_id': 'IG1', 'page_id': 'PG1', 'mode': 'page', 'token_enc': encrypt('t'), 'status': 'connected'})
            ids = {}
            for k, extra in (('ad', {'wa_id': '5511999990001', 'ad': {'ctwa_clid': 'CLID1', 'source_id': 'AD9'}}), ('plain', {'wa_id': '5511999990002'}),
                             ('mg', {'fb_id': 'PSID1', 'channel': 'messenger'})):
                ids[k] = uid(); await db.contacts.insert_one({'id': ids[k], 'company_id': cid, 'name': k, 'created_at': now(), **extra})
                await db.sales.insert_one({'id': 's' + k, 'company_id': cid, 'contact_id': ids[k], 'status': 'ok', 'total': 250.0, 'created_at': now()})
            return ids
        ids = asyncio.run(prep())
        r = c.put('/api/capi', json={'on': True}); assert r.status_code == 200 and r.json()['on']
        asyncio.run(capi.tick())
        evs = [x for x in calls if x[1].endswith('/events')]
        assert len(evs) == 2
        wa = next(x for x in evs if x[0] == 'wa')[2]['data'][0]
        assert wa['event_name'] == 'Purchase' and wa['user_data']['ctwa_clid'] == 'CLID1' and wa['custom_data'] == {'currency': 'BRL', 'value': 250.0}
        assert wa['action_source'] == 'business_messaging' and wa['messaging_channel'] == 'whatsapp'
        mg = next(x for x in evs if x[0] == 'ig')[2]['data'][0]
        assert mg['user_data'] == {'page_id': 'PG1', 'page_scoped_user_id': 'PSID1'}
        assert asyncio.run(db.sales.find_one({'id': 'splain'}))['capi']['status'] == 'skip'  # não veio de anúncio
        asyncio.run(capi.tick())  # não manda de novo
        assert len([x for x in calls if x[1].endswith('/events')]) == 2
        d = c.get('/api/capi').json()
        assert d['stats']['sent'] == 2 and d['stats']['value'] == 500.0 and d['channels']['whatsapp']['ready']
        # lead qualificado: cliente de anúncio chega na etapa escolhida
        st = d['stages'][1]['id']
        assert c.put('/api/capi', json={'on': True, 'ql_stage': 'nada'}).status_code == 400
        c.put('/api/capi', json={'on': True, 'ql_stage': st})
        asyncio.run(db.contacts.update_one({'id': ids['ad']}, {'$set': {'stage': st, 'stage_at': now()}}))
        asyncio.run(capi.tick())
        ql = [x for x in calls if x[1].endswith('/events') and x[2]['data'][0]['event_name'] == 'QualifiedLead']
        assert len(ql) == 1 and 'custom_data' not in ql[0][2]['data'][0]
        # falha da Meta: tenta de novo e mostra o motivo
        async def boom(method, path, token, payload=None, params=None):
            from fastapi import HTTPException
            raise HTTPException(400, 'A Meta recusou: sem permissão')
        monkeypatch.setattr(whatsapp, 'graph', boom)
        asyncio.run(db.sales.insert_one({'id': 'snew', 'company_id': cid, 'contact_id': ids['ad'], 'status': 'ok', 'total': 90.0, 'created_at': now()}))
        asyncio.run(capi.tick())
        s = asyncio.run(db.sales.find_one({'id': 'snew'}))['capi']
        assert s['status'] == 'failed' and s['tries'] == 1
        assert 'sem permissão' in c.get('/api/capi').json()['last_error']

def test_so_admin_ate_liberar(monkeypatch):
    monkeypatch.setenv('CAPI_OPEN', '0')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona X', 'email': 'dx@x.com', 'password': 'senha-forte-1', 'company': 'Loja X', 'segment': 'loja'})
        d = c.get('/api/capi').json()
        assert d['available'] is False
        r = c.put('/api/capi', json={'on': True}); assert r.status_code == 400 and 'liberando' in r.json()['detail']
