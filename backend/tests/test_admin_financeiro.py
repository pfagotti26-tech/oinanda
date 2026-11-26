import os
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import billing, mailer
from core import db, now
from main import app

def mk(): return TestClient(app, base_url='https://testserver')

def fake(monkeypatch):
    """Mercado Pago de mentira: assinaturas em `subs`, faturas em `invs`."""
    subs, invs = {}, {}
    async def fake_mp(method, path, body=None):
        if method == 'POST' and path == '/preapproval':
            sid = 'sub%d' % (len(subs) + 1); subs[sid] = {'id': sid, 'status': 'pending', 'external_reference': body['external_reference']}
            return {'id': sid, 'init_point': 'https://mp/checkout?preapproval_id=' + sid}
        if method == 'GET' and path.startswith('/preapproval/'): return subs[path.split('/')[-1]]
        if method == 'PUT': subs[path.split('/')[-1]]['status'] = body['status']; return subs[path.split('/')[-1]]
        if path.startswith('/authorized_payments/search'):
            sid = path.split('preapproval_id=')[-1]
            return {'results': [i for i in invs.values() if i['preapproval_id'] == sid]}
        if path.startswith('/authorized_payments/'): return invs[path.split('/')[-1]]
        return {}
    monkeypatch.setattr(billing, 'mp', fake_mp); monkeypatch.setenv('MP_ACCESS_TOKEN', 'APP_USR-x')
    return subs, invs

def assina(c, subs, plan='essencial', nxt=None):
    # estes testes são de conta que já paga: o teste grátis acabou antes (assinar no meio do teste guarda os dias que faltam; ver test_teste_sem_cartao)
    cid = c.get('/api/auth/me').json()['company']['id']
    async def fim(): await db.companies.update_one({'id': cid}, {'$set': {'trial_ends_at': now() - timedelta(minutes=1)}})
    c.portal.call(fim)
    c.post('/api/billing/subscribe', json={'plan': plan, 'cycle': 'monthly', 'email': 'x@x.com'})
    sid = sorted(subs)[-1]
    subs[sid].update(status='authorized', next_payment_date=(nxt or now() + timedelta(days=30)).isoformat())
    c.post('/api/billing/webhook', json={'type': 'subscription_preapproval', 'data': {'id': sid}})
    return sid

def test_cancelou_e_o_periodo_pago_acabou(monkeypatch):
    subs, _ = fake(monkeypatch)
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Dona B', 'email': 'db@x.com', 'password': 'senha-forte-1', 'company': 'Loja B', 'segment': 'loja'})
        assina(c, subs)
        assert c.get('/api/billing').json()['state'] == 'active'
        assert c.post('/api/billing/cancel').status_code == 200
        assert c.get('/api/billing').json()['state'] == 'active'  # segue até o fim do período pago
        async def passa():
            await db.companies.update_many({}, {'$set': {'billing.paid_until': now() - timedelta(days=1), 'trial_ends_at': now() - timedelta(days=30)}})
        c.portal.call(passa)
        b = c.get('/api/billing').json()
        assert b['state'] == 'readonly'  # antes ficava ativa para sempre
        r = c.post('/api/team', json={'name': 'Ana', 'email': 'ab@x.com', 'role': 'agent'})
        assert r.status_code == 402
        # assinatura "ativa" com a data paga vencida também cai (cartão que parou de passar)
        async def ativa_vencida():
            await db.companies.update_many({}, {'$set': {'billing.status': 'active'}})
        c.portal.call(ativa_vencida)
        assert c.get('/api/billing').json()['state'] == 'readonly'

def test_cobrancas_registradas_e_avisos(monkeypatch):
    subs, invs = fake(monkeypatch); mailer.SENT.clear()
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Dona B', 'email': 'db@x.com', 'password': 'senha-forte-1', 'company': 'Loja B', 'segment': 'loja'})
        sid = assina(c, subs)
        # fatura paga: fica registrada, avisa o dono e estende o acesso
        invs['101'] = {'id': 101, 'preapproval_id': sid, 'transaction_amount': 147, 'debit_date': now().isoformat(), 'status': 'processed', 'retry_attempt': 1,
                       'payment': {'id': 9, 'status': 'approved', 'status_detail': 'accredited'}}
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '101'}})
        p = c.portal.call(lambda: db.payments.find_one({'id': '101'}, {'_id': 0}))
        assert p['status'] == 'paid' and p['amount'] == 147 and p['plan'] == 'essencial'
        assert any('Recebemos seu pagamento' in m['subject'] and m['to'] == 'db@x.com' for m in mailer.SENT)
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '101'}})  # aviso repetido não manda de novo
        assert sum(1 for m in mailer.SENT if 'Recebemos seu pagamento' in m['subject']) == 1
        # mês seguinte: cartão recusado
        invs['102'] = {'id': 102, 'preapproval_id': sid, 'transaction_amount': 147, 'debit_date': (now() - timedelta(days=10)).isoformat(), 'status': 'recycling', 'retry_attempt': 2,
                       'payment': {'id': 10, 'status': 'rejected', 'status_detail': 'cc_rejected_insufficient_amount'}}
        async def volta():
            await db.payments.update_one({'id': '101'}, {'$set': {'due': now() - timedelta(days=41)}})
            await db.companies.update_many({}, {'$set': {'billing.paid_until': now() - timedelta(days=7)}})
        c.portal.call(volta)
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '102'}})
        assert any('não passou' in m['subject'] for m in mailer.SENT)
        # o Mercado Pago ainda diz "authorized" com a próxima data lá na frente: o acesso NÃO se estende por cima da recusada
        b = c.get('/api/billing').json()
        assert b['state'] == 'readonly'
        # pagou na nova tentativa: volta
        invs['102']['payment']['status'] = 'approved'
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '102'}})
        assert c.get('/api/billing').json()['state'] == 'active'

def test_admin_financeiro_clientes_e_ficha(monkeypatch):
    subs, invs = fake(monkeypatch); mailer.SENT.clear()
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Renata Souza', 'email': 're@bella.com', 'password': 'senha-forte-1', 'company': 'Clínica Bella', 'segment': 'clinica', 'phone': '11 90000-0000'})
        assert c.get('/api/admin/overview').status_code == 403
        sid = assina(c, subs, 'profissional')
        invs['201'] = {'id': 201, 'preapproval_id': sid, 'transaction_amount': 297, 'debit_date': now().isoformat(), 'status': 'processed',
                       'payment': {'id': 1, 'status': 'approved'}}
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '201'}})
    with mk() as c2:
        c2.post('/api/auth/signup', json={'name': 'Diego', 'email': 'di@dom.com', 'password': 'senha-forte-1', 'company': 'Barbearia Dom', 'segment': 'salao'})
    with mk() as a:
        a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1', 'company': 'Oi Nanda'})
        o = a.get('/api/admin/overview').json()
        assert o['mrr'] == 297 and o['paying'] == 1 and o['trial'] == 1 and o['received'] == 297  # a conta do admin fica fora
        assert o['plans'][1] == {'plan': 'profissional', 'name': 'Profissional', 'count': 1, 'mrr': 297}
        assert o['revenue_months'][-1]['total'] == 297 and len(o['revenue_months']) == 6
        assert o['attention']['refused']['count'] == 0 and o['usage']['ai_cost'] == 0
        st = a.get('/api/admin/stats').json(); assert st['paying'] == 1 and st['companies'] == 3
        # financeiro
        ini, fim = (now() - timedelta(days=3)).strftime('%Y-%m-%d'), (now() + timedelta(days=40)).strftime('%Y-%m-%d')
        P = f'/api/admin/payments?start={ini}&end={fim}'
        f = a.get(P).json()
        assert f['totals']['received'] == 297 and f['totals']['to_receive'] == 297 and f['counts']['paid'] == 1 and f['counts']['scheduled'] == 1
        assert [x['status'] for x in f['rows']] == ['scheduled', 'paid'] and f['rows'][1]['company'] == 'Clínica Bella'
        assert len(a.get(P + '&status=paid').json()['rows']) == 1
        assert a.get(P + '&plan=essencial').json()['rows'] == []
        assert a.get(P + '&q=bella&vmin=200').json()['total'] == 2
        assert a.get('/api/admin/payments?start=2020-01-01&end=2020-01-31').json()['rows'] == []
        csv = a.get(P + '&fmt=csv'); assert 'Clínica Bella' in csv.text and 'text/csv' in csv.headers['content-type']
        assert len(f['upcoming']) == 0  # próxima cobrança em 30 dias, fora dos 7
        sm = f['summary']
        assert sm['mrr'] == 297 and sm['arr'] == 3564 and sm['paying'] == 1 and sm['ticket'] == 297 and sm['late']['n'] == 0
        assert sm['forecast']['n'] == 1 and sm['forecast']['total'] == 297 and len(sm['months']) == 12 and sm['months'][-1]['total'] == 297
        assert sm['cycles']['monthly']['n'] == 1 and sm['movement']['new'] == 297
        assert sm['nf']['missing'] == 1 and len(sm['subs']) == 1 and sm['subs'][0]['paid_total'] == 297 and sm['goal'] is None
        pid = next(x['id'] for x in f['rows'] if x['status'] == 'paid')
        assert a.put(f'/api/admin/payments/{pid}/nf', json={'done': True, 'number': '000123'}).status_code == 200
        f2 = a.get(P).json(); assert f2['summary']['nf']['missing'] == 0 and next(x for x in f2['rows'] if x['id'] == pid)['nf_number'] == '000123'
        assert a.get(P + '&status=nf').json()['rows'] == []
        a.put('/api/admin/platform', json={'mrr_goal': 5000}); assert a.get(P).json()['summary']['goal'] == 5000
        # clientes: abas, busca e filtros
        l = a.get('/api/admin/companies').json()
        assert l['counts'] == {'all': 2, 'paying': 1, 'courtesy': 0, 'trial': 1, 'readonly': 0, 'suspended': 0, 'risk': 0, 'test': 0} and l['total'] == 3
        assert [x['name'] for x in a.get('/api/admin/companies?tab=paying').json()['rows']] == ['Clínica Bella']
        assert [x['name'] for x in a.get('/api/admin/companies?q=diego').json()['rows']] == ['Barbearia Dom']
        assert a.get('/api/admin/companies?wa=yes').json()['rows'] == []
        assert a.get('/api/admin/companies?by=created&start=2020-01-01&end=2020-01-02').json()['rows'] == []
        assert 'Barbearia Dom' in a.get('/api/admin/companies.csv').text
        bella = next(x for x in l['rows'] if x['name'] == 'Clínica Bella'); dom = next(x for x in l['rows'] if x['name'] == 'Barbearia Dom')
        assert dom['health']['level'] in ('ok', 'atencao') and bella['monthly'] == 297 and l['summary']['mrr'] == 297 and l['counts']['risk'] == 0
        assert [x['name'] for x in a.get('/api/admin/companies?plan=profissional&cycle=monthly&vmin=200').json()['rows']] == ['Clínica Bella']
        assert a.get('/api/admin/companies?vmin=300').json()['rows'] == [] and a.get('/api/admin/companies?q=90000').json()['rows'][0]['name'] == 'Clínica Bella'
        assert [x['name'] for x in a.get('/api/admin/companies?last=never&flags=trial_soon').json()['rows']] == [] and a.get('/api/admin/companies?health=risco').json()['rows'] == []
        assert all(not x['own'] for x in a.get('/api/admin/companies?wa=no').json()['rows'])
        assert a.get('/api/admin/companies?tab=risk&sort=health').status_code == 200 and a.get('/api/admin/companies?sort=value').json()['rows'][0]['name'] == 'Clínica Bella'
        assert bella['kind'] == 'paying' and bella['ai_limit'] == 600 and dom['kind'] == 'trial' and dom['trial_ends_at']
        # ficha
        d = a.get(f'/api/admin/companies/{bella["id"]}').json()
        assert d['phone'] == '11 90000-0000' and d['terms']['version'] == '1' and d['terms']['at'] and d['billing']['amount'] == 297
        assert len(d['payments']) == 1 and d['channels']['whatsapp']['connected'] is False and d['users_limit'] == 5
        assert a.patch(f'/api/admin/companies/{bella["id"]}', json={'cnpj': '12.345.678/0001-90', 'legal_name': 'Bella Ltda', 'contract_url': 'https://docs/x'}).status_code == 200
        assert a.patch(f'/api/admin/companies/{bella["id"]}', json={'contract_url': 'javascript:alert(1)'}).status_code == 400
        n = a.post(f'/api/admin/companies/{bella["id"]}/notes', json={'text': 'Pediu ajuda com a agenda.'}).json()
        assert a.post(f'/api/admin/companies/{bella["id"]}/message', json={'subject': 'Novidade na agenda', 'text': 'Oi!\nJá está no ar.'}).json()['sent']
        assert any(m['subject'] == 'Novidade na agenda' and m['to'] == 're@bella.com' for m in mailer.SENT)
        d = a.get(f'/api/admin/companies/{bella["id"]}').json()
        assert d['cnpj'] == '12.345.678/0001-90' and len(d['notes']) == 2 and any(x['id'] == n['id'] for x in d['notes'])
        # dias grátis e cortesia com prazo
        a.patch(f'/api/admin/companies/{dom["id"]}', json={'extend_trial_days': 7})
        d2 = a.get(f'/api/admin/companies/{dom["id"]}').json(); assert d2['kind'] == 'trial'
        a.patch(f'/api/admin/companies/{dom["id"]}', json={'plan': 'equipe', 'courtesy_days': 30})
        d2 = a.get(f'/api/admin/companies/{dom["id"]}').json()
        assert d2['kind'] == 'courtesy' and d2['plan'] == 'equipe' and d2['courtesy_until']
        assert a.get('/api/admin/overview').json()['mrr'] == 297  # cortesia não entra na receita
        async def vence():
            await db.companies.update_one({'id': dom['id']}, {'$set': {'courtesy.until': now() - timedelta(days=1), 'trial_ends_at': now() - timedelta(days=1)}})
        a.portal.call(vence)
        assert a.get(f'/api/admin/companies/{dom["id"]}').json()['kind'] == 'readonly'
        # avisos automáticos
        nn = a.get('/api/admin/notices').json(); assert len(nn['items']) == 21 and all(nn['notices'].values())
        assert nn['totals_30']['sent'] >= 1 and all('stats' in i and 'optional' in i for i in nn['items'])
        assert a.get('/api/admin/notices?days=7').json()['days'] == 7 and isinstance(a.get('/api/admin/notices/upcoming').json(), list)
        lg = a.get('/api/admin/notices/log?channel=failed&how=auto&days=30').json(); assert all(not (x['email'] or x['wa']) and not x.get('manual') for x in lg)
        assert all(x.get('manual') for x in a.get('/api/admin/notices/log?how=manual').json())
        assert a.put('/api/admin/notices', json={'paid': False}).json()['notices']['paid'] is False

def test_admin_funil_cancelamento_e_conta_de_teste(monkeypatch):
    subs, invs = fake(monkeypatch); mailer.SENT.clear()
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Renata', 'email': 're2@bella.com', 'password': 'senha-forte-1', 'company': 'Clínica Bella', 'segment': 'clinica'})
        sid = assina(c, subs, 'profissional')
        invs['301'] = {'id': 301, 'preapproval_id': sid, 'transaction_amount': 297, 'debit_date': now().isoformat(), 'status': 'processed', 'payment': {'id': 3, 'status': 'approved'}}
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '301'}})
        assert c.post('/api/billing/cancel').status_code == 200
    with mk() as t:
        t.post('/api/auth/signup', json={'name': 'Camila', 'email': 'camila+revisao@x.com', 'password': 'senha-forte-1', 'company': 'Studio Teste', 'segment': 'salao'})
    with mk() as a:
        a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1', 'company': 'Oi Nanda'})
        o = a.get('/api/admin/overview').json()
        f = {x['key']: x['count'] for x in o['funnel']}
        assert o['signups'] == 2 and f['signup'] == 2 and f['card'] == 1 and f['whatsapp'] == 0 and o['funnel'][0]['pct'] == 100
        assert o['churn']['count'] == 1 and o['churn']['mrr_lost'] == 297
        assert set(o['activity']) >= {'conversations', 'msgs_in', 'msgs_out', 'handoffs', 'appointments', 'sales', 'sales_total'} and o['ops']['errors_24h'] == 0
        pf = a.get('/api/admin/platform').json(); assert pf['cnpj'] == '40.864.395/0001-95' and pf['address'] == ''
        assert a.put('/api/admin/platform', json={'address': 'Av. Teste, 100'}).json()['address'] == 'Av. Teste, 100'
        assert a.get('/api/admin/platform').json()['legal_name'] == 'Fagotti Marketing Direto Ltda'
        at = a.get('/api/admin/attention').json(); assert at['counts']['cancelled'] == 1
        assert next(x for x in at['items'] if x['kind'] == 'cancelled')['notice'] is None
        # conta de teste: sai dos números, aparece marcada na lista e não recebe avisos automáticos
        st = next(x for x in a.get('/api/admin/companies').json()['rows'] if x['name'] == 'Studio Teste')
        assert a.patch(f'/api/admin/companies/{st["id"]}', json={'test': True}).status_code == 200
        l = a.get('/api/admin/companies').json()
        assert l['counts']['all'] == 1 and next(x for x in l['rows'] if x['name'] == 'Studio Teste')['test'] is True
        o = a.get('/api/admin/overview').json(); assert o['signups'] == 1 and o['trial'] == 0
        import notices
        async def tenta():
            comp = await db.companies.find_one({'id': st['id']}, {'_id': 0})
            return await notices.deliver(comp, 'trial_3d', once='x')
        assert a.portal.call(tenta) is False
        a.patch(f'/api/admin/companies/{st["id"]}', json={'test': False})
        assert a.get('/api/admin/companies').json()['counts']['all'] == 2

def test_aviso_de_cobranca_chegando(monkeypatch):
    subs, _ = fake(monkeypatch); mailer.SENT.clear()
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Dona B', 'email': 'db@x.com', 'password': 'senha-forte-1', 'company': 'Loja B', 'segment': 'loja'})
        assina(c, subs, nxt=now() + timedelta(days=2, hours=12))
        c.portal.call(mailer.charge_tick); c.portal.call(mailer.charge_tick)
        assert sum(1 for m in mailer.SENT if 'assinatura renova em' in m['subject']) == 1
