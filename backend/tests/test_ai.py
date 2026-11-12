import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  AI_DEBOUNCE='0.05', ANTHROPIC_API_KEY='teste')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai
from main import app

sent, prompts = [], []
async def fake_graph(method, path, token, payload=None, params=None):
    if path.endswith('/messages'):
        sent.append(payload['text']['body']); return {'messages': [{'id': f'wamid.AI{len(sent)}'}]}
    if 'subscribed_apps' in path: return {'success': True}
    return {'display_phone_number': '+55 11 95555-0000', 'verified_name': 'Clínica IA'}

async def fake_llm(system, messages, max_tokens=500):
    prompts.append((system, messages))
    last = messages[-1]['content']
    if 'botox' in last: return '{"reply": "Vou chamar a Dra. Camila, ela já te responde!", "handoff": true, "reason": "pergunta clínica"}', {'input_tokens': 10, 'output_tokens': 5}
    return '{"reply": "Oi Lu! A limpeza de pele custa R$ 180.", "handoff": false, "reason": ""}', {'input_tokens': 100, 'output_tokens': 20}

def signed(c, body):
    raw = json.dumps(body).encode()
    sig = 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': sig, 'content-type': 'application/json'})

def inbound(text, mid):
    return {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770001112'},
        'contacts': [{'wa_id': '5511977776666', 'profile': {'name': 'Luciana Prado'}}],
        'messages': [{'from': '5511977776666', 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}]}}]}]}

def wait(cond, t=3):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False

def test_nanda(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm)
    monkeypatch.setenv('AI_DEBOUNCE', '0.05'); monkeypatch.setenv('ANTHROPIC_API_KEY', 'teste'); monkeypatch.setenv('AI_COVER_DELAY', '5')  # a cobertura pós-repasse tem teste próprio
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Rita Dona', 'email': 'rita@clinica.com', 'password': 'senha-forte-1', 'company': 'Clínica Bela'})
        assert c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30}).status_code == 200
        s = c.get('/api/ai/settings').json()
        assert s['default_mode'] == 'copilot' and s['key_configured']
        # copiloto (padrão): não responde sozinha
        signed(c, inbound('Oi, quanto é a limpeza de pele?', 'wamid.L1'))
        time.sleep(0.3)
        assert sent == []
        cid = c.get('/api/conversations').json()[0]['id']
        sug = c.post(f'/api/conversations/{cid}/suggest').json()
        assert 'R$ 180' in sug['reply'] and not sug['handoff']
        assert 'Clínica Bela' in prompts[-1][0] and prompts[-1][1][0]['role'] == 'user'
        # ensina e liga o automático
        r = c.put('/api/ai/settings', json={'knowledge': 'Limpeza de pele: R$ 180. Seg a sáb, 9h às 19h.', 'default_mode': 'auto', 'tone': 'descontraido'})
        assert r.status_code == 200 and r.json()['default_mode'] == 'auto'
        signed(c, inbound('E tem horário sábado?', 'wamid.L2'))
        assert wait(lambda: len(sent) == 1)
        assert 'Limpeza de pele: R$ 180' in prompts[-1][0]
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert msgs[-1]['dir'] == 'out' and msgs[-1]['by'] == 'Nanda (IA)'
        # várias mensagens seguidas: responde uma vez só
        signed(c, inbound('ah', 'wamid.L3')); signed(c, inbound('e o preço?', 'wamid.L4'))
        assert wait(lambda: len(sent) == 2); time.sleep(0.3); assert len(sent) == 2
        # pergunta fora do conhecimento -> passa para o time
        signed(c, inbound('posso fazer botox grávida?', 'wamid.L5'))
        assert wait(lambda: len(sent) == 3)
        conv = c.get('/api/conversations').json()[0]
        assert wait(lambda: c.get('/api/conversations').json()[0].get('needs_human'))
        conv = c.get('/api/conversations').json()[0]
        assert conv['ai_mode'] == 'off'
        assert any(m['dir'] == 'sys' for m in c.get(f'/api/conversations/{cid}').json()['messages'])
        # pausada: não responde na hora (dá tempo para o time; a cobertura do básico está em test_cobertura)
        signed(c, inbound('alô?', 'wamid.L6')); time.sleep(0.3); assert len(sent) == 3
        # time responde, volta para o automático, e o pedido de atendente vai direto
        assert c.post(f'/api/conversations/{cid}/send', json={'text': 'Oi Lu, aqui é a Rita!'}).status_code == 200
        assert c.get('/api/conversations').json()[0]['needs_human'] is False
        c.post(f'/api/conversations/{cid}/mode', json={'mode': 'auto'})
        signed(c, inbound('quero falar com um atendente', 'wamid.L7'))
        assert wait(lambda: len(sent) == 5) and 'chamar alguém do time' in sent[-1]
        assert wait(lambda: c.get('/api/conversations').json()[0].get('needs_human'))
        # time respondendo tira o automático
        c.post(f'/api/conversations/{cid}/mode', json={'mode': 'auto'})
        c.post(f'/api/conversations/{cid}/send', json={'text': 'Pode falar!'})
        assert c.get('/api/conversations').json()[0]['ai_mode'] == 'copilot'
        # teste no painel não envia nada
        t = c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'quanto custa?'}]}).json()
        assert 'R$ 180' in t['reply']
        u = c.get('/api/ai/settings').json()['usage']
        assert u['replies'] >= 2 and u['suggestions'] == 1 and u['tests'] == 1
        assert ai.parse('lixo sem json') == {'reply': 'lixo sem json', 'handoff': False, 'reason': '', 'book': None, 'origin': None, 'origin_detail': '', 'marcos': [], 'anotar': '', 'valor': None, 'valor_item': '', 'etapa': None}
