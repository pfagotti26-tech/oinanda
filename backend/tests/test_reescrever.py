"""Caixa de mensagem: corrigir, encurtar e deixar mais simpático, sem enviar nada."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif', ANTHROPIC_API_KEY='x')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import ai
from main import app

def test_reescrever(monkeypatch):
    seen = []
    async def fake(system, msgs, **k):
        seen.append(system)
        if system == ai.FIX_PT: return 'Tenho sim! Sábado às 9h.', {'input_tokens': 1}
        return 'Tenho sábado às 9h 😊', {'input_tokens': 1}
    monkeypatch.setattr(ai, 'llm', fake)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'rw@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        r = c.post('/api/ai/rewrite', json={'text': 'Tenho sim! Sabado as 9h.', 'mode': 'pt'}).json()
        assert r['text'] == 'Tenho sim! Sábado às 9h.' and ['Sabado as', 'Sábado às'] in r['changes']
        r = c.post('/api/ai/rewrite', json={'text': 'tenho sabado as 9h', 'mode': 'simpatico'}).json()
        assert r['text'] == 'Tenho sábado às 9h 😊' and 'simpática' in seen[-1]
        assert c.post('/api/ai/rewrite', json={'text': 'oi', 'mode': 'outro'}).status_code == 422
