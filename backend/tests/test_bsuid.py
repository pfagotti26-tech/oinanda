"""Nome de usuário do WhatsApp (BSUID): cliente sem telefone no webhook continua sendo atendido, sem ficha duplicada."""
import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from main import app

sent = []
async def fake_graph(method, path, token, payload=None, params=None):
    if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'wamid.O%d' % time.time_ns()}]}
    if 'subscribed_apps' in path: return {'success': True}
    return {'display_phone_number': '+55 11 90000-1234', 'verified_name': 'Loja U', 'quality_rating': 'GREEN'}

def signed(c, value):
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770001111'}, **value}}]}]}).encode()
    sig = 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': sig, 'content-type': 'application/json'})

def txt(mid, body='oi', **who):
    return {'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': body}, **who}

def test_bsuid(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona U', 'email': 'bu@x.com', 'password': 'senha-forte-1', 'company': 'Loja U'})
        assert c.post('/api/wa/connect', json={'phone_number_id': '7770001111', 'waba_id': '8880001', 'token': 'EAA' + 'u' * 30}).status_code == 200
        # 1) cliente com telefone: a ficha guarda também o BSUID
        signed(c, {'contacts': [{'profile': {'name': 'Rita'}, 'wa_id': '5511933330001', 'user_id': 'BR.111'}], 'messages': [txt('w1') | {'from': '5511933330001', 'from_user_id': 'BR.111'}]})
        # 2) a mesma pessoa adota nome de usuário: vem só o BSUID e cai na mesma ficha
        signed(c, {'contacts': [{'profile': {'name': 'Rita', 'username': 'rita.doces'}, 'user_id': 'BR.111'}], 'messages': [txt('w2', from_user_id='BR.111')]})
        # 3) pessoa nova que já chega sem telefone
        signed(c, {'contacts': [{'profile': {'name': 'Caio', 'username': 'caio'}, 'user_id': 'BR.222'}], 'messages': [txt('w3', 'quanto custa?', from_user_id='BR.222')]})
        rows = c.get('/api/conversations').json()
        assert len(rows) == 2
        rita = next(r for r in rows if r['name'] == 'Rita'); caio = next(r for r in rows if r['name'] == 'Caio')
        assert rita['wa_id'] == '5511933330001' and rita['wa_uid'] == 'BR.111' and rita['wa_username'] == 'rita.doces'
        assert not caio.get('wa_id') and caio['wa_uid'] == 'BR.222' and caio['window_open'] and not caio['old_line']
        # responder: com telefone vai em "to"; sem telefone vai pelo BSUID em "recipient"
        assert c.post(f"/api/conversations/{rita['id']}/send", json={'text': 'oi Rita'}).status_code == 200
        assert c.post(f"/api/conversations/{caio['id']}/send", json={'text': 'R$ 50'}).status_code == 200
        assert sent[-2]['to'] == '5511933330001' and 'recipient' not in sent[-2]
        assert sent[-1]['recipient'] == 'BR.222' and 'to' not in sent[-1]
        # 4) o BSUID mudou: a ficha acompanha
        signed(c, {'messages': [{'id': 's1', 'timestamp': str(int(time.time())), 'type': 'system', 'system': {'type': 'user_changed_user_id', 'user_id': 'BR.333', 'previous_user_id': 'BR.222'}}]})
        signed(c, {'contacts': [{'profile': {'name': 'Caio'}, 'user_id': 'BR.333'}], 'messages': [txt('w4', from_user_id='BR.333')]})
        rows = c.get('/api/conversations').json()
        assert len(rows) == 2 and next(r for r in rows if r['name'] == 'Caio')['wa_uid'] == 'BR.333'
