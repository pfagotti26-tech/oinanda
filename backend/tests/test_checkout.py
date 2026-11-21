import os, json, hmac, hashlib
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from core import db, now
from main import app
import automations

def mk(): return TestClient(app, base_url='https://testserver')

def kiwify_body(kind, order='ord-1', status='waiting_payment', method='boleto'):
    return {'order_id': order, 'order_status': status, 'webhook_event_type': kind, 'payment_method': method,
            'boleto_URL': 'https://kiwify.com.br/boleto/abc', 'Product': {'product_id': 'p1', 'product_name': 'Curso Vendas 10x'},
            'Customer': {'full_name': 'Bruno Lima', 'email': 'Bruno@x.com', 'mobile': '+5511988887777'},
            'Commissions': {'charge_amount': '99700'}, 'TrackingParameters': {'src': 'aula3'}}

def post_kiwify(c, url, body, token='tok-kiwify-123'):
    raw = json.dumps(body).encode()
    sig = hmac.new(token.encode(), raw, hashlib.sha1).hexdigest()
    return c.post(url.replace('https://app.oinanda.com.br', '') + '?signature=' + sig, content=raw, headers={'Content-Type': 'application/json'})

def test_kiwify_boleto_recuperacao_venda_e_reembolso():
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Ana Dona', 'email': 'ana@lc.com', 'password': 'senha-forte-1', 'company': 'Escola Online', 'segment': 'lancamento'})
        cid = c.get('/api/auth/me').json()['company']['id']
        h = c.post('/api/checkout/hooks', json={'provider': 'kiwify'}).json()
        assert h['url'].startswith('https://') and not h['has_secret']
        # sem o código de segurança, nada entra
        assert post_kiwify(c, h['url'], kiwify_body('billet_created')).status_code == 401
        h = c.post('/api/checkout/hooks', json={'provider': 'kiwify', 'secret': 'tok-kiwify-123'}).json(); assert h['has_secret']
        # assinatura errada: recusa e mostra o erro na tela
        assert post_kiwify(c, h['url'], kiwify_body('billet_created'), token='outro-token').status_code == 401
        assert c.get('/api/checkout/hooks').json()['items'][0]['last_error']
        # boleto gerado: cria o contato pelo celular e guarda o pendente
        assert post_kiwify(c, h['url'], kiwify_body('billet_created')).status_code == 200
        assert post_kiwify(c, h['url'], kiwify_body('billet_created')).json().get('duplicate')
        ct = c.portal.call(lambda: db.contacts.find_one({'company_id': cid, 'wa_id': '5511988887777'}, {'_id': 0}))
        assert ct and ct['name'] == 'Bruno Lima' and ct['email'] == 'bruno@x.com'
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'kind': 'pending'}, {'_id': 0}))
        assert ev['value'] == 997.0 and ev['url'] == 'https://kiwify.com.br/boleto/abc' and not ev['resolved']
        # automação "Boleto ou Pix sem pagar" (6 h): pega o evento com o link e o produto
        a = c.post('/api/automations', json={'recipe': 'boleto_pix'}).json()
        c.portal.call(lambda: db.checkout_events.update_one({'id': ev['id']}, {'$set': {'at': now() - timedelta(hours=7)}}))
        comp = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        auto = c.portal.call(lambda: db.automations.find_one({'id': a['id']}, {'_id': 0}))
        cand = c.portal.call(lambda: automations.candidates(auto, comp, []))
        assert len(cand) == 1 and cand[0][2]['_extra']['link_pagamento'] == 'https://kiwify.com.br/boleto/abc' and cand[0][2]['_extra']['produto'] == 'Curso Vendas 10x'
        msg = automations.render(auto['message'], {**{'primeiro_nome': 'Bruno'}, **cand[0][2]['_extra']})
        assert 'Curso Vendas 10x' in msg and 'kiwify.com.br/boleto/abc' in msg and '{' not in msg
        # pagou: venda registrada, card na etapa de venda e o boleto sai da recuperação
        assert post_kiwify(c, h['url'], kiwify_body('order_approved', status='paid')).status_code == 200
        post_kiwify(c, h['url'], kiwify_body('order_approved', status='paid'))  # repetido não duplica
        sales = c.portal.call(lambda: db.sales.find({'company_id': cid}, {'_id': 0}).to_list(10))
        assert len(sales) == 1 and sales[0]['total'] == 997.0 and sales[0]['created_by_name'] == 'Kiwify'
        ct = c.portal.call(lambda: db.contacts.find_one({'id': ct['id']}, {'_id': 0}))
        assert ct['sales_count'] == 1 and ct['stage'] == next(s['id'] for s in c.get('/api/funnel/stages').json() if s['kind'] == 'won')
        assert c.portal.call(lambda: automations.candidates(auto, comp, [])) == []
        st = c.get('/api/checkout/hooks').json()['stats']; assert st['approved'] == 1 and st['pending'] == 1 and st['recovered'] == 1
        # reembolso: a venda é cancelada
        post_kiwify(c, h['url'], kiwify_body('order_refunded', status='refunded'))
        assert c.portal.call(lambda: db.sales.find_one({'company_id': cid}, {'_id': 0}))['status'] == 'cancelled'
        assert c.get(f'/api/contacts/{ct["id"]}/checkout').status_code == 200

def test_hotmart_e_eduzz_conferem_a_origem_e_carrinho_abandonado():
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Leo Dono', 'email': 'leo@lc.com', 'password': 'senha-forte-1', 'company': 'Mentoria Leo', 'segment': 'lancamento'})
        cid = c.get('/api/auth/me').json()['company']['id']
        hm = c.post('/api/checkout/hooks', json={'provider': 'hotmart', 'secret': 'hottok-segredo'}).json()
        path = hm['url'].replace('https://app.oinanda.com.br', '')
        cart = {'event': 'PURCHASE_OUT_OF_SHOPPING_CART', 'id': 'e1', 'data': {'buyer': {'name': 'Carla Dias', 'email': 'carla@x.com', 'phone': '11 97777-6666'}, 'product': {'id': 9, 'name': 'Mentoria'}}}
        assert c.post(path, json=cart, headers={'X-HOTMART-HOTTOK': 'errado'}).status_code == 401
        assert c.post(path, json=cart, headers={'X-HOTMART-HOTTOK': 'hottok-segredo', 'Origin': 'https://hotmart.com'}).status_code == 200
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'kind': 'abandoned'}, {'_id': 0}))
        assert ev and ev['product'] == 'Mentoria'
        # evento que não usamos: aceita e ignora
        assert c.post(path, json={'event': 'CLUB_FIRST_ACCESS', 'data': {}}, headers={'X-HOTMART-HOTTOK': 'hottok-segredo'}).json()['ignored']
        ed = c.post('/api/checkout/hooks', json={'provider': 'eduzz', 'secret': 'chave-eduzz'}).json()
        body = json.dumps({'id': 'w1', 'event': 'myeduzz.invoice_paid', 'data': {'id': 'inv-77', 'status': 'paid', 'buyer': {'name': 'Carla Dias', 'email': 'carla@x.com', 'cellphone': '11977776666'},
                           'items': [{'productId': '9', 'name': 'Mentoria'}], 'paid': {'currency': 'BRL', 'value': 1497}, 'paymentMethod': 'creditCard'}}).encode()
        sig = hmac.new(b'chave-eduzz', body, hashlib.sha256).hexdigest()
        r = c.post(ed['url'].replace('https://app.oinanda.com.br', ''), content=body, headers={'Content-Type': 'application/json', 'x-signature': sig})
        assert r.status_code == 200
        s = c.portal.call(lambda: db.sales.find_one({'company_id': cid}, {'_id': 0}))
        assert s['total'] == 1497 and s['provider'] == 'eduzz' and s['payment'] == 'cartao'
        # a mesma pessoa (mesmo celular): o carrinho abandonado da Hotmart foi resolvido com a compra
        assert c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'kind': 'abandoned'}, {'_id': 0}))['resolved']

def test_iugu_fatura_buscada_na_api(monkeypatch):
    import checkout, base64
    faturas = {'INV1': {'id': 'INV1', 'status': 'pending', 'payable_with': 'bank_slip', 'total_cents': 49900, 'secure_url': 'https://faturas.iugu.com/inv1',
                        'items': [{'description': 'Plano Mensal Studio', 'quantity': 1, 'price_cents': 49900}], 'payer_name': 'Rita Souza', 'email': 'rita@x.com',
                        'variables': [{'variable': 'payer.phone_prefix', 'value': '11'}, {'variable': 'payer.phone', 'value': '966665555'}]}}
    pedidos = []
    async def fake_inv(tok, iid): pedidos.append((tok, iid)); return faturas[iid]
    monkeypatch.setattr(checkout, 'iugu_invoice', fake_inv)
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Rui Dono', 'email': 'rui@iu.com', 'password': 'senha-forte-1', 'company': 'Studio Rui', 'segment': 'studio'})
        cid = c.get('/api/auth/me').json()['company']['id']
        h = c.post('/api/checkout/hooks', json={'provider': 'iugu', 'secret': 'autoriza-123'}).json()
        assert not h['has_secret']  # falta o token da API
        h = c.post('/api/checkout/hooks', json={'provider': 'iugu', 'api_token': 'tok-api-iugu-123456'}).json(); assert h['has_secret']
        path = h['url'].replace('https://app.oinanda.com.br', '')
        form = 'event=invoice.created&data%5Bid%5D=INV1&data%5Bstatus%5D=pending'
        hdr = {'Content-Type': 'application/x-www-form-urlencoded'}
        assert c.post(path, content=form, headers=hdr).status_code == 401
        assert c.post(path, content=form, headers={**hdr, 'Authorization': 'Basic ' + base64.b64encode(b'autoriza-123:').decode()}).status_code == 200
        assert pedidos == [('tok-api-iugu-123456', 'INV1')]
        ct = c.portal.call(lambda: db.contacts.find_one({'company_id': cid, 'wa_id': '5511966665555'}, {'_id': 0}))
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid}, {'_id': 0}))
        assert ct['name'] == 'Rita Souza' and ev['kind'] == 'pending' and ev['value'] == 499.0 and ev['url'] == 'https://faturas.iugu.com/inv1'
        faturas['INV1'] = {**faturas['INV1'], 'status': 'paid', 'total_paid_cents': 49900}
        c.post(path, content='event=invoice.status_changed&data%5Bid%5D=INV1&data%5Bstatus%5D=paid', headers={**hdr, 'Authorization': 'autoriza-123'})
        s = c.portal.call(lambda: db.sales.find_one({'company_id': cid}, {'_id': 0}))
        assert s['total'] == 499.0 and s['provider'] == 'iugu' and s['payment'] == 'boleto' and s['items'][0]['name'] == 'Plano Mensal Studio'
        assert c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'kind': 'pending'}, {'_id': 0}))['resolved']
