import os, json, hmac, hashlib, time
from datetime import date, timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai, gcal
from main import app

def next_monday():
    d = date.today() + timedelta(days=7)
    return d - timedelta(days=d.weekday())

def test_agenda(monkeypatch):
    g = []
    async def fake_http(method, url, token=None, data=None, json_body=None, params=None):
        g.append((method, url, json_body))
        if url.endswith('/token'): return {'access_token': 'ya29', 'expires_in': 3600, 'refresh_token': 'rt'}
        if url.endswith('/freeBusy'):
            d = next_monday()
            return {'calendars': {'primary': {'busy': [{'start': f'{d}T13:00:00Z', 'end': f'{d}T14:00:00Z'}]}}}  # 10h-11h em SP
        if url.endswith('/events'): return {'id': 'gev1'}
        return {}
    monkeypatch.setattr(gcal, 'http', fake_http)
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'cid'); monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'sec')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Bia Dona', 'email': 'bia@cl.com', 'password': 'senha-forte-1', 'company': 'Clínica Bia', 'segment': 'clinica'})
        s = c.get('/api/agenda/settings').json()
        sv = s['services'][0]
        assert sv['name'] == 'Avaliação' and sv['duration'] == 30 and s['hours']['6'] == [] and not s['google']['connected']
        mon = next_monday()
        slots = c.get('/api/agenda/slots', params={'service_id': sv['id'], 'date': str(mon)}).json()
        assert slots[0] == '09:00' and slots[-1] == '17:30' and len(slots) == 18
        assert c.get('/api/agenda/slots', params={'service_id': sv['id'], 'date': str(mon + timedelta(days=6))}).json() == []  # domingo fechado
        # contato + etapa ao agendar
        cid = c.post('/api/contacts', json={'name': 'Lara', 'phone': '11955554444'}).json()['id']
        stages = c.get('/api/funnel/stages').json()
        marcada = next(x for x in stages if 'marcada' in x['name'])
        c.put('/api/agenda/settings', json={'stage_on_book': marcada['id'], 'services': [{**sv, 'price': 0}]})
        r = c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(mon), 'time': '09:00'})
        assert r.status_code == 200, r.text
        appt = r.json(); assert appt['start'].startswith(f'{mon}T09:00') and appt['status'] == 'scheduled'
        assert c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(mon), 'time': '09:00'}).status_code == 409
        assert '09:00' not in c.get('/api/agenda/slots', params={'service_id': sv['id'], 'date': str(mon)}).json()
        assert c.get('/api/funnel').json()['cards'][0]['stage'] == marcada['id']
        # encaixe do dono fora do horário
        assert c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(mon), 'time': '20:00', 'force': True}).status_code == 200
        # remarcar e cancelar
        p = c.patch(f'/api/appointments/{appt["id"]}', json={'date': str(mon), 'time': '15:00'})
        assert p.status_code == 200 and 'T15:00' in p.json()['start']
        assert '09:00' in c.get('/api/agenda/slots', params={'service_id': sv['id'], 'date': str(mon)}).json()
        assert c.patch(f'/api/appointments/{appt["id"]}', json={'status': 'canceled'}).json()['status_label'] == 'Cancelado'
        lst = c.get('/api/appointments', params={'start': str(mon), 'end': str(mon + timedelta(days=6))}).json()
        assert len(lst) == 2
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert any('agendou Avaliação' in m['text'] for m in msgs) and any('remarcou' in m['text'] for m in msgs)
        # capacidade (studio-like): 2 vagas no mesmo horário
        c.put('/api/agenda/settings', json={'services': [{**sv, 'capacity': 2}]})
        cid2 = c.post('/api/contacts', json={'name': 'Mia', 'phone': '11944443333'}).json()['id']
        cid3 = c.post('/api/contacts', json={'name': 'Nina', 'phone': '11933332222'}).json()['id']
        assert c.post('/api/appointments', json={'contact_id': cid2, 'service_id': sv['id'], 'date': str(mon), 'time': '11:00'}).status_code == 200
        assert c.post('/api/appointments', json={'contact_id': cid3, 'service_id': sv['id'], 'date': str(mon), 'time': '11:00'}).status_code == 200
        assert c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(mon), 'time': '11:00'}).status_code == 409
        # Google: conectar, bloquear ocupado e criar evento
        url = c.post('/api/google/connect').json()['url']
        state = url.split('state=')[1].split('&')[0]
        cb = c.get('/api/google/callback', params={'state': state, 'code': 'abc'}, follow_redirects=False)
        assert cb.status_code in (302, 307) and cb.headers['location'].endswith('google=ok')
        assert c.get('/api/google/status').json()['connected']
        free = c.get('/api/agenda/slots', params={'service_id': sv['id'], 'date': str(mon)}).json()
        assert '10:00' not in free and '10:30' not in free and '09:30' in free
        c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(mon), 'time': '16:00'})
        assert any(m == 'POST' and u.endswith('/events') for m, u, _ in g)
        assert c.get('/api/google/callback', params={'state': state, 'code': 'abc'}, follow_redirects=False).headers['location'].endswith('google=expirado')

def test_nanda_agenda(monkeypatch):
    sent = []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload['text']['body']); return {'messages': [{'id': f'w{len(sent)}'}]}
        if 'subscribed_apps' in path: return {'success': True}
        return {'display_phone_number': '+55', 'verified_name': 'X'}
    seen = {}
    async def fake_llm(system, messages, max_tokens=500):
        seen['system'] = system
        mon = next_monday()
        svc = system.split('(id ')[1].split(',')[0]
        return json.dumps({'reply': 'Fechado! Avaliação na segunda às 09:00.', 'handoff': False, 'book': {'service_id': svc, 'start': f'{mon}T09:00'}}), {}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(ai, 'llm', fake_llm)
    monkeypatch.setenv('AI_DEBOUNCE', '0.05'); monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Bia Dona', 'email': 'bia2@cl.com', 'password': 'senha-forte-1', 'company': 'Clínica Dois', 'segment': 'clinica'})
        c.post('/api/wa/connect', json={'phone_number_id': '4440001112', 'waba_id': '5556667779', 'token': 'EAA' + 'q' * 30})
        c.put('/api/ai/settings', json={'default_mode': 'auto'})
        body = {'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '4440001112'}, 'contacts': [{'wa_id': '5511922221111', 'profile': {'name': 'Rosa'}}],
            'messages': [{'from': '5511922221111', 'id': 'wq1', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'quero marcar uma avaliação segunda cedo'}}]}}]}]}
        raw = json.dumps(body).encode()
        c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})
        end = time.time() + 3
        while time.time() < end and not sent: time.sleep(0.05)
        assert sent and 'segunda às 09:00' in sent[0]
        assert 'AGENDA' in seen['system'] and '09:00' in seen['system']
        mon = next_monday()
        lst = c.get('/api/appointments', params={'start': str(mon), 'end': str(mon)}).json()
        assert len(lst) == 1 and lst[0]['by'] == 'Nanda (IA)'
        t = c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'segunda 9h'}]}).json()
        assert t['book'] and 'às 09:00' in t['book_label']
        assert len(c.get('/api/appointments', params={'start': str(mon), 'end': str(mon)}).json()) == 1  # teste não agenda
