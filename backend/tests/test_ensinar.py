"""Ensinar em 1 minuto: itens com preço vão para o Catálogo, horário e endereço para a Nanda; o teste não usa o exemplo do ramo."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import ai
from main import app

def test_ensinar_e_testar_sem_exemplo(monkeypatch):
    seen = []
    async def fake_llm(system, msgs, **k):
        seen.append(system); return '{"reply":"ok"}', {}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'lv@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'quanto custa?'}]})
        assert 'Avaliação gratuita para harmonização' not in seen[-1]  # o exemplo do ramo não vira verdade
        r = c.post('/api/ai/quick', json={'items': [{'name': 'Limpeza de pele', 'price': 180}, {'name': 'Drenagem', 'price': None}], 'hours': 'seg a sex, 9h às 19h', 'where': 'Moema, São Paulo'}).json()
        assert {x['name'] for x in r['catalog']} >= {'Limpeza de pele', 'Drenagem'}
        c.post('/api/ai/quick', json={'items': [{'name': 'limpeza de pele', 'price': 200}], 'hours': 'seg a sáb, 9h às 18h'})  # mesmo item: atualiza o preço, não duplica
        s = c.get('/api/ai/settings').json()
        assert [x['price'] for x in s['catalog'] if x['name'] == 'Limpeza de pele'] == [200]
        assert 'Horário de atendimento: seg a sáb, 9h às 18h' in s['about'] and 'seg a sex' not in s['about'] and 'Moema' in s['about']
        c.post('/api/ai/quick', json={'extra': 'Pergunta: quanto dura a drenagem?\nResposta: 50 minutos.'})
        assert '50 minutos' in c.get('/api/ai/settings').json()['knowledge']
        c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'quanto custa?'}]})
        assert 'Limpeza de pele' in seen[-1] and '50 minutos' in seen[-1]
        st = c.get('/api/setup').json(); done = {x['key'] for x in st['steps'] if x['done']}
        assert {'sobre', 'precos'} <= done
