import os, json, hmac, hashlib, time
from datetime import timedelta, datetime
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '4440011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def ago(d): return (datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')

def test_carteira_e_segmentos(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona W', 'email': 'dw@x.com', 'password': 'senha-forte-1', 'company': 'Escola W', 'segment': 'escola'})
        c.post('/api/wa/connect', json={'phone_number_id': '4440011', 'waba_id': '4440012', 'token': 'EAA' + 'q' * 30})
        m2 = c.post('/api/products', json={'name': 'Módulo 2', 'category': 'Cursos', 'price': 900, 'duration_days': 365}).json()
        m3 = c.post('/api/products', json={'name': 'Módulo 3', 'category': 'Cursos', 'price': 900, 'duration_days': 365}).json()
        m1 = c.post('/api/products', json={'name': 'Módulo 1', 'category': 'Cursos', 'price': 900, 'duration_days': 365, 'next_ids': [m2['id'], m3['id']]}).json()
        mens = c.post('/api/products', json={'name': 'Mensalidade', 'category': 'Planos', 'price': 200, 'duration_days': 30}).json()
        for wa, nm in [('5511910000001', 'Ana'), ('5511910000002', 'Beto'), ('5511910000003', 'Caio'), ('5511910000004', 'Duda')]: inbound(c, wa, nm, 'oi')
        cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        sale = lambda who, prod, d, qty=1: c.post('/api/sales', json={'contact_id': cv[who], 'date': ago(d), 'items': [{'product_id': prod['id'], 'name': prod['name'], 'price': prod['price'], 'qty': qty}]})
        sale('Ana', m1, 100)                      # tem Módulo 1, não tem 2
        sale('Beto', m1, 300); sale('Beto', m2, 50)  # tem 1 e 2
        sale('Caio', mens, 20)                    # mensalidade vence em ~10 dias
        sale('Duda', mens, 45)                    # venceu há ~15 dias
        w = c.get(f'/api/contacts/{cv["Ana"]}/wallet').json()
        assert w['items'][0]['name'] == 'Módulo 1' and w['items'][0]['state'] == 'ativo'
        assert {o['name'] for o in w['offers']} == {'Módulo 2', 'Módulo 3'}
        wc = c.get(f'/api/contacts/{cv["Caio"]}/wallet').json()
        assert wc['items'][0]['state'] == 'vence_logo' and wc['offers'][0]['why'] == 'renovar'
        # renovou antes de vencer: soma no fim do período
        sale('Caio', mens, 5)
        wc2 = c.get(f'/api/contacts/{cv["Caio"]}/wallet').json()['items'][0]
        exp = datetime.fromisoformat(wc2['expires_at'].replace('Z', '+00:00'))
        assert 38 <= (exp - datetime.now(exp.tzinfo)).days <= 41
        P = lambda rules: c.post('/api/segments/preview', json={'rules': rules}).json()
        r = P([{'field': 'product', 'op': 'active', 'value': m1['id']}, {'field': 'product', 'op': 'never', 'value': m2['id']}])
        assert [x['name'] for x in r['items']] == ['Ana']
        assert [x['name'] for x in P([{'field': 'expires_in', 'value': 15}])['items']] == []  # Caio renovou
        assert [x['name'] for x in P([{'field': 'expired_within', 'value': {'days': 30, 'product': mens['id']}}])['items']] == ['Duda']
        assert {x['name'] for x in P([{'field': 'kind', 'value': 'cliente'}])['items']} == {'Ana', 'Beto', 'Caio', 'Duda'}
        assert P([{'field': 'spent', 'op': 'gte', 'value': 1800}])['count'] == 1
        assert P([{'field': 'last_purchase', 'op': 'more', 'value': 40}])['count'] == 3
        # mudar a validade refaz a carteira
        c.put(f'/api/products/{mens["id"]}', json={'name': 'Mensalidade', 'category': 'Planos', 'price': 200, 'duration_days': 60})
        assert [x['name'] for x in P([{'field': 'expired_within', 'value': 30}])['items']] == []
        s = c.post('/api/segments', json={'name': 'Upsell Módulo 2', 'rules': [{'field': 'product', 'op': 'active', 'value': m1['id']}, {'field': 'product', 'op': 'never', 'value': m2['id']}]}).json()
        assert c.get('/api/segments').json()[0]['count'] == 1
        assert c.post('/api/segments/tag', json={'rules': s['rules'], 'tag': 'oferta-m2'}).json()['count'] == 1
        assert 'oferta-m2' in next(x for x in c.get('/api/conversations').json() if x['name'] == 'Ana')['tags']
        assert 'Ana' in c.post('/api/segments/export', json={'rules': s['rules']}).text
