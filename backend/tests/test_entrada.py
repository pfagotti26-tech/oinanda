"""Entrada nova: conta em modo demonstração, 14 dias grátis com cartão (free_trial no Mercado Pago), Nanda cobrada por atendimento,
pacotes extras (clique e automático) somados na próxima mensalidade."""
import os
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import billing, notices, mailer, ai
from core import db, now
from main import app


def fake_mp(monkeypatch):
    calls, subs = [], {}
    async def mp(method, path, body=None):
        calls.append((method, path, body))
        if method == 'POST' and path == '/preapproval':
            sid = 'sub%d' % (len(subs) + 1)
            subs[sid] = {'id': sid, 'status': 'pending', 'external_reference': body['external_reference']}
            return {'id': sid, 'init_point': 'https://mp/checkout?' + sid}
        if method == 'GET' and path.startswith('/preapproval/'): return subs[path.split('/')[-1]]
        if method == 'PUT':
            s = subs[path.split('/')[-1]]
            if 'status' in body: s['status'] = body['status']
            return s
        return {}
    monkeypatch.setattr(billing, 'mp', mp); monkeypatch.setenv('MP_ACCESS_TOKEN', 'APP_USR-x')
    return calls, subs


def test_demo_teste_com_cartao_e_atendimentos(monkeypatch):
    monkeypatch.setenv('SIGNUP_MODE', 'demo')
    calls, subs = fake_mp(monkeypatch); mailer.SENT.clear()
    async def fake_llm(system, messages, max_tokens=500):
        assert ai.CACHE_SPLIT in system and system.index('<conhecimento>') < system.index(ai.CACHE_SPLIT) < system.index('Hoje é')
        return '{"reply": "Oi! Posso ajudar?", "handoff": false}', {'input_tokens': 100, 'output_tokens': 10, 'cache_read_input_tokens': 900}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        r = c.post('/api/auth/signup', json={'name': 'Renata Lopes', 'email': 'r@x.com', 'password': 'senha-forte-1', 'company': 'Bella Pele',
                                              'segment': 'clinica', 'phone': '11 98765-4321', 'wa_optin': True})
        assert r.status_code == 200
        me = c.get('/api/auth/me').json(); cid = me['company']['id']
        assert me['access']['state'] == 'demo' and me['company']['notice_wa'] is True and me['company']['wa_optin']['on']
        b = c.get('/api/billing').json()
        assert b['state'] == 'demo' and b['trial_offer'] and b['limits']['users'] == 1
        # demonstração: conversa com a Nanda, mas não conecta o WhatsApp nem chama o time
        assert c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'Quanto custa?'}]}).status_code == 200
        r = c.post('/api/wa/connect', json={'phone_number_id': '5550001', 'waba_id': '5550002', 'token': 'EAA' + 'q' * 30})
        assert r.status_code == 402 and '14 dias grátis' in r.json()['detail']
        assert c.post('/api/team', json={'name': 'Ana', 'email': 'a@x.com', 'role': 'agent'}).status_code == 402
        # limite de conversas de teste no modo demonstração
        c.portal.call(lambda: db.ai_usage.update_one({'company_id': cid, 'month': billing.month()}, {'$set': {'tests': billing.DEMO_TESTS}}))
        assert c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'Oi'}]}).status_code == 402
        # lembrete gentil no dia seguinte, uma vez só
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'created_at': now() - timedelta(days=1, hours=1)}}))
        monkeypatch.setattr(notices, 'quiet', lambda: False)
        c.portal.call(notices.tick); c.portal.call(notices.tick)
        assert [m['subject'] for m in mailer.SENT].count('A Nanda da Bella Pele está esperando') == 1

        # ativa: assinatura com 14 dias grátis
        r = c.post('/api/billing/subscribe', json={'plan': 'profissional', 'email': 'r@x.com'}).json()
        assert r['trial'] and r['amount'] == 297
        rec = calls[-1][2]['auto_recurring']
        assert rec['free_trial'] == {'frequency': 14, 'frequency_type': 'days'} and rec['transaction_amount'] == 297
        first = now() + timedelta(days=14)
        subs['sub1'].update(status='authorized', next_payment_date=first.isoformat().replace('+00:00', 'Z'))
        b = c.post('/api/billing/sync').json()
        assert b['state'] == 'trial' and b['plan'] == 'profissional' and b['trial_left'] in (14, 15) and not b['trial_offer']
        assert b['limits']['atend'] == 600 and b['limits']['users'] == 5
        co = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        assert co['trial_used'] and co['plan'] == 'profissional'
        # assinar de novo não ganha outro teste
        c.post('/api/billing/subscribe', json={'plan': 'equipe', 'email': 'r@x.com'})
        assert 'free_trial' not in calls[-1][2]['auto_recurring']
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$unset': {'billing.pending': '', 'billing.pending_url': ''}}))
        # 3 dias antes da primeira mensalidade: avisa com valor e data
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'billing.trial_until': now() + timedelta(days=2, hours=12)}}))
        c.portal.call(notices.tick); c.portal.call(notices.tick)
        assert sum(1 for m in mailer.SENT if m['subject'].startswith('Seu teste grátis termina em')) == 1
        assert 'R$ 297' in next(m['html'] for m in mailer.SENT if m['subject'].startswith('Seu teste grátis termina em'))

        # atendimentos: um cliente num dia conta uma vez
        async def atende(contact):
            await ai.track(cid, {'input_tokens': 10, 'output_tokens': 5}, 'replies', contact)
        c.portal.call(lambda: atende('k1')); c.portal.call(lambda: atende('k1')); c.portal.call(lambda: atende('k2'))
        u = c.get('/api/billing/ai').json()
        assert u['atend'] == {'used': 2, 'quota': 600, 'plan': 600}
        # cota acabou: só quem já foi atendido hoje segue; os novos ficam no copiloto
        c.portal.call(lambda: db.ai_usage.update_one({'company_id': cid, 'month': billing.month()}, {'$set': {'atend': 600}}))
        co = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        assert c.portal.call(lambda: billing.ai_allowed(co, 'auto', 'k1'))
        assert not c.portal.call(lambda: billing.ai_allowed(co, 'auto', 'k9'))
        assert c.portal.call(lambda: billing.ai_allowed(co, 'sug'))
        # pacote com um clique
        u = c.post('/api/billing/ai/pack').json()
        assert u['atend']['quota'] == 1100 and u['packs'][0]['how'] == 'click'
        # pacote automático até 1 por mês
        c.put('/api/billing/ai/auto', json={'max': 1})
        c.portal.call(lambda: db.ai_usage.update_one({'company_id': cid, 'month': billing.month()}, {'$set': {'atend': 1100}}))
        co = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        assert c.portal.call(lambda: billing.ai_allowed(co, 'auto', 'k10'))
        c.portal.call(lambda: db.ai_usage.update_one({'company_id': cid, 'month': billing.month()}, {'$set': {'atend': 1600}}))
        assert not c.portal.call(lambda: billing.ai_allowed(co, 'auto', 'k11'))  # teto do automático
        assert c.get('/api/billing/ai').json()['atend']['quota'] == 1600
        assert c.put('/api/billing/ai/auto', json={'max': 99}).status_code == 422

        # os pacotes entram na próxima mensalidade e depois o valor volta ao do plano
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'billing.next_payment': now() + timedelta(days=1)}}))
        c.portal.call(billing.bill_packs)
        put = [x for x in calls if x[0] == 'PUT' and 'auto_recurring' in (x[2] or {})]
        assert put[-1][2]['auto_recurring']['transaction_amount'] == 297 + 2 * 59
        co = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        assert co['billing']['extra_due'] == 118
        c.portal.call(billing.bill_packs)  # não soma duas vezes
        assert len([x for x in calls if x[0] == 'PUT' and 'auto_recurring' in (x[2] or {})]) == len(put)
        inv = {'id': 'inv1', 'preapproval_id': 'sub1', 'status': 'processed', 'transaction_amount': 415, 'debit_date': co['billing']['next_payment'].isoformat(),
               'payment': {'status': 'approved'}}
        c.portal.call(lambda: billing.record_payment(inv, co))
        put = [x for x in calls if x[0] == 'PUT' and 'auto_recurring' in (x[2] or {})]
        assert put[-1][2]['auto_recurring']['transaction_amount'] == 297
        assert all(p['status'] == 'pago' for p in c.get('/api/billing/ai').json()['packs'])

        # cancelar no teste: usa até o fim dos 14 dias e nada é cobrado
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'billing.trial_until': now() + timedelta(days=5)}}))
        r = c.post('/api/billing/cancel').json()
        assert r['in_trial']
        b = c.get('/api/billing').json()
        assert b['state'] == 'trial'
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'billing.trial_until': now() - timedelta(minutes=1), 'billing.paid_until': now() - timedelta(minutes=1)}}))
        assert c.get('/api/billing').json()['state'] == 'readonly'


def test_custo_de_ia_no_admin(monkeypatch):
    import adminapi
    v = adminapi.ai_cost(1_000_000, 100_000, 5_000_000, 0)
    assert v and v > 0
