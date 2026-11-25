"""A chamada à IA desliga o pensamento adaptativo (senão ele gasta o limite e a resposta sai cortada ou vazia)."""
import os, asyncio
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
import ai

class R:
    status_code = 200; content = b'x'
    def json(self): return {'content': [{'type': 'thinking', 'thinking': '...'}, {'type': 'text', 'text': '{"reply":"oi"}'}], 'usage': {'output_tokens': 5}}

def run(monkeypatch, env):
    seen = {}
    class C:
        def __init__(self, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers=None, json=None): seen.update(json); return R()
    monkeypatch.setattr(ai.httpx, 'AsyncClient', C); monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    if env: monkeypatch.setenv('AI_THINKING', env)
    else: monkeypatch.delenv('AI_THINKING', raising=False)
    text, _ = asyncio.run(ai.llm('sys', [{'role': 'user', 'content': 'oi'}]))
    return seen, text

def test_pensamento_desligado_por_padrao(monkeypatch):
    seen, text = run(monkeypatch, None)
    assert seen['thinking'] == {'type': 'disabled'} and seen['max_tokens'] == 900 and text == '{"reply":"oi"}'

def test_pensamento_ligado_com_folga(monkeypatch):
    seen, _ = run(monkeypatch, 'on')
    assert 'thinking' not in seen and seen['max_tokens'] == 4000
