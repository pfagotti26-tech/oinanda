import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ['SECRET_KEY'] = Fernet.generate_key().decode()
from fastapi.testclient import TestClient
import whatsapp
from main import app

calls = []
async def fake_graph(method, path, token, payload=None, params=None):
    calls.append((method, path, token, payload))
    if path.endswith('/messages'): return {'messages': [{'id': 'wamid.OUT1'}]}
    if 'subscribed_apps' in path: return {'success': True}
    return {'display_phone_number': '+55 11 90000-0000', 'verified_name': 'Studio Teste', 'quality_rating': 'GREEN'}

def signed(c, body):
    raw = json.dumps(body).encode()
    sig = 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': sig, 'content-type': 'application/json'})

def inbound(text, mid, ts=None):
    return {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '1112223334'},
        'contacts': [{'wa_id': '5511988887777', 'profile': {'name': 'Mariana'}}],
        'messages': [{'from': '5511988887777', 'id': mid, 'timestamp': str(ts or int(time.time())), 'type': 'text', 'text': {'body': text}}]}}]}]}

def test_whatsapp_fluxo(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        assert c.get('/api/wa/webhook', params={'hub.mode': 'subscribe', 'hub.verify_token': 'verif', 'hub.challenge': '42'}).text == '42'
        assert c.get('/api/wa/webhook', params={'hub.mode': 'subscribe', 'hub.verify_token': 'x', 'hub.challenge': '42'}).status_code == 403
        c.post('/api/auth/signup', json={'name': 'Ana Dona', 'email': 'ana@st.com', 'password': 'senha-forte-1', 'company': 'Studio Teste'})
        assert c.get('/api/wa/number').json() is None
        r = c.post('/api/wa/connect', json={'phone_number_id': '1112223334', 'waba_id': '9998887776', 'token': 'EAAtoken' + 'x' * 30})
        assert r.status_code == 200, r.text
        assert r.json()['display_phone'] == '+55 11 90000-0000' and r.json()['webhook'] == 'ok'
        assert any('subscribed_apps' in x[1] for x in calls)
        # assinatura errada
        assert c.post('/api/wa/webhook', content=b'{}', headers={'x-hub-signature-256': 'sha256=00'}).status_code == 403
        assert signed(c, inbound('Oi, quanto custa?', 'wamid.IN1')).status_code == 200
        assert signed(c, inbound('Oi, quanto custa?', 'wamid.IN1')).status_code == 200  # duplicado ignorado
        conv = c.get('/api/conversations').json()
        assert len(conv) == 1 and conv[0]['name'] == 'Mariana' and conv[0]['unread'] == 1 and conv[0]['window_open']
        cid = conv[0]['id']
        d = c.get(f'/api/conversations/{cid}').json()
        assert len(d['messages']) == 1 and d['messages'][0]['text'] == 'Oi, quanto custa?'
        s = c.post(f'/api/conversations/{cid}/send', json={'text': 'Oi Mariana! O trimestral é R$ 790.'})
        assert s.status_code == 200, s.text
        assert calls[-1][2].startswith('EAAtoken') and calls[-1][3]['to'] == '5511988887777'
        # status entregue
        signed(c, {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '1112223334'}, 'statuses': [{'id': 'wamid.OUT1', 'status': 'read'}]}}]}]})
        d = c.get(f'/api/conversations/{cid}').json()
        assert d['messages'][-1]['status'] == 'read' and d['contact']['unread'] == 0 and d['contact']['assigned_to']
        # janela fechada
        signed(c, inbound('msg velha', 'wamid.OLD', ts=int(time.time()) - 90000))
        assert c.get('/api/conversations').json()[0]['window_open']  # mensagem antiga não fecha a janela
    with TestClient(app, base_url='https://testserver') as c2:
        c2.post('/api/auth/signup', json={'name': 'Bia Dona', 'email': 'bia@st.com', 'password': 'senha-forte-1', 'company': 'Outra'})
        assert c2.get('/api/conversations').json() == []  # isolamento entre empresas
        assert c2.post('/api/wa/connect', json={'phone_number_id': '1112223334', 'waba_id': '9998887776', 'token': 'EAA' + 'y' * 30}).status_code == 409
