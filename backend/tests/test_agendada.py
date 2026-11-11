"""Mensagem agendada: escreve agora, sai na data e hora combinadas."""
import os, json, hmac, hashlib, time
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, later
from core import db, now
from main import app

def hook(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770011'},
         'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_agendada(monkeypatch):
    sent = []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(whatsapp, 'ON_INBOUND', None)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Renata', 'email': 'r@lumi.com', 'password': 'senha-forte-1', 'company': 'Lumi', 'segment': 'clinica'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770011', 'waba_id': '7770012', 'token': 'EAA' + 'q' * 30})
        hook(c, '5511980000001', 'Bruno', 'Me chama na segunda?')
        cid = c.get('/api/conversations').json()[0]['id']

        assert c.post(f'/api/conversations/{cid}/later', json={'text': 'Oi!', 'at': '2020-01-01T08:00:00'}).status_code == 400  # passado
        r = c.post(f'/api/conversations/{cid}/later', json={'text': 'Bom dia, Bruno! Já estou disponível.', 'at': (now() + timedelta(minutes=30)).isoformat()})
        assert r.status_code == 200 and r.json()['status'] == 'queued' and 'às' in r.json()['local']
        sid = r.json()['id']
        assert [x['id'] for x in c.get(f'/api/conversations/{cid}/later').json()] == [sid]

        c.portal.call(later.tick)  # ainda não é hora
        assert sent == [] and c.get(f'/api/conversations/{cid}/later').json()[0]['status'] == 'queued'

        c.portal.call(db.scheduled.update_one, {'id': sid}, {'$set': {'at': now() - timedelta(minutes=1)}})
        c.portal.call(later.tick)
        assert len(sent) == 1 and sent[0]['text']['body'] == 'Bom dia, Bruno! Já estou disponível.'
        assert c.get(f'/api/conversations/{cid}/later').json() == []  # saiu da fila

        # fora da janela de 24 h o WhatsApp recusa: vira nota na conversa, não some em silêncio
        r2 = c.post(f'/api/conversations/{cid}/later', json={'text': 'Oi de novo', 'at': (now() + timedelta(days=2)).isoformat()})
        sid2 = r2.json()['id']
        c.portal.call(db.scheduled.update_one, {'id': sid2}, {'$set': {'at': now() - timedelta(minutes=1)}})
        c.portal.call(db.contacts.update_one, {'id': cid}, {'$set': {'last_in_at': now() - timedelta(days=2)}})
        c.portal.call(later.tick)
        assert len(sent) == 1
        d = c.portal.call(db.scheduled.find_one, {'id': sid2}, {'_id': 0})
        assert d['status'] == 'failed' and '24 horas' in d['error']
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert any(m.get('type') == 'note' and 'não saiu' in (m.get('text') or '') for m in msgs)

        # cancelar tira da fila
        r3 = c.post(f'/api/conversations/{cid}/later', json={'text': 'Terceira', 'at': (now() + timedelta(hours=2)).isoformat()})
        assert c.delete(f'/api/later/{r3.json()["id"]}').status_code == 200
        assert c.get(f'/api/conversations/{cid}/later').json() == []
