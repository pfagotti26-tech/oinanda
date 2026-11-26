import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import voice, media
from main import app

def test_audio_no_teste_da_nanda(monkeypatch):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-teste-1234567890abcdef'); monkeypatch.setenv('TRANSCRIBE_API_KEY', 'k')
    said = []
    async def fake_tr(data, mime, fn): return 'Tem estacionamento?' if data else None
    async def fake_tts(text, vid): said.append((text, vid)); return b'mp3'
    async def fake_spoken(t): return t
    monkeypatch.setattr(media, 'transcribe', fake_tr); monkeypatch.setattr(voice, 'tts', fake_tts); monkeypatch.setattr(voice, 'spoken', fake_spoken)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Vera Dona', 'email': 'vera@aud.com', 'password': 'senha-forte-1', 'company': 'Clínica Vera', 'segment': 'clinica'})
        r = c.post('/api/ai/test/audio', files={'file': ('t.webm', b'abc', 'audio/webm')}).json()
        assert r['text'] == 'Tem estacionamento?' and r['left'] == 9 and r['voice'] is False
        c.put('/api/ai/voice', json={'voice_reply': True, 'voice_id': voice.VOICES[2]['id']})
        r = c.post('/api/ai/test/audio', files={'file': ('t.webm', b'abc', 'audio/webm')}).json()
        assert r['voice'] and r['voice_name'] == 'Fernanda'
        s = c.post('/api/ai/test/speak', json={'text': 'Tem sim! *Estacionamento* na rua de trás 🚗'})
        assert s.status_code == 200 and s.content == b'mp3' and said[0] == ('Tem sim! Estacionamento na rua de trás', voice.VOICES[2]['id'])
        assert c.post('/api/ai/test/speak', json={'text': 'Veja em https://x.com'}).status_code == 400
        for _ in range(8): c.post('/api/ai/test/audio', files={'file': ('t.webm', b'abc', 'audio/webm')})
        assert c.post('/api/ai/test/audio', files={'file': ('t.webm', b'abc', 'audio/webm')}).status_code == 429
        assert c.get('/api/ai/voice').json()['used'] == 0  # teste não gasta a franquia do mês
