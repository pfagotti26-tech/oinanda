from zoneinfo import ZoneInfo
import os, json, hmac, hashlib, time
from datetime import timedelta, datetime
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, goals
from core import db, hash_password
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '2220011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_metas(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona G', 'email': 'dg@x.com', 'password': 'senha-forte-1', 'company': 'Loja G', 'segment': 'loja'})
        c.post('/api/wa/connect', json={'phone_number_id': '2220011', 'waba_id': '2220012', 'token': 'EAA' + 'q' * 30})
        ids = {}
        for nm, em in [('Ana', 'ag@x.com'), ('João', 'jg@x.com')]:
            ids[nm] = c.post('/api/team', json={'name': nm, 'email': em, 'role': 'agent'}).json()['member']['id']
            async def setpw(u=ids[nm]): await db.users.update_one({'id': u}, {'$set': {'password_hash': hash_password('senha-forte-1'), 'must_change_password': False}})
            c.portal.call(setpw)
        team = c.post('/api/teams', json={'name': 'Vendas', 'member_ids': [ids['Ana'], ids['João']]}).json()
        assert c.get('/api/goals/board').json()['enabled'] is False
        c.put('/api/goals/settings', json={'goals': True, 'awards': True, 'game': True, 'ranking': 'podium'})
        plano = c.post('/api/products', json={'name': 'Plano', 'price': 1000, 'duration_days': 30}).json()
        for wa, nm in [('5511930000001', 'Cli A'), ('5511930000002', 'Cli B'), ('5511930000003', 'Cli C')]: inbound(c, wa, nm, 'oi')
        cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        today = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d')
        c.post('/api/sales', json={'contact_id': cv['Cli A'], 'date': today, 'items': [{'product_id': plano['id'], 'name': 'Plano', 'price': 1000}], 'sellers': [{'user_id': ids['Ana'], 'pct': 100}]})
        c.post('/api/sales', json={'contact_id': cv['Cli B'], 'date': today, 'items': [{'product_id': plano['id'], 'name': 'Plano', 'price': 1000}], 'sellers': [{'user_id': ids['Ana'], 'pct': 50}, {'user_id': ids['João'], 'pct': 50}]})
        c.post('/api/sales', json={'contact_id': cv['Cli A'], 'date': today, 'items': [{'product_id': plano['id'], 'name': 'Plano', 'price': 1000}], 'sellers': [{'user_id': ids['João'], 'pct': 100}]})  # renovação
        g = c.post('/api/goals', json={'name': 'Faturamento da equipe', 'metric': 'revenue', 'scope': 'team', 'target_id': team['id'], 'target': 2500, 'award_meta': 'Pizza para o time', 'award_super': 'Folga na sexta'}).json()
        assert c.post('/api/goals', json={'name': 'xx', 'metric': 'revenue', 'scope': 'team', 'target': 10}).status_code == 400
        gu = c.post('/api/goals', json={'name': 'Renovações João', 'metric': 'renewals', 'scope': 'user', 'target_id': ids['João'], 'target': 2}).json()
        lst = {x['name']: x for x in c.get('/api/goals').json()}
        assert lst['Faturamento da equipe']['value'] == 3000 and lst['Faturamento da equipe']['pct'] == 120.0 and lst['Faturamento da equipe']['level'] == 'super'
        assert lst['Renovações João']['value'] == 1 and lst['Renovações João']['level'] == 'abaixo'
        assert 'per_day' in lst['Renovações João']
        # dividir a meta da equipe por pesos
        sp = c.post(f'/api/goals/{g["id"]}/split', json={'mode': 'weights', 'weights': {ids['Ana']: 60, ids['João']: 40}}).json()
        assert sorted(x['target'] for x in sp) == [1000, 1500]
        # painel: prêmios, ranking, pontos e conquistas
        b = c.get('/api/goals/board').json()
        assert {a['level'] for a in b['awards'] if a['goal'] == 'Faturamento da equipe'} == {'meta', 'super'}
        r = {x['name']: x for x in b['ranking']}
        assert r['Ana']['points'] == 15 + 20 and r['João']['points'] == 20 + 20 + 40  # vendas + bateu a meta dividida (João também a supermeta)
        assert b['team_ranking'][0]['name'] == 'Vendas'
        cel = c.get('/api/inbox/counts').json()['celebrations']
        assert any('Vendas bateu a supermeta Faturamento da equipe' in x['text'] for x in cel)
        aw = next(a for a in b['awards'] if a['level'] == 'super')
        assert c.post(f'/api/awards/{aw["id"]}/deliver').json()['ok']
        # atendente: vê a própria meta, ranking só pódio, conquistas
        c.post('/api/auth/logout'); c.post('/api/auth/login', json={'email': 'jg@x.com', 'password': 'senha-forte-1'})
        bj = c.get('/api/goals/board').json()
        assert any(x['name'] == 'Renovações João' for x in bj['my_goals']) and bj['my_rank']['pos'] == 1
        assert next(a for a in bj['achievements'] if a['key'] == 'first_sale')['done'] is True
        assert c.post('/api/goals', json={'name': 'xx', 'metric': 'sales', 'target': 1}).status_code == 403
        # desafio encerrado premia o primeiro
        c.post('/api/auth/logout'); c.post('/api/auth/login', json={'email': 'dg@x.com', 'password': 'senha-forte-1'})
        y = (datetime.now(ZoneInfo('America/Sao_Paulo')) - timedelta(days=1)).strftime('%Y-%m-%d'); w = (datetime.now(ZoneInfo('America/Sao_Paulo')) - timedelta(days=7)).strftime('%Y-%m-%d')
        c.post('/api/challenges', json={'name': 'Semana do plano', 'metric': 'sales', 'among': 'users', 'start': w, 'end': y, 'award': 'Vale-jantar'})
        co = c.portal.call(lambda: db.companies.find_one({}, {'_id': 0}))
        c.portal.call(lambda: goals.close_challenges(co))
        assert c.get('/api/challenges').json()[0]['state'] == 'encerrado'
