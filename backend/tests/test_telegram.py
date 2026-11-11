"""Telegram: bot da própria empresa, canal que ela liga quando quiser, sem janela de 24 h."""
import os, json
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import telegram, whatsapp
from main import app

TOKEN = '8012345678:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

def test_telegram(monkeypatch):
    calls = []
    async def fake_tg(token, method, payload=None):
        calls.append((method, payload))
        if method == 'getMe': return {'id': 8012345678, 'is_bot': True, 'username': 'clinicalumi_bot', 'first_name': 'Clínica Lumi'}
        if method == 'sendMessage': return {'message_id': 55}
        return True
    monkeypatch.setattr(telegram, 'tg', fake_tg)
    monkeypatch.setattr(whatsapp, 'ON_INBOUND', None); monkeypatch.setattr(whatsapp, 'ON_HUMAN_SEND', None)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Renata', 'email': 'r@lumi.com', 'password': 'senha-forte-1', 'company': 'Lumi', 'segment': 'clinica'})
        r = c.post('/api/tg/connect', json={'token': TOKEN})
        assert r.status_code == 200 and r.json()['account']['username'] == 'clinicalumi_bot' and r.json()['link'] == 'https://t.me/clinicalumi_bot'
        hook = next(p for m, p in calls if m == 'setWebhook')
        assert hook['url'].endswith('/api/tg/webhook') and hook['allowed_updates'] == ['message'] and len(hook['secret_token']) > 20
        sec = hook['secret_token']

        upd = {'message': {'message_id': 10, 'chat': {'id': 4455, 'type': 'private'}, 'from': {'id': 4455, 'first_name': 'Caio', 'last_name': 'Prado'}, 'text': 'Oi! Fazem peeling?'}}
        # canal desligado: nem cria ficha
        assert c.post('/api/tg/webhook', json=upd, headers={'x-telegram-bot-api-secret-token': sec}).status_code == 200
        assert c.get('/api/conversations').json() == []
        # segredo errado é recusado
        assert c.post('/api/tg/webhook', json=upd, headers={'x-telegram-bot-api-secret-token': 'nao-sou-eu'}).status_code == 403

        c.put('/api/company/channels', json={'telegram': True})
        assert c.post('/api/tg/webhook', json=upd, headers={'x-telegram-bot-api-secret-token': sec}).status_code == 200
        c.post('/api/tg/webhook', json=upd, headers={'x-telegram-bot-api-secret-token': sec})  # a mesma mensagem não entra duas vezes
        cv = c.get('/api/conversations').json()
        assert len(cv) == 1 and cv[0]['name'] == 'Caio Prado' and cv[0]['channel'] == 'telegram' and cv[0]['window_open']

        det = c.get(f'/api/conversations/{cv[0]["id"]}').json()
        ct = det['contact']
        assert len(det['messages']) == 1 and ct['window_open'] and ct['window_ends_at'] is None  # sem janela no Telegram
        assert ct['channels'] == ['telegram'] and ct['reply_channel'] == 'telegram'
        assert c.post(f'/api/conversations/{cv[0]["id"]}/send', json={'text': 'Fazemos sim!'}).status_code == 200
        assert ('sendMessage', {'chat_id': '4455', 'text': 'Fazemos sim!'}) in calls

        assert c.delete('/api/tg/account').status_code == 200 and c.get('/api/tg/status').json()['account'] is None
