import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import voice, media, whatsapp
from core import db, now, uid
from main import app

def test_voz(monkeypatch, tmp_path):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-teste-1234567890abcdef'); monkeypatch.setenv('MEDIA_DIR', str(tmp_path))
    sent, spoken = [], []
    async def fake_tts(text, vid): spoken.append((text, vid)); return b'mp3'
    async def fake_up(*a): return 'media1'
    async def fake_graph(method, path, token, body=None): sent.append(body); return {'messages': [{'id': 'wamid.x'}]}
    monkeypatch.setattr(voice, 'tts', fake_tts); monkeypatch.setattr(media, 'upload', fake_up); monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setattr(media, 'to_voice', lambda d, m: (b'ogg', 'audio/ogg'))
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Vera Dona', 'email': 'vera@voz.com', 'password': 'senha-forte-1', 'company': 'Clínica Vera', 'segment': 'clinica'})
        v = c.get('/api/ai/voice').json()
        assert v['ready'] and not v['on'] and len(v['voices']) >= 6 and v['quota'] > 0 and v['used'] == 0
        assert c.put('/api/ai/voice', json={'voice_id': 'nao-existe'}).status_code == 400
        v = c.put('/api/ai/voice', json={'voice_reply': True, 'voice_id': voice.VOICES[4]['id']}).json()
        assert v['on'] and v['voice_id'] == voice.VOICES[4]['id']
        cid = c.get('/api/auth/me').json()['company']['id']
        ct = {'id': uid(), 'company_id': cid, 'name': 'Lia', 'wa_id': '5511955550000', 'last_in_at': now(), 'created_at': now()}
        audio, texto = uid(), uid()
        def setup():
            async def go():
                await db.wa_numbers.insert_one({'company_id': cid, 'phone_number_id': '123', 'token_enc': whatsapp.encrypt('tok'), 'status': 'connected'})
                await db.contacts.insert_one(ct.copy())
                await db.messages.insert_many([{'id': audio, 'company_id': cid, 'contact_id': ct['id'], 'dir': 'in', 'type': 'audio', 'at': now()},
                                               {'id': texto, 'company_id': cid, 'contact_id': ct['id'], 'dir': 'in', 'type': 'text', 'text': 'oi', 'at': now()}])
            return go()
        c.portal.call(setup)
        # cliente mandou áudio: responde em áudio, sem emoji
        m = c.portal.call(lambda: voice.reply(cid, ct, 'Oi, Lia! Temos horário amanhã às 10h 😊', 'Nanda (IA)', audio))
        assert m['type'] == 'audio' and m['tts'] and spoken[-1] == ('Oi, Lia! Temos horário amanhã às 10h', voice.VOICES[4]['id'])
        assert sent[-1]['type'] == 'audio' and c.get('/api/ai/voice').json()['used'] == 1
        # cliente mandou texto: texto
        m = c.portal.call(lambda: voice.reply(cid, ct, 'Claro!', 'Nanda (IA)', texto))
        assert m['type'] == 'text' and len(spoken) == 1
        # resposta com link: texto, mesmo vindo de áudio
        m = c.portal.call(lambda: voice.reply(cid, ct, 'Pague aqui: https://pag.me/x', 'Nanda (IA)', audio))
        assert m['type'] == 'text'
        # franquia acabou: texto
        c.portal.call(lambda: db.voice_usage.update_one({'company_id': cid, 'month': voice.month()}, {'$set': {'count': 9999}}))
        m = c.portal.call(lambda: voice.reply(cid, ct, 'Até amanhã!', 'Nanda (IA)', audio))
        assert m['type'] == 'text'
        # voz desligada
        c.put('/api/ai/voice', json={'voice_reply': False})
        c.portal.call(lambda: db.voice_usage.update_one({'company_id': cid, 'month': voice.month()}, {'$set': {'count': 0}}))
        assert c.portal.call(lambda: voice.reply(cid, ct, 'Até!', 'Nanda (IA)', audio))['type'] == 'text'

def test_ouvir(monkeypatch, tmp_path):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-teste-1234567890abcdef'); monkeypatch.setenv('MEDIA_DIR', str(tmp_path))
    calls = []
    async def fake_tts(text, vid): calls.append(text); return b'ID3mp3'
    monkeypatch.setattr(voice, 'tts', fake_tts)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Ana Dona', 'email': 'ana@voz2.com', 'password': 'senha-forte-1', 'company': 'Salão Ana', 'segment': 'salao'})
        r = c.post('/api/ai/voice/test', json={'voice_id': voice.VOICES[1]['id']})
        assert r.status_code == 200 and r.headers['content-type'] == 'audio/mpeg' and r.content == b'ID3mp3' and 'Salão Ana' in calls[0]
        assert c.post('/api/ai/voice/test', json={'voice_id': voice.VOICES[1]['id']}).status_code == 200 and len(calls) == 1  # guardado
        async def no(text, vid): voice.LAST_ERR = 'A ElevenLabs não liberou essa voz pela API.'; return None
        monkeypatch.setattr(voice, 'tts', no)
        r = c.post('/api/ai/voice/test', json={'voice_id': voice.VOICES[2]['id']})
        assert r.status_code == 400 and 'não liberou' in r.json()['detail']
