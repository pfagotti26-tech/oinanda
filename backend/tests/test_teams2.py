import os, json, hmac, hashlib, time
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from core import db, now, hash_password
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '8880011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def login(c, email): return c.post('/api/auth/login', json={'email': email, 'password': 'senha-forte-1'})

def test_disponibilidade_credito_e_aviso(monkeypatch):
    sent = []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'o1'}]}
        if path.endswith('/message_templates') and method == 'POST': return {'id': 't1', 'status': 'PENDING'}
        if path.endswith('/message_templates'): return {'data': [{'name': params['name'], 'status': 'APPROVED'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona Dois', 'email': 'd2@x.com', 'password': 'senha-forte-1', 'company': 'Loja Dois', 'segment': 'loja'})
        c.post('/api/wa/connect', json={'phone_number_id': '8880011', 'waba_id': '8880012', 'token': 'EAA' + 'q' * 30})
        ids = {}
        for nm, em in [('Ana', 'a2@x.com'), ('João', 'j2@x.com'), ('Bia', 'b2@x.com')]:
            r = c.post('/api/team', json={'name': nm, 'email': em, 'role': 'agent'}).json(); ids[nm] = r['member']['id']
            async def setpw(u=ids[nm]): await db.users.update_one({'id': u}, {'$set': {'password_hash': hash_password('senha-forte-1'), 'must_change_password': False}})
            c.portal.call(setpw)
        ven = c.post('/api/teams', json={'name': 'Vendas', 'member_ids': [ids['João'], ids['Bia']], 'distribution': 'roundrobin', 'max_open': 1}).json()
        assert ven['max_open'] == 1
        # aviso: o gestor cria o modelo; o João cadastra o celular
        assert c.post('/api/notify/template').json()['status'] == 'PENDING'
        assert c.get('/api/notify').json()['template']['status'] == 'APPROVED'
        c.post('/api/auth/logout'); login(c, 'j2@x.com')
        assert c.put('/api/me/notify', json={'wa_phone': '123', 'wa_notify': True}).status_code == 400
        assert c.put('/api/me/notify', json={'wa_phone': '(11) 98888-7777', 'wa_notify': True}).json()['wa_phone'] == '5511988887777'
        async def offline(): await db.users.update_one({'id': ids['João']}, {'$set': {'last_seen': now() - timedelta(minutes=30)}})
        c.portal.call(offline)
        # a Bia está ausente: o rodízio pula ela
        c.post('/api/auth/logout'); login(c, 'b2@x.com')
        assert c.put('/api/me/status', json={'away': True}).json()['away'] is True
        c.post('/api/auth/logout'); login(c, 'd2@x.com')
        for wa, nm in [('5511900000001', 'Cli Um'), ('5511900000002', 'Cli Dois')]: inbound(c, wa, nm, 'oi')
        convs = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        c.post('/api/auth/logout'); login(c, 'a2@x.com')
        r1 = c.post(f'/api/conversations/{convs["Cli Um"]}/transfer', json={'team_id': ven['id'], 'reason': 'Quer comprar hoje'}).json()
        assert r1['assigned_to'] == ids['João']
        tpl = [p for p in sent if p.get('type') == 'template']
        assert len(tpl) == 1 and tpl[0]['to'] == '5511988887777' and tpl[0]['template']['components'][0]['parameters'][3]['text'] == 'Quer comprar hoje'
        # João já tem 1 aberta (limite 1) e Bia ausente: a próxima fica na fila
        r2 = c.post(f'/api/conversations/{convs["Cli Dois"]}/transfer', json={'team_id': ven['id'], 'reason': 'Também quer comprar'}).json()
        assert r2['assigned_to'] is None
        # João com o painel aberto: aviso só na tela
        c.post('/api/auth/logout'); login(c, 'j2@x.com'); c.get('/api/inbox/counts')
        n_before = len(sent)
        c.post('/api/auth/logout'); login(c, 'd2@x.com')
        c.post(f'/api/conversations/{convs["Cli Dois"]}/transfer', json={'user_id': ids['João'], 'reason': 'Passo pra você'})
        assert len(sent) == n_before
        # João fecha a venda da Cli Um: crédito para a Ana no relatório
        stages = c.get('/api/funnel/stages').json(); won = next(s for s in stages if s['kind'] == 'won')
        c.patch(f'/api/contacts/{convs["Cli Um"]}', json={'stage': won['id'], 'value': 900})
        p = {x['name']: x for x in c.get('/api/reports/sales').json()['people']}
        assert p['Ana']['passed'] == 2 and p['Ana']['assisted'] == 1 and p['Ana']['assisted_value'] == 900
        assert p['João']['won'] == 1 and p['João']['assisted'] == 0
