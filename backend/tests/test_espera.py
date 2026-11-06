import os, json, hmac, hashlib, time
from datetime import datetime
from zoneinfo import ZoneInfo
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, inbox
from main import app

def test_horario():
    tz = ZoneInfo('America/Sao_Paulo'); h = {'0': [['09:00', '18:00']], '1': [['09:00', '18:00']], '5': [], '6': []}
    sex_noite = datetime(2026, 11, 6, 22, 0, tzinfo=tz)  # sexta 22h -> próxima abertura segunda 9h
    assert inbox.wait_from(sex_noite, h, tz) == datetime(2026, 11, 9, 9, 0, tzinfo=tz)
    seg_10 = datetime(2026, 11, 9, 10, 0, tzinfo=tz)
    assert inbox.wait_from(seg_10, h, tz) == seg_10
    assert inbox.wait_from(datetime(2026, 11, 9, 7, 0, tzinfo=tz), h, tz) == datetime(2026, 11, 9, 9, 0, tzinfo=tz)
    assert inbox.wait_from(seg_10, None, tz) == seg_10

def test_espera(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona E', 'email': 'de@x.com', 'password': 'senha-forte-1', 'company': 'Loja E', 'segment': 'loja'})
        c.post('/api/wa/connect', json={'phone_number_id': '5550011', 'waba_id': '5550012', 'token': 'EAA' + 'q' * 30})
        m = {'from': '5511960000001', 'id': 'w1', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'ok obrigado'}}
        raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '5550011'}, 'contacts': [{'wa_id': '5511960000001', 'profile': {'name': 'Cli E'}}], 'messages': [m]}}]}]}).encode()
        c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})
        r = c.get('/api/conversations').json()[0]
        assert r['waiting'] and r['wait_from'] and r['wait_limit'] == 15
        assert c.get('/api/inbox/counts').json()['waiting'] == 1
        c.post(f"/api/conversations/{r['id']}/no-reply")
        r = c.get('/api/conversations').json()[0]
        assert not r['waiting'] and r['wait_from'] is None and c.get('/api/inbox/counts').json()['waiting'] == 0
        assert c.get(f"/api/conversations/{r['id']}").json()['messages'][-1]['text'].endswith('marcou que não precisa responder')
