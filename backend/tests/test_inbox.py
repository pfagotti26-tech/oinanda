import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '9990001'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_caixa(monkeypatch):
    calls = {'llm': 0}
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    async def fake_llm(system, messages, max_tokens=500):
        calls['llm'] += 1
        return json.dumps({'reply': 'O plano custa R$ 289.', 'handoff': False}), {}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm); monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona Caixa', 'email': 'cx@x.com', 'password': 'senha-forte-1', 'company': 'Studio Caixa', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '9990001', 'waba_id': '9990002', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511900001111', 'Marina Alves', 'Quanto custa?')
        inbound(c, '5511900002222', 'Rafael Costa', 'Tem aula às 7h?')
        cnt = c.get('/api/inbox/counts').json()
        assert cnt['all'] == 2 and cnt['waiting'] == 2 and cnt['unassigned'] == 2
        assert [x['name'] for x in c.get('/api/conversations', params={'q': 'mari'}).json()] == ['Marina Alves']
        assert [x['name'] for x in c.get('/api/conversations', params={'q': '2222'}).json()] == ['Rafael Costa']
        mid = c.get('/api/conversations', params={'q': 'Marina'}).json()[0]['id']
        # sugestão automática fica guardada para a mesma mensagem
        s1 = c.post(f'/api/conversations/{mid}/suggest', params={'cached': True}).json()
        s2 = c.post(f'/api/conversations/{mid}/suggest', params={'cached': True}).json()
        assert s1['reply'] == s2['reply'] == 'O plano custa R$ 289.' and calls['llm'] == 1
        # nota interna não vai para o cliente nem muda a fila
        n = c.post(f'/api/conversations/{mid}/note', json={'text': 'Ela já veio ano passado'}).json()
        assert n['dir'] == 'note'
        msgs = c.get(f'/api/conversations/{mid}').json()['messages']
        assert msgs[-1]['dir'] == 'note' and c.get('/api/inbox/counts').json()['waiting'] == 2
        # resolvido sai da fila e volta quando o cliente escreve
        c.post(f'/api/conversations/{mid}/resolve', json={'resolved': True})
        assert c.get('/api/inbox/counts').json()['waiting'] == 1
        assert [x['name'] for x in c.get('/api/conversations', params={'filter': 'waiting'}).json()] == ['Rafael Costa']
        time.sleep(0.01)
        inbound(c, '5511900001111', 'Marina Alves', 'Oi de novo')
        assert c.get('/api/inbox/counts').json()['waiting'] == 2
        # respostas rápidas
        r = c.post('/api/quick-replies', json={'shortcut': '/Preço', 'text': 'Oi, {primeiro_nome}! O mensal é R$ 289.'})
        assert r.status_code == 200 and r.json()['shortcut'] == 'preco'
        assert c.post('/api/quick-replies', json={'shortcut': 'preco', 'text': 'x'}).status_code == 409
        rid = r.json()['id']
        assert c.patch(f'/api/quick-replies/{rid}', json={'shortcut': 'valores', 'text': 'Mensal R$ 289'}).json()['shortcut'] == 'valores'
        assert [q['shortcut'] for q in c.get('/api/quick-replies').json()] == ['valores']
        assert c.delete(f'/api/quick-replies/{rid}').json()['ok']
