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

def test_voz_no_instagram(monkeypatch, tmp_path):
    import instagram
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-teste-1234567890abcdef'); monkeypatch.setenv('MEDIA_DIR', str(tmp_path))
    sent = []
    async def fake_tts(text, vid): return b'mp3'
    async def fake_send(cid, c, message, human, ch='instagram'):
        mid = message['attachment']['payload']['url'].split('/api/media/pub/')[1].split('?')[0]
        sent.append((message, human, ch, bool(await db.messages.find_one({'id': mid}))))  # a Meta baixa durante o envio: já precisa existir
        return {'message_id': 'mid.1'}
    monkeypatch.setattr(voice, 'tts', fake_tts); monkeypatch.setattr(instagram, 'send', fake_send)
    monkeypatch.setattr(media, 'to_m4a', lambda d, m: (b'm4a', 'audio/mp4'))
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Vera Dona', 'email': 'vera-ig@voz.com', 'password': 'senha-forte-1', 'company': 'Clínica Vera', 'segment': 'clinica'})
        c.put('/api/ai/voice', json={'voice_reply': True})
        cid = c.get('/api/auth/me').json()['company']['id']
        ct = {'id': uid(), 'company_id': cid, 'name': 'Lia', 'ig_id': '178400', 'channel': 'instagram', 'last_in_at': now(), 'created_at': now()}
        audio = uid()
        async def setup():
            await db.contacts.insert_one(ct.copy())
            await db.messages.insert_one({'id': audio, 'company_id': cid, 'contact_id': ct['id'], 'channel': 'instagram', 'dir': 'in', 'type': 'audio', 'at': now()})
        c.portal.call(setup)
        m = c.portal.call(lambda: voice.reply(cid, ct, 'Oi, Lia! Temos horário amanhã 😊', 'Nanda (IA)', audio))
        assert m['type'] == 'audio' and m['channel'] == 'instagram' and m['tts'] and m['wa_msg_id'] == 'mid.1'
        msg, human, ch, existed = sent[-1]
        assert existed and msg['attachment']['type'] == 'audio' and '/api/media/pub/' + m['id'] in msg['attachment']['payload']['url'] and human is False and ch == 'instagram'
        assert c.get('/api/ai/voice').json()['used'] == 1
        r = c.get(msg['attachment']['payload']['url'].split('testserver')[-1] if 'testserver' in msg['attachment']['payload']['url'] else '/api' + msg['attachment']['payload']['url'].split('/api', 1)[1])
        assert r.status_code == 200 and r.content == b'm4a'  # a Meta consegue baixar pelo link assinado

def test_voz_recusada_avisa_na_conversa(monkeypatch, tmp_path):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-teste-1234567890abcdef'); monkeypatch.setenv('MEDIA_DIR', str(tmp_path))
    async def fake_tts(text, vid): voice.LAST_ERR = 'A ElevenLabs não liberou essa voz pela API.'; return None
    async def fake_graph(method, path, token, body=None): return {'messages': [{'id': 'wamid.y'}]}
    monkeypatch.setattr(voice, 'tts', fake_tts); monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Vera Dona', 'email': 'vera-no@voz.com', 'password': 'senha-forte-1', 'company': 'Clínica Vera', 'segment': 'clinica'})
        c.put('/api/ai/voice', json={'voice_reply': True})
        cid = c.get('/api/auth/me').json()['company']['id']
        ct = {'id': uid(), 'company_id': cid, 'name': 'Lia', 'wa_id': '5511955551111', 'last_in_at': now(), 'created_at': now()}
        audio = uid()
        async def setup():
            await db.wa_numbers.insert_one({'company_id': cid, 'phone_number_id': '124', 'token_enc': whatsapp.encrypt('tok'), 'status': 'connected'})
            await db.contacts.insert_one(ct.copy())
            await db.messages.insert_one({'id': audio, 'company_id': cid, 'contact_id': ct['id'], 'dir': 'in', 'type': 'audio', 'at': now()})
        c.portal.call(setup)
        m = c.portal.call(lambda: voice.reply(cid, ct, 'Oi, Lia!', 'Nanda (IA)', audio))
        assert m['type'] == 'text'
        notes = c.portal.call(lambda: db.messages.find({'contact_id': ct['id'], 'dir': 'sys'}, {'_id': 0}).to_list(10))
        assert any('em texto em vez de áudio' in n['text'] and 'liberou essa voz' in n['text'] for n in notes)

def test_texto_falado():
    import asyncio
    assert voice.money_fallback('Custa R$ 1.597,00; ou R$ 90') == 'Custa 1597 reais, ou 90 reais'
    async def fake_llm(system, msgs, **k): return 'O Essencial sai por cento e quarenta e sete reais por mês.', {}
    import ai; old = ai.llm; ai.llm = fake_llm
    try: assert asyncio.run(voice.spoken('O Essencial custa R$ 147 por mês.')) == 'O Essencial sai por cento e quarenta e sete reais por mês.'
    finally: ai.llm = old
    async def bad_llm(system, msgs, **k): return 'x', {}  # resposta estranha: fica o texto original ajustado
    ai.llm = bad_llm
    try: assert asyncio.run(voice.spoken('Custa R$ 90.')) == 'Custa 90 reais.'
    finally: ai.llm = old

def test_tts_tenta_modelo_mais_simples(monkeypatch):
    import asyncio, httpx
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'xi-teste')
    seen = []
    class R:
        def __init__(self, st): self.status_code = st; self.text = '{"detail":{"status":"invalid","message":"model not available"}}'; self.content = b'mp3' if st == 200 else b''
        def json(self): return {'detail': {'status': 'invalid', 'message': 'model not available'}}
    class C:
        def __init__(self, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, params=None, headers=None, json=None):
            seen.append((json['model_id'], 'voice_settings' in json)); return R(200 if json['model_id'] == voice.FALLBACK_MODEL else 400)
    monkeypatch.setattr(voice.httpx, 'AsyncClient', C); voice.SKIP.clear()
    assert asyncio.run(voice.tts('oi', 'v')) == b'mp3'
    assert seen == [(voice.MODEL, True), (voice.MODEL, False), (voice.FALLBACK_MODEL, True)]
    seen.clear(); asyncio.run(voice.tts('oi', 'v')); assert seen == [(voice.FALLBACK_MODEL, True)]  # lembra que o modelo não está liberado
    voice.SKIP.clear()
