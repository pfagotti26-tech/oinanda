"""Corrigir uma resposta no teste: só o português muda, o que o dono escreveu vira resposta ensinada e a Nanda refaz respeitando isso."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import ai
from main import app

CONV = [{'dir': 'in', 'text': 'Oi, você faz drenagem?'}, {'dir': 'out', 'text': 'Fazemos sim! Custa R$ 150.'}, {'dir': 'in', 'text': 'pode ser. no sábado funciona?'}]

def test_corrigir_e_refazer(monkeypatch):
    seen = []
    async def fake_llm(system, msgs, **k):
        if system == ai.FIX_PT:  # revisão de português
            return msgs[0]['content'].replace('funcionamaos', 'funcionamos').replace('ao 12:00', 'às 12:00'), {'input_tokens': 5, 'output_tokens': 5}
        seen.append((system, msgs)); return '{"reply":"Claro! Aos sábados funcionamos das 08:00 às 12:00. Você tem preferência de horário?"}', {}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'lv2@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        r = c.post('/api/ai/correct', json={'messages': CONV, 'original': 'Vou chamar a Camila', 'mode': 'texto',
                                            'text': 'Claro! Aos sábados funcionamaos das 08:00 ao 12:00. Você já tem preferência de horário?'}).json()
        assert r['lesson']['a'] == 'Claro! Aos sábados funcionamos das 08:00 às 12:00. Você já tem preferência de horário?'
        assert ['funcionamaos', 'funcionamos'] in r['changes'] and r['reply'].startswith('Claro!')
        system, msgs = seen[-1]
        assert 'RESPOSTAS QUE O DONO ENSINOU' in system and 'pode ser. no sábado funciona?' in system and 'funcionamos das 08:00' in system
        assert msgs[-1] == {'role': 'user', 'content': 'pode ser. no sábado funciona?'}  # refaz a partir da pergunta, sem a resposta antiga
        assert 'Camila' not in system  # nada de nome inventado no exemplo de passar para o time
        # a mesma pergunta corrigida de novo substitui, não duplica; "dizer o que mudar" vira pedido
        c.post('/api/ai/correct', json={'messages': CONV, 'mode': 'pedido', 'text': 'não chame o time, pergunte qual horário a pessoa prefere'})
        ex = c.get('/api/ai/settings').json()['examples']
        assert len(ex) == 1 and ex[0]['note'].startswith('não chame o time') and ex[0]['a'] == ''
        assert 'O dono pediu: não chame o time' in seen[-1][0]
        assert c.delete('/api/ai/examples/' + ex[0]['id']).json()['examples'] == []

def test_revisao_que_muda_demais_fica_com_o_texto_do_dono(monkeypatch):
    async def fake_llm(system, msgs, **k):
        if system == ai.FIX_PT: return 'Olá! Ficamos muito felizes com o seu contato, nosso horário de sábado é pela manhã.', {}
        return '{"reply":"ok"}', {}
    monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Lívia', 'email': 'lv3@x.com', 'password': 'senha-forte-1', 'company': 'Bella', 'segment': 'clinica'})
        r = c.post('/api/ai/correct', json={'messages': CONV, 'text': 'sabado abrimos das 8 as 12'}).json()
        assert r['lesson']['a'] == 'sabado abrimos das 8 as 12' and r['changes'] == []
