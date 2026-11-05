import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from core import db, hash_password
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '9990011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_vendas(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona V', 'email': 'dv@x.com', 'password': 'senha-forte-1', 'company': 'Studio V', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '9990011', 'waba_id': '9990012', 'token': 'EAA' + 'q' * 30})
        ids = {}
        for nm, em in [('Ana', 'av@x.com'), ('João', 'jv@x.com')]:
            ids[nm] = c.post('/api/team', json={'name': nm, 'email': em, 'role': 'agent'}).json()['member']['id']
            async def setpw(u=ids[nm]): await db.users.update_one({'id': u}, {'$set': {'password_hash': hash_password('senha-forte-1'), 'must_change_password': False}})
            c.portal.call(setpw)
        prods = c.get('/api/products').json()
        assert any(p['category'] == 'Agenda' for p in prods)  # serviço da agenda entra no catálogo
        plano = c.post('/api/products', json={'name': 'Plano trimestral', 'category': 'Planos', 'price': 750}).json()
        assert c.post('/api/products', json={'name': 'Plano trimestral'}).status_code == 409
        inbound(c, '5511900001111', 'Cli Venda', 'quero o plano'); inbound(c, '5511900002222', 'Cli Dois', 'oi')
        conv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        # venda compartilhada: plano + item avulso, Ana 30% / João 70%
        bad = c.post('/api/sales', json={'contact_id': conv['Cli Venda'], 'items': [{'name': 'x', 'price': 10}], 'sellers': [{'user_id': ids['Ana'], 'pct': 50}]})
        assert bad.status_code == 400
        s1 = c.post('/api/sales', json={'contact_id': conv['Cli Venda'], 'payment': 'pix',
                                        'items': [{'product_id': plano['id'], 'name': 'Plano trimestral', 'qty': 1, 'price': 750, 'discount': 50},
                                                  {'name': 'Garrafinha', 'qty': 2, 'price': 25}],
                                        'sellers': [{'user_id': ids['Ana'], 'pct': 30}, {'user_id': ids['João'], 'pct': 70}]}).json()
        assert s1['total'] == 750 and s1['items'][0]['category'] == 'Planos'
        ct = c.get(f'/api/conversations/{conv["Cli Venda"]}').json()['contact']
        assert ct['value'] == 750
        st = {s['id']: s for s in c.get('/api/funnel/stages').json()}
        assert st[ct['stage']]['kind'] == 'won'  # foi para a etapa de venda
        # recompra do mesmo cliente
        c.post('/api/sales', json={'contact_id': conv['Cli Venda'], 'items': [{'name': 'Garrafinha', 'qty': 1, 'price': 25}], 'sellers': [{'user_id': ids['João'], 'pct': 100}], 'payment': 'dinheiro'})
        s3 = c.post('/api/sales', json={'contact_id': conv['Cli Dois'], 'items': [{'product_id': plano['id'], 'name': 'Plano trimestral', 'price': 750}], 'sellers': [{'user_id': ids['Ana'], 'pct': 100}]}).json()
        assert c.get(f'/api/conversations/{conv["Cli Venda"]}').json()['contact']['value'] == 775
        r = c.get('/api/sales/report', params={'group': 'seller'}).json()
        assert r['totals']['value'] == 1525 and r['totals']['count'] == 3
        sel = {x['label']: x for x in r['rows']}
        assert sel['Ana']['value'] == 225 + 750 and sel['João']['value'] == 525 + 25
        assert round(sum(x['value'] for x in r['rows']), 2) == 1525  # dividir não duplica
        prod = {x['label']: x for x in c.get('/api/sales/report', params={'group': 'product'}).json()['rows']}
        assert prod['Plano trimestral']['value'] == 1450 and prod['Garrafinha']['qty'] == 3
        cat = {x['label']: x['value'] for x in c.get('/api/sales/report', params={'group': 'category'}).json()['rows']}
        assert cat == {'Planos': 1450, 'Sem categoria': 75}
        # filtro por vendedor: só a parte dele
        rj = c.get('/api/sales/report', params={'group': 'product', 'seller': ids['João']}).json()
        assert rj['totals']['value'] == 550
        # filtro por produto
        assert c.get('/api/sales/report', params={'product': plano['id']}).json()['totals']['value'] == 1450
        assert c.get('/api/sales/report', params={'group': 'payment'}).json()['rows'][0]['label'] in ('Pix', 'Não informado')
        assert len(c.get('/api/sales/report', params={'group': 'day'}).json()['series']) == 30
        # editar e cancelar
        e = c.put(f'/api/sales/{s3["id"]}', json={'contact_id': conv['Cli Dois'], 'items': [{'product_id': plano['id'], 'name': 'Plano trimestral', 'price': 700}], 'sellers': [{'user_id': ids['Ana'], 'pct': 100}]}).json()
        assert e['total'] == 700 and e['history'][-1]['what'] == 'itens, valor'
        assert c.post(f'/api/sales/{s3["id"]}/cancel', json={'reason': 'desistiu'}).json()['ok']
        assert c.get(f'/api/conversations/{conv["Cli Dois"]}').json()['contact']['value'] is None
        assert c.get('/api/sales/report').json()['totals']['value'] == 775
        csv = c.get('/api/sales/export').text
        assert 'Plano trimestral' in csv and 'Ana 30%' in csv
        # atendente vê só as próprias
        c.post('/api/auth/logout'); c.post('/api/auth/login', json={'email': 'jv@x.com', 'password': 'senha-forte-1'})
        rj = c.get('/api/sales/report').json()
        assert rj['mine_only'] and rj['totals']['value'] == 550
        v = c.post('/api/sales/views', json={'name': 'Minhas do mês', 'params': {'group': 'product', 'hack': 'x'}}).json()
        assert v['params'] == {'group': 'product'} and v['shared'] is False

def test_venda_antiga_do_funil(monkeypatch):
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona L', 'email': 'dl@x.com', 'password': 'senha-forte-1', 'company': 'Loja L', 'segment': 'loja'})
        st = c.get('/api/funnel/stages').json(); won = next(s for s in st if s['kind'] == 'won')
        ct = c.post('/api/contacts', json={'name': 'Antigo', 'phone': '11977776666'}).json()
        c.patch(f'/api/contacts/{ct["id"]}', json={'stage': won['id'], 'value': 300})
        r = c.get('/api/sales/report').json()
        assert r['totals']['value'] == 300 and r['rows'][0]['label'] == 'Sem vendedor'
        assert c.get('/api/sales/report').json()['totals']['count'] == 1  # não duplica
