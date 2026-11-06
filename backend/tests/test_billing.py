import os, json
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import billing
from core import db, now
from main import app

def test_planos(monkeypatch):
    calls, subs = [], {}
    async def fake_mp(method, path, body=None):
        calls.append((method, path, body))
        if method == 'POST' and path == '/preapproval':
            sid = 'sub%d' % (len(subs) + 1); subs[sid] = {'id': sid, 'status': 'pending', 'external_reference': body['external_reference']}
            return {'id': sid, 'init_point': 'https://www.mercadopago.com.br/subscriptions/checkout?preapproval_id=' + sid}
        if method == 'GET' and path.startswith('/preapproval/'): return subs[path.split('/')[-1]]
        if method == 'PUT': subs[path.split('/')[-1]]['status'] = body['status']; return subs[path.split('/')[-1]]
        if path.startswith('/authorized_payments/'): return {'preapproval_id': 'sub1'}
        return {}
    monkeypatch.setattr(billing, 'mp', fake_mp); monkeypatch.setenv('MP_ACCESS_TOKEN', 'APP_USR-x')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona B', 'email': 'db@x.com', 'password': 'senha-forte-1', 'company': 'Loja B', 'segment': 'loja'})
        b = c.get('/api/billing').json()
        assert b['state'] == 'trial' and b['trial_left'] == 14 and b['limits']['users'] == 12 and b['plans']['essencial']['price'] == 147
        assert c.get('/api/auth/me').json()['access']['state'] == 'trial'
        # fim do teste: só leitura
        async def vence():
            await db.companies.update_many({}, {'$set': {'trial_ends_at': now() - timedelta(days=1)}})
        c.portal.call(vence)
        assert c.get('/api/billing').json()['state'] == 'readonly'
        r = c.post('/api/team', json={'name': 'Ana', 'email': 'ab@x.com', 'role': 'agent'})
        assert r.status_code == 402 and 'só para leitura' in r.json()['detail']
        assert c.get('/api/conversations').status_code == 200  # ler continua
        # assina o essencial (anual, 1 usuário extra)
        r = c.post('/api/billing/subscribe', json={'plan': 'essencial', 'cycle': 'annual', 'extra_users': 1, 'email': 'db@x.com'}).json()
        assert r['url'].endswith('sub1') and r['amount'] == (147 + 39) * 10
        body = calls[-1][2]
        assert body['auto_recurring'] == {'frequency': 12, 'frequency_type': 'months', 'transaction_amount': 1860, 'currency_id': 'BRL'} and body['status'] == 'pending'
        assert c.get('/api/billing').json()['state'] == 'readonly'  # ainda não pagou
        subs['sub1'].update(status='authorized', next_payment_date='2027-11-06T10:00:00.000-03:00')
        c.post('/api/billing/webhook', json={'type': 'subscription_preapproval', 'data': {'id': 'sub1'}})
        b = c.get('/api/billing').json()
        assert b['state'] == 'active' and b['plan'] == 'essencial' and b['limits']['users'] == 3 and b['billing']['status'] == 'active'
        # limites do plano
        for i in range(2): assert c.post('/api/team', json={'name': f'P{i}', 'email': f'p{i}@x.com', 'role': 'agent'}).status_code == 200
        r = c.post('/api/team', json={'name': 'P3', 'email': 'p3@x.com', 'role': 'agent'})
        assert r.status_code == 402 and 'permite 3 pessoas' in r.json()['detail']
        r = c.post('/api/teams', json={'name': 'Vendas', 'member_ids': []})
        assert r.status_code == 402 and 'plano Equipe' in r.json()['detail']
        assert c.post('/api/goals', json={'name': 'Meta X', 'metric': 'revenue', 'scope': 'company', 'target': 10}).status_code == 402
        co = c.portal.call(lambda: db.companies.find_one({'name': 'Loja B'}, {'_id': 0}))
        assert not billing.has(co, 'auto') and billing.has(co, 'automacoes') and billing.limits(co)['ai'] == 300
        # troca de plano: cancela a antiga quando a nova for paga
        c.post('/api/billing/subscribe', json={'plan': 'equipe', 'cycle': 'monthly', 'email': 'db@x.com'})
        assert ('PUT', '/preapproval/sub1', {'status': 'cancelled'}) in calls
        subs['sub2'].update(status='authorized', next_payment_date='2026-12-06T10:00:00.000-03:00')
        b = c.post('/api/billing/sync').json()
        assert b['plan'] == 'equipe' and b['limits']['users'] == 12 and b['billing']['amount'] == 597
        assert c.post('/api/teams', json={'name': 'Vendas', 'member_ids': []}).status_code == 200
        # cancelar: continua até o fim do período pago
        assert c.post('/api/billing/cancel').status_code == 200
        b = c.get('/api/billing').json()
        assert b['state'] == 'active' and b['billing']['status'] == 'cancelled'

def test_assinatura_webhook():
    import hmac, hashlib
    class R:
        headers = {}
    os.environ['MP_WEBHOOK_SECRET'] = 's3'
    try:
        r = R(); ts = '1700000000'; msg = f'id:abc;request-id:rq1;ts:{ts};'
        v1 = hmac.new(b's3', msg.encode(), hashlib.sha256).hexdigest()
        r.headers = {'x-signature': f'ts={ts},v1={v1}', 'x-request-id': 'rq1'}
        assert billing.signature_ok(r, 'abc')
        r.headers = {'x-signature': f'ts={ts},v1=errado', 'x-request-id': 'rq1'}
        assert not billing.signature_ok(r, 'abc')
    finally: del os.environ['MP_WEBHOOK_SECRET']
