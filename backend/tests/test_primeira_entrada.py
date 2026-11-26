"""Primeira entrada: 5 passos essenciais e conversa de exemplo do ramo enquanto nada foi ensinado."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import ai
from main import app

def test_passos_essenciais_e_exemplo(monkeypatch):
    seen = []
    async def fake_llm(system, msgs, **k):
        seen.append(system); return '{"reply":"A limpeza de pele sai R$ 180."}', {}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'pe1@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        d = c.get('/api/setup').json()
        assert [s['key'] for s in d['core']] == ['testar', 'precos', 'whatsapp'] and d['core_done'] == 0 and d['core_total'] == 3 and d['core_pct'] == 0
        r = c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'quanto custa a limpeza de pele?'}], 'example': True}).json()
        assert r['example'] and 'Limpeza de pele: R$ 180' in seen[-1]
        # sem pedir exemplo, nada de preço inventado
        c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'oi'}]}); assert 'R$ 180' not in seen[-1]
        # ensinou: o exemplo some, vale o que ela ensinou
        c.put('/api/ai/settings', json={'about': 'Clínica Bella, em Moema.', 'offer': 'Peeling R$ 250'})
        r = c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'oi'}], 'example': True}).json()
        assert not r['example'] and 'R$ 180' not in seen[-1] and 'Peeling R$ 250' in seen[-1]
        # quem trabalha sozinho pula o time e os passos essenciais ficam prontos com o resto
        c.post('/api/setup/skip', json={'key': 'time'})
        d = c.get('/api/setup').json(); assert [s['key'] for s in d['core']] == ['testar', 'precos', 'whatsapp'] and d['core_done'] == 2
