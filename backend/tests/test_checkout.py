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
        assert st['recovered_value'] == 997.0 and st['approved_value'] == 997.0
        evs = c.get(f"/api/checkout/hooks/{h['id']}/events").json()['events']
        assert evs[0]['kind'] == 'approved' and evs[0]['name'] == 'Bruno Lima' and any(e['kind'] == 'pending' and e['resolved'] for e in evs)
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

def test_cakto_asaas_e_mercado_pago(monkeypatch):
    import checkout, time
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Mia Dona', 'email': 'mia@pg.com', 'password': 'senha-forte-1', 'company': 'Clínica Mia', 'segment': 'clinica'})
        cid = c.get('/api/auth/me').json()['company']['id']
        P = lambda h: h['url'].replace('https://app.oinanda.com.br', '')
        # Cakto: assinatura no cabeçalho (timestamp.corpo) ou o secret no corpo
        ck = c.post('/api/checkout/hooks', json={'provider': 'cakto', 'secret': 'segredo-cakto'}).json()
        body = {'secret': 'segredo-cakto', 'event': 'pix_gerado', 'data': {'id': 'ck1', 'status': 'waiting_payment', 'checkoutUrl': 'https://pay.cakto.com.br/a8', 'amount': 350.0,
                'paymentMethod': 'pix', 'customer': {'name': 'Nina Paz', 'email': 'nina@x.com', 'phone': '5511955554444'}, 'product': {'name': 'Pacote 4 sessões'}, 'pix': {'qrCode': '000201pix'}}}
        raw = json.dumps({**body, 'secret': 'outro'}).encode(); ts = str(int(time.time()))
        sig = 'v1=' + hmac.new(b'segredo-cakto', ts.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        assert c.post(P(ck), content=raw, headers={'Content-Type': 'application/json', 'X-Cakto-Timestamp': ts, 'X-Cakto-Signature': sig}).status_code == 200
        assert c.post(P(ck), json={**body, 'secret': 'errado', 'data': {**body['data'], 'id': 'ck2'}}).status_code == 401
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'cakto'}, {'_id': 0}))
        assert ev['kind'] == 'pending' and ev['pix'] == '000201pix' and ev['url'] == 'https://pay.cakto.com.br/a8'
        # Asaas: token no cabeçalho e cliente lido na API; mensalidade futura não entra
        async def fake_cus(key, cus): assert key == 'aact_chave_api_123'; return {'name': 'Otto Reis', 'email': 'otto@x.com', 'mobilePhone': '11944443333'}
        monkeypatch.setattr(checkout, 'asaas_customer', fake_cus)
        tok = 'tok-asaas-' + 'x' * 30
        asa = c.post('/api/checkout/hooks', json={'provider': 'asaas', 'secret': tok, 'api_token': 'aact_chave_api_123'}).json(); assert asa['has_secret']
        from datetime import date, timedelta as td
        pay = {'id': 'pay_1', 'customer': 'cus_1', 'value': 280.0, 'billingType': 'BOLETO', 'status': 'PENDING', 'description': 'Mensalidade dezembro', 'invoiceUrl': 'https://www.asaas.com/i/pay_1'}
        H = {'asaas-access-token': tok}
        c.post(P(asa), json={'id': 'evt1', 'event': 'PAYMENT_CREATED', 'payment': {**pay, 'dueDate': str(date.today() + td(days=10))}}, headers=H)
        assert not c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'asaas'}))
        assert c.post(P(asa), json={'id': 'evt2', 'event': 'PAYMENT_OVERDUE', 'payment': pay}, headers={'asaas-access-token': 'errado'}).status_code == 401
        c.post(P(asa), json={'id': 'evt2', 'event': 'PAYMENT_OVERDUE', 'payment': pay}, headers=H)
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'asaas'}, {'_id': 0}))
        assert ev['kind'] == 'pending' and ev['url'] == 'https://www.asaas.com/i/pay_1' and ev['value'] == 280.0
        c.post(P(asa), json={'id': 'evt3', 'event': 'PAYMENT_RECEIVED', 'payment': {**pay, 'status': 'RECEIVED'}}, headers=H)
        s = c.portal.call(lambda: db.sales.find_one({'company_id': cid, 'provider': 'asaas'}, {'_id': 0}))
        assert s['total'] == 280.0 and s['contact_name'] == 'Otto Reis'
        assert c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'asaas', 'kind': 'pending'}, {'_id': 0}))['resolved']
        # Mercado Pago: x-signature com o manifesto e pagamento lido na API
        async def fake_pay(tok, pid): return {'id': int(pid), 'status': 'approved', 'payment_type_id': 'bank_transfer', 'transaction_amount': 150,
                                              'payer': {'email': 'lu@x.com', 'first_name': 'Lu', 'last_name': 'Prado', 'phone': {'area_code': '11', 'number': '933332222'}},
                                              'additional_info': {'items': [{'title': 'Avaliação premium'}]}}
        monkeypatch.setattr(checkout, 'mp_payment', fake_pay)
        mp = c.post('/api/checkout/hooks', json={'provider': 'mercadopago', 'secret': 'assinatura-mp', 'api_token': 'APP_USR-123456789'}).json()
        ts = str(int(time.time())); man = f'id:987654;request-id:req-1;ts:{ts};'
        v1 = hmac.new(b'assinatura-mp', man.encode(), hashlib.sha256).hexdigest()
        r = c.post(P(mp) + '?data.id=987654&type=payment', json={'action': 'payment.updated', 'type': 'payment', 'data': {'id': '987654'}},
                   headers={'x-signature': f'ts={ts},v1={v1}', 'x-request-id': 'req-1'})
        assert r.status_code == 200
        s = c.portal.call(lambda: db.sales.find_one({'company_id': cid, 'provider': 'mercadopago'}, {'_id': 0}))
        assert s['total'] == 150 and s['payment'] == 'pix' and s['items'][0]['name'] == 'Avaliação premium'
        ct = c.portal.call(lambda: db.contacts.find_one({'company_id': cid, 'wa_id': '5511933332222'}, {'_id': 0}))
        assert ct and ct['name'] == 'Lu Prado'
        bad = c.post(P(mp) + '?data.id=987654&type=payment', json={'type': 'payment', 'data': {'id': '987654'}}, headers={'x-signature': f'ts={ts},v1=abc', 'x-request-id': 'req-1'})
        assert bad.status_code == 401

def test_vindi_pagarme_stripe_e_pagbank(monkeypatch):
    import checkout, time
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Gil Dono', 'email': 'gil@pg.com', 'password': 'senha-forte-1', 'company': 'Academia Gil', 'segment': 'studio'})
        cid = c.get('/api/auth/me').json()['company']['id']
        P = lambda h: h['url'].replace('https://app.oinanda.com.br', '')
        sale = lambda prov: c.portal.call(lambda: db.sales.find_one({'company_id': cid, 'provider': prov}, {'_id': 0}))
        # Vindi: o aviso só diz a fatura; vale o que a API confirma
        async def fake_vindi(key, path):
            assert key == 'chave-vindi-123456'
            if path.startswith('bills/'): return {'bill': {'id': 77, 'amount': '189.9', 'status': 'paid', 'url': 'https://app.vindi.com.br/customer/bills/77', 'customer': {'id': 5, 'name': 'Iara Lopes'},
                                                           'charges': [{'payment_method': {'code': 'credit_card'}}], 'bill_items': [{'product': {'name': 'Plano mensal'}}]}}
            return {'customer': {'id': 5, 'name': 'Iara Lopes', 'email': 'iara@x.com', 'phones': [{'phone_type': 'mobile', 'number': '5511922221111'}]}}
        monkeypatch.setattr(checkout, 'vindi_get', fake_vindi)
        vi = c.post('/api/checkout/hooks', json={'provider': 'vindi', 'api_token': 'chave-vindi-123456'}).json(); assert vi['has_secret']
        assert c.post(P(vi), json={'event': {'type': 'bill_paid', 'data': {'bill': {'id': 77}}}}).status_code == 200
        assert sale('vindi')['total'] == 189.9 and sale('vindi')['contact_name'] == 'Iara Lopes'
        # Pagar.me: pedido confirmado na API (o aviso dizendo "pago" não basta)
        estado = {'status': 'pending'}
        async def fake_pm(key, oid): return {'id': oid, 'status': estado['status'], 'amount': 49700, 'customer': {'name': 'Caio Mendes', 'email': 'caio@x.com', 'phones': {'mobile_phone': {'country_code': '55', 'area_code': '11', 'number': '911112222'}}},
                                             'items': [{'description': 'Pacote trimestral'}], 'charges': [{'payment_method': 'pix', 'last_transaction': {'qr_code': '000201', 'qr_code_url': 'https://api.pagar.me/qr/1'}}]}
        monkeypatch.setattr(checkout, 'pagarme_order', fake_pm)
        pg = c.post('/api/checkout/hooks', json={'provider': 'pagarme', 'api_token': 'sk_test_abcdef123'}).json()
        c.post(P(pg), json={'id': 'hook_1', 'type': 'order.paid', 'data': {'id': 'or_1'}})
        assert not sale('pagarme')  # a API ainda diz pendente: nada de venda
        c.post(P(pg), json={'id': 'hook_0', 'type': 'order.created', 'data': {'id': 'or_1'}})
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'pagarme'}, {'_id': 0}))
        assert ev['kind'] == 'pending' and ev['pix'] == '000201' and ev['value'] == 497.0
        estado['status'] = 'paid'
        c.post(P(pg), json={'id': 'hook_2', 'type': 'order.paid', 'data': {'id': 'or_1'}})
        assert sale('pagarme')['total'] == 497.0 and sale('pagarme')['payment'] == 'pix'
        # Stripe: assinatura t.corpo e checkout expirado = carrinho abandonado
        st = c.post('/api/checkout/hooks', json={'provider': 'stripe', 'secret': 'whsec_teste123'}).json()
        def stripe(body, secret=b'whsec_teste123'):
            raw = json.dumps(body).encode(); t = str(int(time.time()))
            v1 = hmac.new(secret, t.encode() + b'.' + raw, hashlib.sha256).hexdigest()
            return c.post(P(st), content=raw, headers={'Content-Type': 'application/json', 'Stripe-Signature': f't={t},v1={v1}'})
        cd = {'name': 'Rui Prado', 'email': 'rui@x.com', 'phone': '+5511900001111'}
        assert stripe({'type': 'checkout.session.expired', 'data': {'object': {'id': 'cs_1', 'amount_total': 29700, 'customer_details': cd, 'metadata': {'produto': 'Mentoria'},
                       'after_expiration': {'recovery': {'url': 'https://buy.stripe.com/r/cs_1'}}}}}).status_code == 200
        assert stripe({'type': 'x'}, secret=b'outro').status_code == 401
        ev = c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'stripe'}, {'_id': 0}))
        assert ev['kind'] == 'abandoned' and ev['url'] == 'https://buy.stripe.com/r/cs_1' and ev['product'] == 'Mentoria'
        stripe({'type': 'checkout.session.completed', 'data': {'object': {'id': 'cs_2', 'payment_intent': 'pi_2', 'payment_status': 'paid', 'amount_total': 29700, 'customer_details': cd}}})
        assert sale('stripe')['total'] == 297.0
        assert c.portal.call(lambda: db.checkout_events.find_one({'company_id': cid, 'provider': 'stripe', 'kind': 'abandoned'}, {'_id': 0}))['resolved']
        stripe({'type': 'charge.refunded', 'data': {'object': {'id': 'ch_2', 'payment_intent': 'pi_2', 'amount_refunded': 29700, 'billing_details': cd}}})
        assert sale('stripe')['status'] == 'cancelled'
        # PagBank: x-authenticity-token = sha256(token-corpo)
        pb = c.post('/api/checkout/hooks', json={'provider': 'pagbank', 'secret': 'token-pagbank-123'}).json()
        order = {'id': 'ORDE_1', 'customer': {'name': 'Bia Luz', 'email': 'bia@x.com', 'phones': [{'country': '55', 'area': '11', 'number': '977776666'}]}, 'items': [{'name': 'Avaliação física'}],
                 'charges': [{'id': 'CHAR_1', 'status': 'PAID', 'amount': {'value': 12000}, 'payment_method': {'type': 'CREDIT_CARD'}}]}
        raw = json.dumps(order).encode()
        assert c.post(P(pb), content=raw, headers={'Content-Type': 'application/json', 'x-authenticity-token': 'errado'}).status_code == 401
        tok = hashlib.sha256(b'token-pagbank-123-' + raw).hexdigest()
        assert c.post(P(pb), content=raw, headers={'Content-Type': 'application/json', 'x-authenticity-token': tok}).status_code == 200
        assert sale('pagbank')['total'] == 120.0 and sale('pagbank')['items'][0]['name'] == 'Avaliação física'
