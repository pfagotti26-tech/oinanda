"""Avisos para o admin: lead novo, ativação, assinatura, pagamento recusado, contatos do Oi Nanda, liga/desliga e jornada."""
import os
from datetime import timedelta, datetime
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from zoneinfo import ZoneInfo
import mailer, owneralerts
from core import db, now, uid
from main import app

def mk(): return TestClient(app, base_url='https://testserver')
def sent(prefix): return [m for m in mailer.SENT if m['subject'].startswith(prefix) and m['to'] == 'admin@oinanda.com.br']

def test_avisos_para_o_admin(monkeypatch):
    with mk() as a, mk() as c:
        r = a.post('/api/auth/signup', json={'name': 'Paulo', 'company': 'Oi Nanda', 'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1'})
        if r.status_code != 200: a.post('/api/auth/login', json={'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1'})
        own = a.get('/api/auth/me').json()['company']['id']
        monkeypatch.setattr(owneralerts, 'digests', lambda rows: _noop())
        a.portal.call(owneralerts.tick)  # primeira volta: só guarda a foto
        assert a.get('/api/admin/alerts').json()['emails'] == ['admin@oinanda.com.br']
        c.post('/api/auth/signup', json={'name': 'Lívia', 'company': 'Clínica Lívia', 'email': 'livia-oa@x.com', 'password': 'senha-forte-1', 'segment': 'clinica', 'phone': '11987654321'})
        cid = c.get('/api/auth/me').json()['company']['id']
        a.portal.call(owneralerts.tick)
        m = sent('Nova conta: Clínica Lívia'); assert len(m) == 1 and 'wa.me/5511987654321' in m[0]['html']
        a.portal.call(owneralerts.tick); assert len(sent('Nova conta: Clínica Lívia')) == 1  # não repete
        # conectou o WhatsApp
        a.portal.call(lambda: db.wa_numbers.insert_one({'company_id': cid, 'phone_number_id': uid(), 'display_phone': '+55 11 98765-4321', 'status': 'connected'}))
        a.portal.call(owneralerts.tick); assert len(sent('Clínica Lívia conectou o WhatsApp')) == 1
        # desligado não chega
        a.put('/api/admin/alerts', json={'off': ['assinou']})
        a.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'billing': {'sub_id': 's1', 'status': 'active', 'plan': 'profissional', 'amount': 297, 'cycle': 'monthly',
                                                                                         'paid_until': now() + timedelta(days=30), 'since': now()}}}))
        a.portal.call(owneralerts.tick); assert not sent('Novo cliente: Clínica Lívia')
        # pagamento recusado
        a.portal.call(lambda: db.payments.insert_one({'id': 'p1', 'company_id': cid, 'sub_id': 's1', 'status': 'refused', 'amount': 297, 'due': now(), 'detail': 'cc_rejected_insufficient_amount'}))
        a.portal.call(owneralerts.tick)
        m = sent('Pagamento recusado: Clínica Lívia'); assert len(m) == 1 and 'sem limite' in m[0]['html']
        # contatos no WhatsApp do Oi Nanda: juntos num e-mail só
        a.portal.call(lambda: db.contacts.insert_many([{'id': uid(), 'company_id': own, 'name': f'Lead {i}', 'wa_id': f'551190000000{i}', 'created_at': now(), 'last_text': 'quanto custa?'} for i in range(3)]))
        a.portal.call(owneralerts.tick); assert len(sent('3 novos contatos no Oi Nanda')) == 1
        # jornada
        j = a.get('/api/admin/journey').json(); st = {s['key']: s for s in j['stages']}
        assert any(x['name'] == 'Clínica Lívia' for x in st['risco']['items'])  # pagante com cobrança recusada
        assert sum(1 for x in st['lead']['items'] if x.get('contact')) >= 3
        assert c.get('/api/admin/alerts').status_code == 403  # só o admin
        a.post('/api/admin/alerts/test'); assert sent('Teste: assim chega')
        assert a.put('/api/admin/alerts', json={'emails': ['nao-e-email']}).status_code == 400

async def _noop(): return None

def test_resumo_do_dia_e_da_semana(monkeypatch):
    seg = datetime(2026, 11, 30, 9, 0, tzinfo=ZoneInfo('America/Sao_Paulo'))  # segunda, 9h
    monkeypatch.setattr(owneralerts, 'now', lambda: seg)
    with mk() as a:
        r = a.post('/api/auth/signup', json={'name': 'Paulo', 'company': 'Oi Nanda', 'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1'})
        if r.status_code != 200: a.post('/api/auth/login', json={'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1'})
        from adminapi import base
        rows = a.portal.call(base)
        a.portal.call(lambda: owneralerts.digests(rows))
        assert len(sent('Oi Nanda hoje:')) >= 1 and len(sent('Semana do Oi Nanda:')) >= 1
        n0 = len(sent('Oi Nanda hoje:')); a.portal.call(lambda: owneralerts.digests(rows)); assert len(sent('Oi Nanda hoje:')) == n0  # um por dia
