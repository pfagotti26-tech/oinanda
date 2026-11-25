"""Ensinar numa frase: a IA organiza, o dono confere, só então salva. Site só com endereço público."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import ai, describe
from main import app

OUT = '''{"sobre":"Clínica de estética em Moema, São Paulo.","onde":"Moema, São Paulo","itens":[{"nome":"Limpeza de pele","preco":180,"duracao_min":60},
{"nome":"Botox","preco":900,"a_partir":true,"duracao_min":null},{"nome":"Massagem","preco":-3}],"horarios":[{"dias":[0,1,2,3,4],"de":"9:00","ate":"19h"},{"dias":[5],"de":"09:00","ate":"13:00"},{"dias":[6],"de":"10:00","ate":"08:00"}],
"pagamento":"Pix e cartão em até 3x","faltou":["Duração do botox"]}'''

def test_descreve_confere_e_salva(monkeypatch):
    async def fake_llm(system, msgs, **k): return OUT, {'input_tokens': 5, 'output_tokens': 5}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'ds1@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        assert c.post('/api/ai/describe', json={'text': 'oi'}).status_code == 400
        r = c.post('/api/ai/describe', json={'text': 'Clínica de estética em Moema. Limpeza de pele R$ 180, botox a partir de 900. Seg a sex 9h às 19h, sáb até 13h.'}).json()
        assert [i['name'] for i in r['items']] == ['Limpeza de pele', 'Botox', 'Massagem'] and r['items'][2]['price'] is None and r['items'][1]['from']
        assert r['hours_grid'] == [{'days': [0, 1, 2, 3, 4], 'from': '09:00', 'to': '19:00'}, {'days': [5], 'from': '09:00', 'to': '13:00'}]  # o horário impossível sai
        assert r['missing'] == ['Duração do botox']
        assert c.get('/api/setup').json()['core'][0]['done'] is False  # nada salvo antes de conferir
        c.post('/api/ai/describe/save', json=r)
        s = c.get('/api/ai/settings').json()
        assert s['about'].startswith('Clínica de estética em Moema') and 'Horário de atendimento: de segunda a sexta, das 09:00 às 19:00; sábado, das 09:00 às 13:00' in s['about']
        assert 'Limpeza de pele' in s['knowledge'] and 'Limpeza de pele leva cerca de 60 minutos' in s['knowledge'] and 'Pix e cartão em até 3x' in s['knowledge']
        assert {x['name']: x['price'] for x in s['catalog']}['Limpeza de pele'] == 180
        core = {x['key']: x['done'] for x in c.get('/api/setup').json()['core']}
        assert core['sobre'] and core['precos']

def test_site_so_endereco_publico():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'ds2@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        for u in ['http://127.0.0.1:8000', 'http://localhost/x', 'http://169.254.169.254/latest', 'ftp://x.com']:
            assert c.post('/api/ai/describe', json={'url': u}).status_code == 400
        r = c.post('/api/ai/describe', json={'url': 'instagram.com/clinicabella'}); assert r.status_code == 400 and 'bio' in r.json()['detail']

def test_monte_com_toques_preco_fixo_a_partir_e_sob_consulta(monkeypatch):
    seen = []
    async def fake_llm(system, msgs, **k):
        seen.append(system); return '{"reply":"ok"}', {}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'ds3@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        c.post('/api/ai/describe/save', json={'about': 'Clínica Bella, clínica de estética em Moema.', 'where': 'Rua Gaivota, 1200, Moema', 'payment': 'Pix, crédito e débito · até 3x no cartão',
            'items': [{'name': 'Limpeza de pele', 'price': 180, 'mins': 60}, {'name': 'Drenagem', 'price': 150, 'from': True}, {'name': 'Botox', 'price': 900, 'consult': True}],
            'hours_grid': [{'days': [0, 1, 2, 3, 4], 'from': '09:00', 'to': '19:00'}], 'rules': ['Tolerância de 15 minutos de atraso'], 'notes': ['Tem estacionamento']})
        s = c.get('/api/ai/settings').json(); cat = {x['name']: x for x in s['catalog']}
        assert cat['Botox']['price'] is None and cat['Drenagem']['price_from'] and not cat['Limpeza de pele']['price_from']
        assert 'Tolerância de 15 minutos' in s['rules'] and 'Tem estacionamento' in s['knowledge'] and 'Botox: valor sob consulta' in s['knowledge']
        c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'oi'}]})
        assert '- Drenagem: a partir de R$ 150' in seen[-1] and '- Limpeza de pele: R$ 180' in seen[-1] and '- Botox: preço sob consulta' in seen[-1]
