import os, json, hmac, hashlib, time, asyncio, shutil, subprocess, tempfile
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
import pytest
from fastapi.testclient import TestClient
import whatsapp, media, ai
from core import db
from main import app

def hook(c, msg):
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '4440011'}, 'contacts': [{'wa_id': '5511950000001', 'profile': {'name': 'Cli M'}}], 'messages': [msg]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def m(t, **x): return {'from': '5511950000001', 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': t, **x}

def test_midia(monkeypatch):
    sent, ups, inbound = [], [], []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'o' + str(len(sent))}]}
        return {'success': True, 'display_phone_number': '+55'}
    async def fake_download(media_id, token): return (b'OGGDATA' if media_id == 'aud1' else b'\xff\xd8JPEG'), ('audio/ogg' if media_id == 'aud1' else 'image/jpeg')
    async def fake_upload(pnid, token, data, mime, name): ups.append((mime, name, len(data))); return 'up' + str(len(ups))
    async def fake_tr(data, mime, name): return 'Oi, queria saber o preço do plano trimestral'
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(media, 'download', fake_download)
    monkeypatch.setattr(media, 'upload', fake_upload); monkeypatch.setattr(media, 'transcribe', fake_tr)
    monkeypatch.setenv('TRANSCRIBE_API_KEY', 'x')
    monkeypatch.setattr(whatsapp, 'ON_INBOUND', lambda cid, kid, mid: inbound.append(mid))
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona M', 'email': 'dm@x.com', 'password': 'senha-forte-1', 'company': 'Studio M', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '4440011', 'waba_id': '4440012', 'token': 'EAA' + 'q' * 30})
        hook(c, m('audio', audio={'id': 'aud1', 'mime_type': 'audio/ogg; codecs=opus', 'voice': True}))
        c.portal.call(asyncio.sleep, 0.2)
        kid = c.get('/api/conversations').json()[0]['id']
        msgs = c.get(f'/api/conversations/{kid}').json()['messages']
        a = msgs[-1]
        assert a['type'] == 'audio' and a['transcript'].startswith('Oi, queria') and a['pending'] is False and a['file'].endswith('.ogg')
        assert inbound == [a['id']]  # a Nanda só é chamada depois da transcrição
        assert c.get('/api/conversations').json()[0]['last_text'].startswith('🎤 Oi, queria')
        r = c.get(f"/api/media/{a['id']}")
        assert r.status_code == 200 and r.content == b'OGGDATA' and r.headers['content-type'].startswith('audio/ogg')
        hook(c, m('image', image={'id': 'img1', 'mime_type': 'image/jpeg', 'caption': 'esse aqui'}))
        c.portal.call(asyncio.sleep, 0.2)
        h = c.portal.call(ai.history, kid)
        assert h[0]['role'] == 'user' and 'queria saber o preço' in json.dumps(h, ensure_ascii=False)
        last = h[-1]['content']
        assert isinstance(last, list) and [b['type'] for b in last] == ['text', 'image', 'text'] and last[2]['text'] == '[foto] esse aqui'
        # envio de foto, PDF e tipo recusado
        r = c.post(f'/api/conversations/{kid}/media', files={'file': ('foto.jpg', b'\xff\xd8abc', 'image/jpeg')}, data={'caption': 'Nossa tabela'})
        assert r.status_code == 200, r.text
        assert sent[-1]['type'] == 'image' and sent[-1]['image'] == {'id': 'up1', 'caption': 'Nossa tabela'}
        r = c.post(f'/api/conversations/{kid}/media', files={'file': ('Proposta.pdf', b'%PDF-1', 'application/pdf')})
        assert sent[-1]['document']['filename'] == 'Proposta.pdf' and r.json()['text'] == '📄 Documento'
        assert c.post(f'/api/conversations/{kid}/media', files={'file': ('x.exe', b'MZ', 'application/x-msdownload')}).status_code == 400
        assert c.get(f"/api/media/{r.json()['id']}").content == b'%PDF-1'
        if shutil.which('ffmpeg'):
            d = tempfile.mkdtemp(); src = os.path.join(d, 'a.webm')
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5', '-c:a', 'libopus', src], check=True)
            r = c.post(f'/api/conversations/{kid}/media', files={'file': ('gravacao.webm', open(src, 'rb').read(), 'audio/webm')}, data={'voice': 'true'})
            assert r.status_code == 200, r.text
            assert ups[-1][0] == 'audio/ogg' and ups[-1][1] == 'audio.ogg' and sent[-1]['type'] == 'audio' and r.json()['voice'] is True
        # janela fechada
        async def fecha(): await db.contacts.update_one({'id': kid}, {'$set': {'last_in_at': whatsapp.now() - whatsapp.timedelta(days=2)}})
        c.portal.call(fecha)
        assert c.post(f'/api/conversations/{kid}/media', files={'file': ('f.jpg', b'\xff\xd8', 'image/jpeg')}).status_code == 400
