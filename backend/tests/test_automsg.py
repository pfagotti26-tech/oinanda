"""Mensagens automáticas: boas-vindas, fora do horário, ao encerrar e assinatura com o nome."""
import os, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, inbox, automsg
from main import app
from tests.test_origins import inbound

def _wait(sent, n, t=3.0):
    end = time.time() + t
    while len(sent) < n and time.time() < end: time.sleep(0.05)

def test_mensagens_automaticas(monkeypatch):
    monkeypatch.setenv('AI_DEBOUNCE', '0')
    sent = []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload['text']['body']); return {'messages': [{'id': 'o%d' % len(sent)}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    closed = {'v': None}
    async def fake_next_open(co): return closed['v']
    monkeypatch.setattr(inbox, 'next_open', fake_next_open)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona Júlia Ramos', 'email': 'am@x.com', 'password': 'senha-forte-1', 'company': 'Studio A', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        c.put('/api/ai/settings', json={'default_mode': 'off'})
        g = c.get('/api/automsg').json()
        assert g['welcome']['on'] is False and '{primeiro_nome}' in g['welcome']['text'] and g['hours'] == ''  # conta nova: horário ainda não definido
        # desligadas: nada sai
        inbound(c, '5511900000001', 'Bruna Lima', 'Oi'); time.sleep(0.4)
        assert sent == []
        assert c.put('/api/automsg', json={'welcome': {'text': '  '}}).status_code == 400
        r = c.put('/api/automsg', json={'welcome': {'on': True}, 'away': {'on': True}, 'bye': {'on': True}, 'sign': True}).json()
        assert r['welcome']['on'] and r['away']['on'] and r['sign']
        # cliente novo, aberto: boas-vindas uma vez só
        inbound(c, '5511900000002', 'Carla Souza', 'Oi, tudo bem?'); _wait(sent, 1)
        assert sent == ['Oi, Carla! Que bom te ver por aqui 😊 Já vou te atender.']
        inbound(c, '5511900000002', 'Carla Souza', 'Quero marcar'); time.sleep(0.4)
        assert len(sent) == 1
        # fechado: aviso com a hora que volta, no máximo uma vez a cada 12 h
        closed['v'] = 'amanhã às 9h'
        inbound(c, '5511900000003', 'Davi', 'Ainda atende?'); _wait(sent, 2)
        assert sent[-1] == 'Oi, Davi! Agora estamos fechados. Voltamos amanhã às 9h e te respondemos primeiro. 😊'
        inbound(c, '5511900000003', 'Davi', 'Alô?'); time.sleep(0.4)
        assert len(sent) == 2
        closed['v'] = None
        # assinatura com o nome de quem atende
        cid = next(x for x in c.get('/api/conversations?filter=all').json() if x['name'] == 'Carla Souza')['id']
        c.post(f'/api/conversations/{cid}/send', json={'text': 'Tenho sábado às 9h.'})
        assert sent[-1] == '*Dona:*\nTenho sábado às 9h.'
        # ao encerrar: agradece (pesquisa desligada)
        r = c.post(f'/api/conversations/{cid}/resolve', json={'resolved': True, 'reason': 'Agendou', 'survey': True}).json()
        assert r['bye'] == 'enviada' and sent[-1].startswith('Obrigada pelo contato, Carla!')
        # pesquisa ligada e enviada: não manda o agradecimento por cima
        c.put('/api/closing/settings', json={'csat': True})
        n = len(sent)
        r = c.post(f'/api/conversations/{cid}/resolve', json={'resolved': True, 'reason': 'Agendou', 'survey': True}).json()
        assert r['survey'] == 'enviada' and r['bye'] is None and len(sent) == n + 1

def test_texto_sem_nome_e_horario():
    co = {'name': 'Studio'}
    assert automsg.fill('Oi, {primeiro_nome}! Tudo bem?', {'name': '+5511999990000'}, co) == 'Oi! Tudo bem?'
    assert automsg.hours_text({'0': [['09:00', '18:00']], '1': [['09:00', '18:00']], '2': [['09:00', '18:00']], '3': [['09:00', '18:00']], '4': [['09:00', '18:00']], '5': [['09:00', '13:00']], '6': []}) == 'Seg a sex, 9h às 18h · sáb, 9h às 13h'
