"""Pausar a Nanda pelo menu: qualquer pessoa do time para as respostas sozinhas; com hora marcada, ela volta sozinha."""
import os, asyncio
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from fastapi.testclient import TestClient
import ai
from core import db, now
from main import app

def test_pausar_voltar_e_acabar_sozinha():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona Pausa', 'email': 'pausa@x.com', 'password': 'senha-forte-1', 'company': 'Studio Pausa', 'segment': 'studio'})
        me = c.get('/api/auth/me').json(); cid = me['company']['id']
        assert me['nanda']['paused'] is False
        r = c.post('/api/ai/pause', json={'until': '1h'}).json()
        assert r['paused'] is True and r['pause_until']
        nz = c.get('/api/auth/me').json()['nanda']
        assert nz['paused'] is True and nz['pause_by'] == 'Dona Pausa'
        assert c.post('/api/ai/resume').json()['paused'] is False
        assert c.get('/api/auth/me').json()['nanda']['paused'] is False
        # desligar sem hora: fica pausada até alguém ligar
        r = c.post('/api/ai/pause', json={'until': 'off'}).json()
        assert r['paused'] is True and r['pause_until'] is None
        # até amanhã: quando a hora passa, volta sozinha
        c.post('/api/ai/pause', json={'until': 'tomorrow'})
        async def vence():
            await db.ai_settings.update_one({'company_id': cid}, {'$set': {'pause_until': now() - timedelta(minutes=1)}})
            return await ai.settings_for(cid)
        assert asyncio.run(vence())['paused'] is False
        assert c.post('/api/ai/pause', json={'until': 'sempre'}).status_code == 422
