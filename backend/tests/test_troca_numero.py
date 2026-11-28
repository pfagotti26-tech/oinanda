"""Troca de número: conversas antigas ficam, marcadas como 'número antigo' até o cliente falar com o número novo."""
import os, json, hmac, hashlib, time, asyncio
os.environ.update(MONGO_URL='mock', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from core import db
from main import app

async def fake_graph(method, path, token, payload=None, params=None):
    if path.endswith('/messages'): return {'messages': [{'id': 'wamid.OUT%d' % time.time_ns()}]}
    if 'subscribed_apps' in path: return {'success': True}
    return {'display_phone_number': '+55 11 9' + path[-4:], 'verified_name': 'Loja T', 'quality_rating': 'GREEN'}

def signed(c, body):
    raw = json.dumps(body).encode()
    sig = 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': sig, 'content-type': 'application/json'})

def msg(pnid, wa, mid):
    return {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': pnid}, 'contacts': [{'wa_id': wa, 'profile': {'name': 'C' + wa[-2:]}}],
        'messages': [{'from': wa, 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'oi'}}]}}]}]}

def test_troca_de_numero(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona T', 'email': 'tn@x.com', 'password': 'senha-forte-1', 'company': 'Loja T'})
        assert c.post('/api/wa/connect', json={'phone_number_id': '5550001111', 'waba_id': '9990001', 'token': 'EAA' + 'x' * 30}).status_code == 200
        signed(c, msg('5550001111', '5511911110001', 'wamid.A1')); signed(c, msg('5550001111', '5511911110002', 'wamid.A2'))
        rows = {r['wa_id']: r for r in c.get('/api/conversations').json()}
        assert not rows['5511911110001']['old_line'] and not rows['5511911110002']['old_line']
        # troca: desconecta e conecta outro número
        c.delete('/api/wa/number')
        assert c.post('/api/wa/connect', json={'phone_number_id': '5550002222', 'waba_id': '9990002', 'token': 'EAA' + 'y' * 30}).status_code == 200
        rows = {r['wa_id']: r for r in c.get('/api/conversations').json()}
        assert len(rows) == 2 and rows['5511911110001']['old_line'] and rows['5511911110002']['old_line']  # histórico continua, marcado
        signed(c, msg('5550002222', '5511911110001', 'wamid.B1'))  # cliente falou com o número novo
        rows = {r['wa_id']: r for r in c.get('/api/conversations').json()}
        assert not rows['5511911110001']['old_line'] and rows['5511911110002']['old_line']
        cid = rows['5511911110002']['id']
        assert rows['5511911110002']['window_open'] is False  # a janela é por número
        r = c.post(f'/api/conversations/{cid}/send', json={'text': 'oi'}); assert r.status_code == 400 and 'número anterior' in r.json()['detail']
        assert c.post(f"/api/conversations/{rows['5511911110001']['id']}/send", json={'text': 'oi'}).status_code == 200

def test_backfill_marca_quem_ja_falou_com_o_atual(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona B', 'email': 'tb@x.com', 'password': 'senha-forte-1', 'company': 'Loja B'})
        assert c.post('/api/wa/connect', json={'phone_number_id': '5550003333', 'waba_id': '9990003', 'token': 'EAA' + 'z' * 30}).status_code == 200
        signed(c, msg('5550003333', '5511922220001', 'wamid.C1'))
        async def legado():  # como estava antes desta versão: sem a marca do número
            await db.contacts.update_many({'wa_id': '5511922220001'}, {'$unset': {'wa_line': ''}})
            await db.wa_numbers.update_many({}, {'$unset': {'lines_ok': ''}})
            await whatsapp.backfill_lines()
        asyncio.run(legado())
        rows = {r['wa_id']: r for r in c.get('/api/conversations').json()}
        assert rows['5511922220001'].get('old_line') is False
