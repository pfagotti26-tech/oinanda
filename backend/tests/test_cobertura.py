"""Depois do repasse a Nanda cobre o básico, e volta de verdade quando o time resolve.
Agenda: a Nanda enxerga todos os horários livres, em faixas."""
import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  AI_DEBOUNCE='0.05', AI_COVER_DELAY='0.2', ANTHROPIC_API_KEY='teste')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import datetime
from fastapi.testclient import TestClient
import whatsapp, ai, agenda
from main import app

sent, prompts = [], []
async def fake_graph(method, path, token, payload=None, params=None):
    if path.endswith('/messages'):
        sent.append(payload['text']['body']); return {'messages': [{'id': f'wamid.C{len(sent)}'}]}
    if 'subscribed_apps' in path: return {'success': True}
    return {'display_phone_number': '+55 11 95555-0000', 'verified_name': 'Escola'}

async def fake_llm(system, messages, max_tokens=500):
    prompts.append(system)
    last = messages[-1]['content']
    if 'desconto' in last: return json.dumps({'reply': 'Vou chamar o time para ver o desconto!', 'handoff': True, 'reason': 'desconto fora da tabela'}), {}
    if 'ESTA CONVERSA JÁ ESTÁ COM O TIME' in system:
        if 'endereço' in last: return json.dumps({'reply': 'Fica na Rua X, 100. O time continua com você amanhã às 7h.', 'handoff': False}), {}
        return json.dumps({'reply': '', 'handoff': False}), {}
    return json.dumps({'reply': 'Oi! Posso ajudar.', 'handoff': False}), {}

def signed(c, body):
    raw = json.dumps(body).encode()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest(), 'content-type': 'application/json'})

def inbound(text, mid):
    return {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770001112'},
        'contacts': [{'wa_id': '5511977776666', 'profile': {'name': 'Rafael'}}],
        'messages': [{'from': '5511977776666', 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}]}}]}]}

def wait(cond, t=3):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False

def test_cobre_depois_do_repasse_e_volta(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona', 'email': 'd@escola.com', 'password': 'senha-forte-1', 'company': 'Escola', 'segment': 'escola'})
        assert c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30}).status_code == 200
        c.put('/api/ai/settings', json={'default_mode': 'auto'})
        signed(c, inbound('tem desconto de 20%?', 'w1'))
        assert wait(lambda: len(sent) == 1)
        cid = c.get('/api/conversations').json()[0]['id']
        assert wait(lambda: c.get('/api/conversations').json()[0].get('needs_human'))
        conv = c.get(f'/api/conversations/{cid}').json()['contact']
        assert conv['ai_mode'] == 'off' and conv['ai_paused'] == 'handoff'
        # pergunta simples com a conversa no time: a Nanda cobre, sem repassar de novo
        signed(c, inbound('ok. qual o endereço?', 'w2'))
        assert wait(lambda: len(sent) == 2)
        assert 'Rua X' in sent[-1] and 'ESTA CONVERSA JÁ ESTÁ COM O TIME' in prompts[-1] and 'desconto fora da tabela' in prompts[-1]
        conv = c.get(f'/api/conversations/{cid}').json()['contact']
        assert conv['needs_human'] and conv['ai_mode'] == 'off'  # continua com o time
        # "obrigado": a Nanda fica quieta
        signed(c, inbound('obrigado', 'w3')); time.sleep(0.6); assert len(sent) == 2
        # se uma pessoa já respondeu, a Nanda não cobre
        assert c.post(f'/api/conversations/{cid}/send', json={'text': 'Oi Rafael, aqui é a Dona.'}).status_code == 200
        assert c.get(f'/api/conversations/{cid}').json()['contact']['ai_mode'] == 'off'
        # resolvido pelo time: a Nanda volta como era (automática, padrão da empresa)
        assert c.post(f'/api/conversations/{cid}/resolve', json={'resolved': True}).status_code == 200
        conv = c.get(f'/api/conversations/{cid}').json()['contact']
        assert conv['ai_mode'] is None and not conv.get('ai_paused') and not conv['needs_human']
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert any('devolveu a conversa para a Nanda' in (m.get('text') or '') for m in msgs)
        n = len(sent)
        signed(c, inbound('oi de novo, e a matrícula?', 'w4'))
        assert wait(lambda: len(sent) == n + 1) and 'Posso ajudar' in sent[-1]

def test_resume_route(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona', 'email': 'd2@escola.com', 'password': 'senha-forte-1', 'company': 'Escola 2', 'segment': 'escola'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30})
        c.put('/api/ai/settings', json={'default_mode': 'auto'})
        signed(c, inbound('tem desconto?', 'r1'))
        assert wait(lambda: c.get('/api/conversations').json() and c.get('/api/conversations').json()[0].get('needs_human'))
        cid = c.get('/api/conversations').json()[0]['id']
        assert c.post(f'/api/conversations/{cid}/resume').json()['ok']
        conv = c.get(f'/api/conversations/{cid}').json()['contact']
        assert conv['ai_mode'] is None and not conv['needs_human']
        # pausada à mão pelo time (não foi repasse): religar também funciona
        c.post(f'/api/conversations/{cid}/mode', json={'mode': 'off'})
        c.post(f'/api/conversations/{cid}/resume')
        assert c.get(f'/api/conversations/{cid}').json()['contact']['ai_mode'] is None

def test_faixas_de_horario():
    H = lambda h, m=0: datetime(2026, 11, 13, h, m)
    assert agenda.ranges_text([H(9), H(9, 30), H(10), H(10, 30), H(12, 30), H(15), H(15, 30), H(16), H(16, 30), H(17), H(17, 30), H(18), H(18, 30)], 30) == '09:00 às 10:30, 12:30, 15:00 às 18:30'
    assert agenda.ranges_text([H(9), H(9, 30)], 30) == '09:00 ou 09:30'
    assert agenda.ranges_text([], 30) == 'nenhum'

async def fake_llm_fatos(system, messages, max_tokens=500):
    prompts.append(system)
    return json.dumps({'reply': 'Legal! O apto de 68 m² sai R$ 690 mil.', 'handoff': False, 'anotar': 'Compra para morar, financia Caixa, entrada R$ 200 mil + FGTS', 'valor': 690000, 'valor_item': 'Apto 2q Campeche'}), {}

def test_anotacoes_e_valor_previsto(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm_fatos)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona', 'email': 'd3@imob.com', 'password': 'senha-forte-1', 'company': 'Imob', 'segment': 'imobiliaria'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30})
        c.put('/api/ai/settings', json={'default_mode': 'auto'})
        n = len(sent)
        signed(c, inbound('vi o apto de 2 quartos no Campeche, financio pela Caixa com 200 de entrada', 'f1'))
        assert wait(lambda: len(sent) == n + 1)
        cid = c.get('/api/conversations').json()[0]['id']
        assert wait(lambda: (c.get(f'/api/conversations/{cid}').json()['contact'].get('value') or 0) == 690000)
        ct = c.get(f'/api/conversations/{cid}').json()['contact']
        assert 'entrada R$ 200 mil + FGTS' in ct['notes'] and '(Nanda, ' in ct['notes']
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert any('estimou o valor previsto: R$ 690.000' in (m.get('text') or '') for m in msgs)
        # o que já está anotado entra no prompt e não é anotado de novo; valor não é sobrescrito
        c.patch(f'/api/contacts/{cid}', json={'value': 650000})
        signed(c, inbound('e tem vaga de garagem?', 'f2'))
        assert wait(lambda: len(sent) == n + 2); time.sleep(0.3)
        assert 'entrada R$ 200 mil + FGTS' in prompts[-1] and 'ANOTAÇÕES DO CLIENTE' in prompts[-1]
        ct = c.get(f'/api/conversations/{cid}').json()['contact']
        assert ct['value'] == 650000 and ct['notes'].count('entrada R$ 200 mil') == 1

def test_etapa_sugerida_e_catalogo_sem_servico_sem_preco(monkeypatch):
    stages_seen = {}
    async def llm_etapa(system, messages, max_tokens=500):
        prompts.append(system)
        import re
        m = re.search(r'ETAPAS DO FUNIL, nesta ordem: (.*?)\. O cliente', system)
        ids = re.findall(r'(\w{6,}) \(', m.group(1)) if m else []
        stages_seen['ids'] = ids
        last = messages[-1]['content']
        if 'link' in last: return json.dumps({'reply': 'Aqui está o link!', 'handoff': False, 'etapa': ids[2]}), {}
        if 'voltar' in last: return json.dumps({'reply': 'Ok', 'handoff': False, 'etapa': ids[0]}), {}
        return json.dumps({'reply': 'Oi!', 'handoff': False, 'etapa': 'won'}), {}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', llm_etapa)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona', 'email': 'd4@info.com', 'password': 'senha-forte-1', 'company': 'Curso', 'segment': 'lancamento'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30})
        c.put('/api/ai/settings', json={'default_mode': 'auto', 'offer': 'Conversa de apresentação grátis.'})
        c.get('/api/products')  # sincroniza o serviço da agenda no catálogo
        n = len(sent)
        signed(c, inbound('oi, quero saber do curso', 'e1'))
        assert wait(lambda: len(sent) == n + 1)
        assert 'preço sob consulta' not in prompts[-1] and 'ETAPAS DO FUNIL' in prompts[-1] and 'Novo lead' in prompts[-1]
        cid = c.get('/api/conversations').json()[0]['id']
        time.sleep(0.3); assert c.get(f'/api/conversations/{cid}').json()['contact'].get('stage') in (None, stages_seen['ids'][0])  # "won" ignorado
        signed(c, inbound('manda o link', 'e2'))
        assert wait(lambda: len(sent) == n + 2)
        assert wait(lambda: c.get(f'/api/conversations/{cid}').json()['contact'].get('stage') == stages_seen['ids'][2])
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert any('Nanda (IA) moveu para Link enviado' in (m.get('text') or '') for m in msgs)
        signed(c, inbound('quero voltar', 'e3'))  # para trás: não move
        assert wait(lambda: len(sent) == n + 3); time.sleep(0.3)
        assert c.get(f'/api/conversations/{cid}').json()['contact']['stage'] == stages_seen['ids'][2]
