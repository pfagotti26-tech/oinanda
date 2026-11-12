"""Agenda por profissional: horário próprio, serviços que faz, escolha automática de quem está livre, Nanda marca com quem o cliente pedir."""
import os, json, hmac, hashlib, time, datetime as dt
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif', AI_DEBOUNCE='0.05', ANTHROPIC_API_KEY='teste')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai
from main import app

sent = []
async def fake_graph(method, path, token, payload=None, params=None):
    if path.endswith('/messages'): sent.append(payload['text']['body']); return {'messages': [{'id': f'wamid.P{len(sent)}'}]}
    if 'subscribed_apps' in path: return {'success': True}
    return {'display_phone_number': '+55 11 95555-0000', 'verified_name': 'Barbearia'}

def signed(c, body):
    raw = json.dumps(body).encode()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest(), 'content-type': 'application/json'})

def inbound(text, mid):
    return {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770001112'}, 'contacts': [{'wa_id': '5511977776666', 'profile': {'name': 'Carlos'}}],
        'messages': [{'from': '5511977776666', 'id': mid, 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}]}}]}]}

def wait(cond, t=3):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False

def next_weekday(wd):
    d = dt.date.today() + dt.timedelta(days=2)
    while d.weekday() != wd: d += dt.timedelta(days=1)
    return d

def test_profissionais(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Pedro', 'email': 'p@barba.com', 'password': 'senha-forte-1', 'company': 'Barbearia', 'segment': 'salao'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30})
        wk = {str(i): [['09:00', '18:00']] for i in range(7)}
        r = c.put('/api/agenda/settings', json={'hours': wk, 'min_notice_hours': 0, 'services': [{'name': 'Corte', 'duration': 30, 'capacity': 1}, {'name': 'Barba', 'duration': 30, 'capacity': 1}]}); assert r.status_code == 200
        corte, barba = [x['id'] for x in r.json()['services']]
        # Léo faz tudo e trabalha só de tarde na terça; Thiago só corta
        ter = next_weekday(1)
        r = c.put('/api/agenda/settings', json={'pros': [{'name': 'Léo', 'services': [], 'hours': {**wk, '1': [['14:00', '18:00']]}}, {'name': 'Thiago', 'services': [corte]}]})
        assert r.status_code == 200 and len(r.json()['pros']) == 2 and r.json()['pros'][0]['color']
        leo, thiago = [p['id'] for p in r.json()['pros']]
        # barba na terça de manhã: só o Léo faz barba e ele não trabalha de manhã → sem horário
        assert '09:00' not in c.get('/api/agenda/slots', params={'service_id': barba, 'date': str(ter)}).json()
        assert '14:00' in c.get('/api/agenda/slots', params={'service_id': barba, 'date': str(ter)}).json()
        # corte de manhã tem (Thiago); pedindo o Léo, não
        assert '09:00' in c.get('/api/agenda/slots', params={'service_id': corte, 'date': str(ter)}).json()
        assert '09:00' not in c.get('/api/agenda/slots', params={'service_id': corte, 'date': str(ter), 'pro_id': leo}).json()
        # agenda sem pedir: cai no Thiago (único livre)
        cid = c.post('/api/contacts', json={'name': 'Carlos', 'phone': '5511977776666'}).json()['id']
        a1 = c.post('/api/appointments', json={'contact_id': cid, 'service_id': corte, 'date': str(ter), 'time': '09:00'}); assert a1.status_code == 200, a1.text
        assert a1.json()['pro_name'] == 'Thiago'
        # mesmo horário de novo: Thiago ocupado, Léo de folga → 409
        cid2 = c.post('/api/contacts', json={'name': 'Bruno', 'phone': '5511977776667'}).json()['id']
        assert c.post('/api/appointments', json={'contact_id': cid2, 'service_id': corte, 'date': str(ter), 'time': '09:00'}).status_code == 409
        # à tarde os dois estão livres: distribui para quem tem menos
        a2 = c.post('/api/appointments', json={'contact_id': cid2, 'service_id': corte, 'date': str(ter), 'time': '15:00'}).json(); assert a2['pro_name'] == 'Léo'
        a3 = c.post('/api/appointments', json={'contact_id': cid, 'service_id': corte, 'date': str(ter), 'time': '15:00'}).json(); assert a3['pro_name'] == 'Thiago'
        # pedir o Léo num horário em que ele está ocupado
        r = c.post('/api/appointments', json={'contact_id': cid, 'service_id': barba, 'date': str(ter), 'time': '15:00', 'pro_id': leo}); assert r.status_code == 409 and 'Léo' in r.json()['detail']
        # trocar o profissional
        r = c.patch('/api/appointments/' + a3['id'], json={'pro_id': leo}); assert r.status_code == 200 and r.json()['pro_name'] == 'Léo'  # gestor pode mesmo ocupado
        assert any('passou Corte para Léo' in (m.get('text') or '') for m in c.get(f'/api/conversations/{cid}').json()['messages'])
        # variável {profissional} nos lembretes
        from automations import VARS
        assert 'profissional' in VARS

async def fake_llm(system, messages, max_tokens=500):
    last = messages[-1]['content']
    import re
    m = re.search(r'Léo \(pro_id (\w+)', system); leo = m.group(1) if m else None
    sv = re.search(r'Corte \(id (\w+)', system).group(1)
    d = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    if 'Léo' in last and leo: return json.dumps({'reply': 'Marcado com o Léo!', 'handoff': False, 'book': {'service_id': sv, 'start': f'{d}T10:00', 'pro_id': leo}}), {}
    return json.dumps({'reply': 'Marcado!', 'handoff': False, 'book': {'service_id': sv, 'start': f'{d}T11:00'}}), {}

def test_nanda_marca_com_profissional(monkeypatch):
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Pedro', 'email': 'p2@barba.com', 'password': 'senha-forte-1', 'company': 'Barbearia 2', 'segment': 'salao'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770001112', 'waba_id': '5556667778', 'token': 'EAA' + 'z' * 30})
        wk = {str(i): [['09:00', '18:00']] for i in range(7)}
        r = c.put('/api/agenda/settings', json={'hours': wk, 'min_notice_hours': 0, 'services': [{'name': 'Corte', 'duration': 30, 'capacity': 1}], 'pros': [{'name': 'Léo'}, {'name': 'Thiago'}]})
        c.put('/api/ai/settings', json={'default_mode': 'auto'})
        n = len(sent)
        signed(c, inbound('quero cortar amanhã às 10h com o Léo', 'n1'))
        assert wait(lambda: len(sent) == n + 1)
        cid = c.get('/api/conversations').json()[0]['id']
        ap = c.get(f'/api/contacts/{cid}/appointments').json()
        assert wait(lambda: len(c.get(f'/api/contacts/{cid}/appointments').json()) == 1)
        ap = c.get(f'/api/contacts/{cid}/appointments').json()
        assert ap[0]['pro_name'] == 'Léo' and sent[-1] == 'Marcado com o Léo!'
        # sem pedir ninguém: o sistema escolhe e a resposta ganha o nome
        signed(c, inbound('e meu irmão às 11h', 'n2'))
        assert wait(lambda: len(sent) == n + 2)
        assert 'Seu horário fica com ' in sent[-1]
