"""Saúde do número: bloqueio, restrição e nota vermelha viram alerta no Início, com aviso por e-mail; voltou ao normal, some."""
import os, json, hmac, hashlib
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, mailer, wahealth
from core import db
from main import app

def hook(c, field, entry_id, value):
    raw = json.dumps({'entry': [{'id': entry_id, 'changes': [{'field': field, 'value': value}]}]}).encode()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_saude_do_numero(monkeypatch):
    state = {'status': 'CONNECTED', 'quality': 'GREEN', 'can_send': 'AVAILABLE'}
    async def fake_graph(method, path, token, payload=None, params=None):
        if params and 'health_status' in params.get('fields', ''): return {'health_status': {'can_send_message': state['can_send'], 'entities': [{'errors': [{'error_description': 'Conta em análise'}]}] if state['can_send'] != 'AVAILABLE' else []}}
        if params and 'status' in params.get('fields', ''): return {'status': state['status'], 'quality_rating': state['quality']}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765', 'quality_rating': 'GREEN'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    mailer.SENT.clear()
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona S', 'email': 'ds@x.com', 'password': 'senha-forte-1', 'company': 'Studio S', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '7710011', 'waba_id': '7710012', 'token': 'EAA' + 'q' * 30})
        c.portal.call(wahealth.tick)
        assert not c.get('/api/wa/number').json().get('alert')
        # a Meta bloqueia: a conferência percebe, avisa por e-mail uma vez e o Início mostra primeiro
        state['status'] = 'BANNED'
        async def force():
            await db.wa_numbers.update_many({}, {'$unset': {'checkup': ''}})
            await wahealth.tick()
        c.portal.call(force); c.portal.call(force)
        a = c.get('/api/wa/number').json()['alert']
        assert a['kind'] == 'banned' and 'Pedir análise' in a['detail']
        assert len([m for m in mailer.SENT if 'bloqueou' in m['subject']]) == 1
        td = c.get('/api/home/todo').json()
        assert td[0]['k'] == 'wa_alert' and td[0]['tone'] == 'bad'
        # liberou: o alerta some sozinho
        state['status'] = 'CONNECTED'
        c.portal.call(force)
        assert not c.get('/api/wa/number').json().get('alert')
        # aviso que a Meta manda pelo webhook
        hook(c, 'account_update', '7710012', {'event': 'ACCOUNT_RESTRICTION', 'restriction_info': {'restriction_type': 'RESTRICTED_BIZ_INITIATED_MESSAGING'}})
        assert c.get('/api/wa/number').json()['alert']['kind'] == 'restricted'
        hook(c, 'phone_number_quality_update', '7710012', {'display_phone_number': '5511975754765', 'event': 'UNFLAGGED'})
        assert not c.get('/api/wa/number').json().get('alert')
        hook(c, 'phone_number_quality_update', '7710012', {'display_phone_number': '5511975754765', 'event': 'FLAGGED', 'current_limit': 'TIER_250'})
        assert c.get('/api/wa/number').json()['alert']['kind'] == 'flagged'

def test_leitura_da_meta():
    assert wahealth.problem_of('CONNECTED', 'GREEN', 'AVAILABLE') is None
    assert wahealth.problem_of('CONNECTED', 'GREEN', 'BLOCKED', ['Pagamento pendente'])[0] == 'blocked'
    assert wahealth.problem_of('CONNECTED', 'RED')[0] == 'flagged'
    assert wahealth.problem_of('RESTRICTED')[0] == 'restricted'
