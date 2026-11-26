import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from fastapi.testclient import TestClient
import whatsapp, mailer
from core import db, now
from main import app

def mk(): return TestClient(app, base_url='https://testserver')

def test_sete_dias_comecam_ao_conectar_e_um_por_numero(monkeypatch):
    monkeypatch.setenv('SIGNUP_MODE', 'demo')
    async def g(method, path, token, body=None, params=None): return {'display_phone_number': '+55 11 90000-0000', 'verified_name': 'X'}
    monkeypatch.setattr(whatsapp, 'graph', g); mailer.SENT.clear()
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Bia Sol', 'email': 'bia@t7.com', 'password': 'senha-forte-1', 'company': 'Bia Studio', 'segment': 'studio'})
        me = c.get('/api/auth/me').json(); cid = me['company']['id']
        assert me['access']['state'] == 'demo'
        r = c.post('/api/wa/connect', json={'phone_number_id': '7770001', 'waba_id': '7770002', 'token': 'EAA' + 'q' * 30})
        assert r.status_code == 200, r.text
        co = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        left = co['trial_ends_at'].replace(tzinfo=now().tzinfo) - now() if co['trial_ends_at'].tzinfo is None else co['trial_ends_at'] - now()
        assert co['plan'] == 'trial' and co['trial_used'] and timedelta(days=6, hours=23) < left <= timedelta(days=7)
        assert c.get('/api/billing').json()['trial_left'] == 7
        assert 'Pronto! Seus 7 dias grátis começaram' in [m['subject'] for m in mailer.SENT]
        # reconectar não reinicia o teste
        c.post('/api/wa/connect', json={'phone_number_id': '7770001', 'waba_id': '7770002', 'token': 'EAA' + 'q' * 30})
        co2 = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        assert co2['trial_ends_at'] == co['trial_ends_at']
        c.portal.call(lambda: db.wa_numbers.delete_many({'company_id': cid}))
    with mk() as c:  # outra conta com o mesmo número: sem teste novo
        c.post('/api/auth/signup', json={'name': 'Bia Dois', 'email': 'bia2@t7.com', 'password': 'senha-forte-1', 'company': 'Bia Dois', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770001', 'waba_id': '7770002', 'token': 'EAA' + 'q' * 30})
        assert c.get('/api/billing').json()['state'] == 'readonly'

def test_chamada_de_onboarding_no_horario_livre_do_time(monkeypatch):
    monkeypatch.setenv('SIGNUP_MODE', 'demo'); mailer.SENT.clear()
    with mk() as a:
        a.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'admin@oinanda.com.br', 'password': 'senha-forte-1', 'company': 'Oi Nanda', 'segment': 'outro'})
        with mk() as c:
            c.post('/api/auth/signup', json={'name': 'Carla Luz', 'email': 'carla@t7.com', 'password': 'senha-forte-1', 'company': 'Clínica Luz', 'segment': 'clinica', 'phone': '11 98888-7777'})
            o = c.get('/api/onboard').json()
            assert o['slots'] and not o['booked']
            s0 = o['slots'][0]
            b = c.post('/api/onboard', json={'start': s0['start']}).json()
            assert b['booked']['label'] == s0['label']
            assert any(m['subject'].startswith('Chamada marcada') and m['to'] == 'carla@t7.com' for m in mailer.SENT)
            assert s0['start'] not in [x['start'] for x in c.get('/api/onboard').json()['slots']]  # horário ocupado sai da lista
            assert c.post('/api/onboard', json={'start': s0['start']}).status_code == 409
            ap = a.get('/api/appointments', params={'start': s0['start'][:10], 'end': s0['start'][:10]}).json()
            assert any(x['service_id'] == 'onboarding' and 'Clínica Luz' in (x.get('notes') or '') for x in ap)
            c.post('/api/onboard', json={'note': 'só à noite'})
        assert a.get('/api/admin/onboarding').json()[0]['company'] == 'Clínica Luz'
