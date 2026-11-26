import os
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import mailer, notices
from core import db, now
from main import app
from tests.test_admin_financeiro import fake, assina

def mk(): return TestClient(app, base_url='https://testserver')
def subjects(to): return [m['subject'] for m in mailer.SENT if m['to'] == to]
def signup(c, email, company, phone='11 91234-5678'):
    c.post('/api/auth/signup', json={'name': 'Paula Reis', 'email': email, 'password': 'senha-forte-1', 'company': company, 'segment': 'studio', 'phone': phone})
    return c.get('/api/auth/me').json()['company']['id']

def test_avisos_do_teste_e_whatsapp_sem_conectar():
    mailer.SENT.clear()
    with mk() as c:
        cid = signup(c, 'p1@av.com', 'Studio Um')
        assert subjects('p1@av.com') == ['Paula, a Nanda da Studio Um já está pronta 🎉']
        html = mailer.SENT[-1]['html']
        assert '7 dias' in html and 'Ligar a Nanda no seu WhatsApp' in html and '{' not in html.split('<body')[1]
        # 1 dia depois, sem WhatsApp: lembra uma vez
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'created_at': now() - timedelta(days=1, hours=2)}}))
        c.portal.call(notices.tick); c.portal.call(notices.tick)
        assert subjects('p1@av.com').count('Sua Nanda está pronta. Ligamos com você?') == 1
        # teste acaba amanhã (o limite de um aviso por dia segura o segundo no mesmo dia)
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'trial_ends_at': now() + timedelta(hours=20)}}))
        c.portal.call(notices.tick)
        assert 'Amanhã termina o seu teste grátis' not in subjects('p1@av.com')
        c.portal.call(lambda: db.notice_log.update_many({}, {'$set': {'at': now() - timedelta(days=1)}}))
        c.portal.call(notices.tick); c.portal.call(notices.tick)
        assert subjects('p1@av.com').count('Amanhã termina o seu teste grátis') == 1
        # fora do horário, aviso que não é urgente espera
        c.portal.call(lambda: db.notice_log.delete_many({}))
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'trial_ends_at': now() + timedelta(days=1, hours=12)}}))
        import pytest
        mp = pytest.MonkeyPatch(); mp.setattr(notices, 'quiet', lambda: True)
        c.portal.call(notices.tick); mp.undo()
        assert 'Faltam 2 dias do seu teste grátis' not in subjects('p1@av.com')
        c.portal.call(notices.tick)
        assert 'Faltam 2 dias do seu teste grátis' in subjects('p1@av.com')

def test_recusa_ultimo_aviso_pausa_e_cancelamento(monkeypatch):
    subs, invs = fake(monkeypatch); mailer.SENT.clear()
    with mk() as c:
        cid = signup(c, 'p2@av.com', 'Studio Dois')
        sid = assina(c, subs, 'profissional')
        invs['301'] = {'id': 301, 'preapproval_id': sid, 'transaction_amount': 297, 'debit_date': now().isoformat(), 'status': 'recycling', 'payment': {'id': 9, 'status': 'rejected', 'status_detail': 'cc_rejected_insufficient_amount'}}
        c.post('/api/billing/webhook', json={'type': 'subscription_authorized_payment', 'data': {'id': '301'}})
        assert 'A cobrança de R$ 297,00 não passou' in subjects('p2@av.com')
        with mk() as a:
            a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'senha-forte-1', 'company': 'Oi Nanda', 'segment': 'outro'})
            at = a.get('/api/admin/attention').json()
            it = next(x for x in at['items'] if x['company'] == 'Studio Dois')
            assert it['kind'] == 'refused' and 'sem limite' in it['detail'] and it['owner'] == 'Paula Reis' and it['noticed_by'] == 'e-mail'
            assert at['counts']['refused'] == 1
            r = a.post(f'/api/admin/companies/{cid}/notify', json={'key': 'refused', 'channel': 'email'}).json()
            assert r == {'email': True, 'wa': False}
            assert subjects('p2@av.com').count('A cobrança de R$ 297,00 não passou') == 2
        # 6 dias depois: último aviso; depois do prazo: conta pausada
        c.portal.call(lambda: db.payments.update_many({}, {'$set': {'due': now() - timedelta(days=6, hours=2)}}))
        c.portal.call(notices.tick); c.portal.call(notices.tick)
        assert subjects('p2@av.com').count('Amanhã é o último dia para acertar a assinatura') == 1
        async def passa():
            await db.payments.update_many({}, {'$set': {'due': now() - timedelta(days=8)}})
            await db.companies.update_one({'id': cid}, {'$set': {'billing.paid_until': now() - timedelta(hours=1)}})
        c.portal.call(passa); c.portal.call(notices.tick); c.portal.call(notices.tick)
        assert subjects('p2@av.com').count('Sua conta está em pausa, e nada foi apagado') == 1
    with mk() as c:
        signup(c, 'p3@av.com', 'Studio Três'); assina(c, subs)
        assert c.post('/api/billing/cancel').status_code == 200
        assert 'Cancelamento confirmado' in subjects('p3@av.com')

def test_uso_da_nanda_textos_e_whatsapp(monkeypatch):
    mailer.SENT.clear(); notices.WA_SENT.clear()
    monkeypatch.setenv('PLATFORM_WA_PHONE_ID', '999'); monkeypatch.setenv('PLATFORM_WA_TOKEN', 'tok')
    with mk() as c, mk() as a:
        cid = signup(c, 'p4@av.com', 'Studio Quatro')
        r = a.post('/api/auth/login', json={'email': 'admin@oinanda.com.br', 'password': 'senha-forte-1'})
        if r.status_code != 200: a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'senha-forte-1', 'company': 'Oi Nanda', 'segment': 'outro'})
        v = a.get('/api/admin/notices/refused'); assert v.status_code == 200, v.text
        # 80% e 100% dos atendimentos do teste sem cartão (200 nos 14 dias)
        c.portal.call(lambda: notices.ai_mark(cid, 159)); assert not [s for s in subjects('p4@av.com') if 'A Nanda' in s]
        c.portal.call(lambda: notices.ai_mark(cid, 160)); c.portal.call(lambda: notices.ai_mark(cid, 160))
        assert subjects('p4@av.com').count('A Nanda já atendeu 160 clientes este mês') == 1
        c.portal.call(lambda: notices.ai_mark(cid, 200))
        assert 'A Nanda usou os 200 atendimentos do mês' in subjects('p4@av.com')
        assert notices.WA_SENT == []  # sem modelo aprovado, o WhatsApp não sai
        # admin edita o texto; campo desconhecido é recusado
        v = a.get('/api/admin/notices/refused').json()
        assert v['whatsapp'] and 'R$ 297,00' in v['preview']['subject'] and '<html' in v['preview']['html']
        assert a.put('/api/admin/notices/refused', json={'subject': 'Oi {nome_errado}'}).status_code == 400
        v = a.put('/api/admin/notices/refused', json={'subject': '{empresa}: a cobrança de {valor} não passou', 'wa': '{primeiro_nome}, a cobrança de *{valor}* não passou. Prazo: *{prazo}*.'}).json()
        assert v['custom'] and v['preview']['subject'] == 'Studio Vita: a cobrança de R$ 297,00 não passou' and v['wa_status'] in ('DRAFT', 'REJECTED', 'PENDING')
        # modelo aprovado pela Meta: o aviso manual sai pelos dois canais
        c.portal.call(lambda: db.notice_templates.update_many({'key': 'refused'}, {'$set': {'status': 'APPROVED'}}))
        r = a.post(f'/api/admin/companies/{cid}/notify', json={'key': 'refused', 'channel': 'both'}).json()
        assert r == {'email': True, 'wa': True}
        assert notices.WA_SENT[-1]['to'] == '5511912345678' and notices.WA_SENT[-1]['vars']['primeiro_nome'] == 'Paula'
        assert subjects('p4@av.com')[-1] == 'Studio Quatro: a cobrança de R$ 0,00 não passou'
        log = a.get('/api/admin/notices/log').json()
        assert log[0]['key'] == 'refused' and log[0]['wa'] and log[0]['manual']
        assert a.post('/api/admin/notices/refused/test').json()['to'] == 'admin@oinanda.com.br'
        assert a.delete('/api/admin/notices/refused').json()['custom'] is False
        assert a.put('/api/admin/notices', json={'paid': False}).json()['notices']['paid'] is False

def test_cliente_escolhe_os_avisos(monkeypatch):
    subs, _ = fake(monkeypatch); mailer.SENT.clear()
    with mk() as c:
        cid = signup(c, 'p5@av.com', 'Studio Cinco')
        p = c.get('/api/company/notices').json()
        assert p['whatsapp'] and len(p['optional']) == 11 and 'Cobrança recusada' in p['always']
        assert c.put('/api/company/notices', json={'phone': '123'}).status_code == 400
        p = c.put('/api/company/notices', json={'whatsapp': False, 'off': ['trial_3d', 'refused'], 'phone': '11 98888-7777'}).json()
        assert not p['whatsapp'] and [o['key'] for o in p['optional'] if not o['on']] == ['trial_3d']  # recusada não dá para desligar
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'trial_ends_at': now() + timedelta(days=2, hours=12)}}))
        c.portal.call(notices.tick)
        assert 'Faltam 2 dias do seu teste grátis' not in subjects('p5@av.com')

def test_avisos_saem_pelo_numero_da_conta_do_oi_nanda():
    """Sem variáveis no servidor: vale o WhatsApp conectado na conta do admin."""
    import whatsapp
    with mk() as a:
        r = a.post('/api/auth/login', json={'email': 'admin@oinanda.com.br', 'password': 'senha-forte-1'})
        if r.status_code != 200: a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'senha-forte-1', 'company': 'Oi Nanda', 'segment': 'outro'})
        cid = a.get('/api/auth/me').json()['company']['id']
        assert a.get('/api/admin/notices').json()['wa_ready'] is False
        a.portal.call(lambda: db.wa_numbers.insert_one({'company_id': cid, 'phone_number_id': '555', 'waba_id': '777', 'status': 'connected', 'token_enc': whatsapp.encrypt('tok'), 'display_phone': '+55 11 97575-4765'}))
        assert a.get('/api/admin/notices').json()['wa_ready'] is True
        p = a.portal.call(notices.platform)
        assert p['phone'] == '555' and p['waba'] == '777' and p['token'] == 'tok'
        a.portal.call(lambda: db.wa_numbers.delete_many({'company_id': cid}))

def test_dicas_do_comeco_e_resumo_da_primeira_semana():
    mailer.SENT.clear()
    with mk() as c:
        cid = signup(c, 'p9@av.com', 'Clínica Nove')
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'segment': 'clinica', 'created_at': now() - timedelta(days=2, hours=3)}}))
        c.portal.call(notices.tick)
        m = next(x for x in mailer.SENT if x['subject'] == '10 minutos que fazem a Nanda responder muito melhor')
        assert 'clínicas' in m['html'] and '/ajuda/ensinar-nanda' in m['html'] and '{' not in m['html'].split('<body')[1]
        # dia 4 sem automação ligada: as 3 do segmento, com o motivo de cada uma
        c.portal.call(lambda: db.notice_log.update_many({}, {'$set': {'at': now() - timedelta(days=1)}}))
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'created_at': now() - timedelta(days=4, hours=3)}}))
        c.portal.call(notices.tick); c.portal.call(notices.tick)
        ms = [x for x in mailer.SENT if x['subject'] == 'As 3 automações que mais ajudam clínicas']
        assert len(ms) == 1 and 'Orçamento sem resposta' in ms[0]['html'] and 'Faltou: oferecer novo horário' in ms[0]['html']
        # uma semana de WhatsApp com conversa: resumo com os números
        c.portal.call(lambda: db.notice_log.update_many({}, {'$set': {'at': now() - timedelta(days=1)}}))
        c.portal.call(lambda: db.wa_numbers.insert_one({'company_id': cid, 'phone_number_id': '555001', 'status': 'connected', 'connected_at': now() - timedelta(days=7, hours=2)}))
        c.portal.call(lambda: db.messages.insert_many([{'id': 'm1', 'company_id': cid, 'contact_id': 'k1', 'dir': 'in', 'text': 'oi', 'at': now() - timedelta(days=3)},
                                                      {'id': 'm2', 'company_id': cid, 'contact_id': 'k1', 'dir': 'out', 'by': 'Nanda (IA)', 'text': 'oi!', 'at': now() - timedelta(days=3)}]))
        c.portal.call(notices.tick)
        w = next(x for x in mailer.SENT if x['subject'] == 'Sua primeira semana com a Nanda')
        assert '/ajuda/seg-clinica' in w['html'] and 'conversas nos primeiros 7 dias' in w['html']

def test_cliente_confirma_o_horario_respondendo_o_lembrete():
    import agenda
    from core import uid
    async def cen():
        await db.appointments.insert_one({'id': 'ap1', 'company_id': 'co', 'contact_id': 'k1', 'status': 'scheduled', 'service_name': 'Avaliação',
                                          'start': now() + timedelta(hours=20), 'created_at': now() - timedelta(days=2)})
        assert await agenda.confirm_by_reply('co', 'k1', 'sim') is None  # sem lembrete enviado, um "sim" não confirma nada
        await db.automation_runs.insert_one({'key': 'a1:ap1', 'company_id': 'co', 'contact_id': 'k1', 'status': 'sent', 'at': now() - timedelta(hours=1)})
        assert await agenda.confirm_by_reply('co', 'k1', 'Sim, mas dá para mudar o horário?') is None
        assert await agenda.confirm_by_reply('co', 'k1', 'Confirmo 👍') == 'ap1'
        ap = await db.appointments.find_one({'id': 'ap1'})
        assert ap['status'] == 'confirmed' and ap['confirmed_by'] == 'cliente'
    import asyncio; asyncio.run(cen())
