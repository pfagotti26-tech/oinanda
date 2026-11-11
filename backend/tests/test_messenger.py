"""Messenger: mesma página do Facebook do Instagram, canal que a empresa liga quando quiser."""
import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  IG_APP_ID='111', IG_HUMAN_AGENT='1', IG_APP_SECRET='igsecreto', IG_CONFIG_ID='777', IG_VERIFY_TOKEN='igverif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import instagram, whatsapp
from core import db, now, uid
from main import app

PAGE, IGID, PSID = '900100', '800100', '700100'

def hook(c, obj, entry_id, sender, text, echo=False):
    m = {'sender': {'id': sender}, 'recipient': {'id': entry_id}, 'timestamp': str(int(time.time() * 1000)),
         'message': {'mid': 'm' + str(time.time_ns()), 'text': text, **({'is_echo': True} if echo else {})}}
    if echo: m['sender'], m['recipient'] = {'id': entry_id}, {'id': sender}
    raw = json.dumps({'object': obj, 'entry': [{'id': entry_id, 'messaging': [m]}]}).encode()
    return c.post('/api/ig/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'igsecreto', raw, hashlib.sha256).hexdigest()})

async def connect_page(company_id):
    from whatsapp import encrypt
    await db.ig_accounts.update_one({'company_id': company_id}, {'$set': {
        'company_id': company_id, 'ig_id': IGID, 'username': 'clinica', 'name': 'Clínica', 'page_id': PAGE, 'page_name': 'Clínica Lumi',
        'mode': 'page', 'token_enc': encrypt('EAA' + 'p' * 30), 'status': 'connected', 'connected_at': now()}}, upsert=True)

def test_messenger(monkeypatch):
    sent = []
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        if method == 'GET': return {'name': 'João Pedro', 'profile_pic': 'http://x/p.jpg'}
        sent.append((path, payload)); return {'message_id': 'out1'}
    monkeypatch.setattr(instagram, 'ig', fake_ig)
    monkeypatch.setattr(whatsapp, 'ON_INBOUND', None); monkeypatch.setattr(whatsapp, 'ON_HUMAN_SEND', None)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Renata', 'email': 'r@lumi.com', 'password': 'senha-forte-1', 'company': 'Lumi', 'segment': 'clinica'})
        cid = c.get('/api/auth/me').json()['company']['id']
        c.portal.call(connect_page, cid)

        # canal desligado: a mensagem do Messenger é ignorada, nada de ficha nova
        assert hook(c, 'page', PAGE, PSID, 'Oi, vocês atendem sábado?').status_code == 200
        assert c.get('/api/conversations').json() == []

        # a empresa liga o canal em Empresa › Mais canais
        assert c.put('/api/company/channels', json={'messenger': True}).json()['channels'] == {'messenger': True}
        assert hook(c, 'page', PAGE, PSID, 'Oi, vocês atendem sábado?').status_code == 200
        cv = c.get('/api/conversations').json()
        assert len(cv) == 1 and cv[0]['name'] == 'João Pedro' and cv[0]['channel'] == 'messenger'

        # o time responde: sai pelo /me/messages com o PSID
        assert c.post(f'/api/conversations/{cv[0]["id"]}/send', json={'text': 'Atendemos sim!'}).status_code == 200
        path, payload = sent[-1]
        assert path == '/me/messages' and payload['recipient'] == {'id': PSID} and payload['message'] == {'text': 'Atendemos sim!'}

        # o Instagram da mesma página continua caindo na ficha de Instagram, separado
        assert hook(c, 'instagram', IGID, '600100', 'Oi pelo Direct').status_code == 200
        cv = {x['channel']: x for x in c.get('/api/conversations').json()}
        assert set(cv) == {'messenger', 'instagram'} and cv['instagram']['id'] != cv['messenger']['id']

        # desligar esconde o canal para as mensagens novas, sem apagar nada
        c.put('/api/company/channels', json={'messenger': False})
        assert hook(c, 'page', PAGE, '700999', 'Mais um').status_code == 200
        assert len(c.get('/api/conversations').json()) == 2
