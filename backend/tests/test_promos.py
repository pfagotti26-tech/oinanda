import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai
from core import db, hash_password
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '5550011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

S = {'name': 'Nanda', 'tone': 'proximo', 'goal': '', 'qualify': '', 'handoff': '', 'rules': '', 'about': '', 'offer': '', 'faq': '', 'knowledge': ''}

def test_promocoes_e_cupons(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona P', 'email': 'dp@x.com', 'password': 'senha-forte-1', 'company': 'Studio P', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '5550011', 'waba_id': '5550012', 'token': 'EAA' + 'q' * 30})
        ana = c.post('/api/team', json={'name': 'Ana', 'email': 'ap@x.com', 'role': 'agent'}).json()['member']['id']
        async def setpw(): await db.users.update_one({'id': ana}, {'$set': {'password_hash': hash_password('senha-forte-1'), 'must_change_password': False}})
        c.portal.call(setpw)
        tri = c.post('/api/products', json={'name': 'Plano trimestral', 'category': 'Planos', 'price': 780}).json()
        gar = c.post('/api/products', json={'name': 'Garrafinha', 'category': 'Loja', 'price': 30, 'kind': 'produto'}).json()
        inbound(c, '5511900003333', 'Cli P', 'oi'); inbound(c, '5511900004444', 'Cli Q', 'oi')
        cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        # validações
        assert c.post('/api/promos', json={'name': 'Errada', 'kind': 'bxgy', 'buy': 2, 'pay': 2}).status_code == 400
        assert c.post('/api/promos', json={'name': 'Errada', 'kind': 'percent', 'value': 150}).status_code == 400
        # automática: 10% em Planos na primeira compra
        p1 = c.post('/api/promos', json={'name': 'Boas-vindas', 'kind': 'percent', 'value': 10, 'scope': 'categories', 'categories': ['Planos'], 'first_purchase': True}).json()
        # com cupom: leve 3 pague 2 em garrafinhas
        p2 = c.post('/api/promos', json={'name': 'Garrafa 3x2', 'kind': 'bxgy', 'buy': 3, 'pay': 2, 'scope': 'products', 'product_ids': [gar['id']], 'requires_coupon': True, 'stackable': True}).json()
        cp = c.post(f'/api/promos/{p2["id"]}/coupons', json={'code': 'agua3x2', 'public': True}).json()[0]
        assert cp['code'] == 'AGUA3X2'
        assert c.post(f'/api/promos/{p2["id"]}/coupons', json={'code': 'AGUA3X2'}).status_code == 409
        p3 = c.post('/api/promos', json={'name': 'VIP', 'kind': 'percent', 'value': 15}).json()
        lote = c.post(f'/api/promos/{p3["id"]}/coupons', json={'count': 5, 'prefix': 'VIP'}).json()
        assert len(lote) == 5 and all(x['code'].startswith('VIP') and x['max_uses'] == 1 for x in lote)
        items = [{'product_id': tri['id'], 'name': 'Plano trimestral', 'qty': 1, 'price': 780}, {'product_id': gar['id'], 'name': 'Garrafinha', 'qty': 3, 'price': 30}]
        ev = c.post('/api/promos/evaluate', json={'contact_id': cv['Cli P'], 'items': items, 'coupon': 'AGUA3X2'}).json()
        assert ev['coupon']['ok'] and ev['discount'] == 78 + 30
        assert {a['name'] for a in ev['applied']} == {'Boas-vindas', 'Garrafa 3x2'}
        bad = c.post('/api/promos/evaluate', json={'contact_id': cv['Cli P'], 'items': items, 'coupon': 'NAOEXISTE'}).json()
        assert bad['coupon']['reason'] == 'cupom não existe'
        # registrar a venda com cupom
        s1 = c.post('/api/sales', json={'contact_id': cv['Cli P'], 'items': items, 'coupon': 'agua3x2'}).json()
        assert s1['subtotal'] == 870 and s1['total'] == 762 and s1['discount'] == 108 and s1['status'] == 'ok'
        assert {a['code'] for a in s1['promos']} == {None, 'AGUA3X2'}
        assert c.post('/api/sales', json={'contact_id': cv['Cli P'], 'items': items, 'coupon': 'NAOEXISTE'}).status_code == 400
        # segunda compra do mesmo cliente: boas-vindas não vale mais
        ev2 = c.post('/api/promos/evaluate', json={'contact_id': cv['Cli P'], 'items': items[:1]}).json()
        assert ev2['discount'] == 0 and next(a for a in ev2['available'] if a['name'] == 'Boas-vindas')['reason'] == 'só vale na primeira compra'
        # cupom individual só uma vez
        code = lote[0]['code']
        c.post('/api/sales', json={'contact_id': cv['Cli Q'], 'items': items[:1], 'coupon': code, 'promo_ids': []})
        ev3 = c.post('/api/promos/evaluate', json={'contact_id': cv['Cli P'], 'items': items[:1], 'coupon': code}).json()
        assert ev3['coupon']['reason'] == 'cupom já foi usado o máximo de vezes'
        # relatório por promoção e desconto total
        r = c.get('/api/sales/report', params={'group': 'promo'}).json()
        assert r['totals']['discount'] == 108 + 117
        assert {x['label'] for x in r['rows']} == {'Boas-vindas + Garrafa 3x2', 'VIP'}
        lp = {p['name']: p for p in c.get('/api/promos').json()['items']}
        assert lp['Boas-vindas']['uses'] == 1 and lp['Boas-vindas']['discount_given'] == 78 and lp['VIP']['coupons'] == 5 and lp['Garrafa 3x2']['state'] == 'ativa'
        # a Nanda sabe das promoções e confere cupom citado
        ct = next(x for x in c.get('/api/conversations').json() if x['name'] == 'Cli Q')
        inbound(c, '5511900004444', 'Cli Q', 'tenho o cupom AGUA3X2, vale?')
        prompt = c.portal.call(lambda: ai.system_for(S, {'id': ct['company_id'], 'name': 'Studio P'}, ct))
        assert 'PROMOÇÕES VALENDO AGORA' in prompt and 'Cupom que pode passar: AGUA3X2' in prompt and 'O cliente citou o cupom AGUA3X2: VÁLIDO' in prompt
        assert code not in prompt  # código individual não vaza
        # pausada some da Nanda
        c.put(f'/api/promos/{p2["id"]}', json={**{k: v for k, v in p2.items() if k in ('name', 'kind', 'buy', 'pay', 'scope', 'product_ids', 'requires_coupon', 'stackable')}, 'active': False})
        assert 'Garrafa 3x2:' not in c.portal.call(lambda: ai.system_for(S, {'id': ct['company_id'], 'name': 'Studio P'}, ct))
        # limite de desconto do atendente: acima vai para aprovação
        c.put('/api/promos-settings', json={'max_discount_pct': 10})
        c.post('/api/auth/logout'); c.post('/api/auth/login', json={'email': 'ap@x.com', 'password': 'senha-forte-1'})
        hi = c.post('/api/sales', json={'contact_id': cv['Cli Q'], 'items': [{'name': 'Plano', 'price': 1000, 'discount': 200}], 'promo_ids': []}).json()
        assert hi['status'] == 'pending'
        lo = c.post('/api/sales', json={'contact_id': cv['Cli Q'], 'items': [{'name': 'Plano', 'price': 1000, 'discount': 50}], 'promo_ids': []}).json()
        assert lo['status'] == 'ok'
        assert c.post(f'/api/sales/{hi["id"]}/approve').status_code == 403
        c.post('/api/auth/logout'); c.post('/api/auth/login', json={'email': 'dp@x.com', 'password': 'senha-forte-1'})
        before = c.get('/api/sales/report').json()
        assert before['pending'] == 1
        assert c.post(f'/api/sales/{hi["id"]}/approve').json()['ok']
        assert c.get('/api/sales/report').json()['totals']['value'] == before['totals']['value'] + 800
