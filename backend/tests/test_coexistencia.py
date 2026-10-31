import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from main import app

def signed(c, field, value):
    raw = json.dumps({'entry': [{'changes': [{'field': field, 'value': value}]}]}).encode()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_entrar_com_a_meta_coexistencia(monkeypatch):
    calls = []
    async def fake_graph(method, path, token, payload=None, params=None):
        calls.append((method, path, token, payload, params))
        if path == '/oauth/access_token': return {'access_token': 'EAAbiz' + 'x' * 30}
        if 'subscribed_apps' in path or 'smb_app_data' in path or path.endswith('/register'): return {'success': True}
        if path.endswith('/messages'): return {'messages': [{'id': 'wout'}]}
        return {'display_phone_number': '+55 11 97575-4765', 'verified_name': 'Oi Nanda', 'quality_rating': 'GREEN'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setenv('META_APP_ID', '1164112056262465'); monkeypatch.setenv('META_ES_CONFIG_ID', '999')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Paulo Dono', 'email': 'p@oinanda.com.br', 'password': 'senha-forte-1', 'company': 'Oi Nanda'})
        assert c.get('/api/wa/signup-config').json() == {'app_id': '1164112056262465', 'config_id': '999', 'enabled': True}
        r = c.post('/api/wa/signup', json={'code': 'codigo-da-meta-123', 'waba_id': '77788899900', 'phone_number_id': '5511975754765', 'coexistence': True})
        assert r.status_code == 200, r.text
        n = r.json(); assert n['mode'] == 'coexistence' and n['display_phone'] == '+55 11 97575-4765'
        paths = [x[1] for x in calls]
        assert '/oauth/access_token' in paths and not any(p.endswith('/register') for p in paths)  # coexistência não registra o número
        assert [x[3]['sync_type'] for x in calls if 'smb_app_data' in x[1]] == ['smb_app_state_sync', 'history']
        assert calls[0][2] is None and calls[0][4]['client_secret'] == 'segredo'
        meta = {'display_phone_number': '5511975754765', 'phone_number_id': '5511975754765'}
        # contatos da agenda do celular
        signed(c, 'smb_app_state_sync', {'metadata': meta, 'state_sync': [{'type': 'contact', 'action': 'add', 'contact': {'full_name': 'Joana Lima', 'phone_number': '+55 11 98888-7777'}}]})
        # histórico de 6 meses
        old = int(time.time()) - 86400 * 10
        signed(c, 'history', {'metadata': meta, 'history': [{'metadata': {'phase': 0}, 'threads': [{'id': '5511988887777', 'messages': [
            {'from': '5511988887777', 'id': 'h1', 'timestamp': str(old), 'type': 'text', 'text': {'body': 'Oi, tem horário sábado?'}},
            {'from': '5511975754765', 'id': 'h2', 'timestamp': str(old + 60), 'type': 'text', 'text': {'body': 'Tem sim, 10h!'}}]}]}]})
        conv = c.get('/api/conversations').json()
        assert len(conv) == 1 and conv[0]['name'] == 'Joana Lima' and not conv[0]['window_open']
        msgs = c.get(f'/api/conversations/{conv[0]["id"]}').json()['messages']
        assert [(m['dir'], m['text']) for m in msgs] == [('in', 'Oi, tem horário sábado?'), ('out', 'Tem sim, 10h!')]
        # mensagem mandada pelo celular agora
        signed(c, 'smb_message_echoes', {'metadata': meta, 'message_echoes': [{'from': '5511975754765', 'to': '5511988887777', 'id': 'e1', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'Joana, confirmado!'}}]})
        msgs = c.get(f'/api/conversations/{conv[0]["id"]}').json()['messages']
        assert msgs[-1]['dir'] == 'out' and msgs[-1]['by'] == 'Celular'
        signed(c, 'smb_message_echoes', {'metadata': meta, 'message_echoes': [{'from': '5511975754765', 'to': '5511988887777', 'id': 'e1', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'Joana, confirmado!'}}]})
        assert len(c.get(f'/api/conversations/{conv[0]["id"]}').json()['messages']) == 3  # sem duplicar

def test_entrar_com_a_meta_numero_novo(monkeypatch):
    calls = []
    async def fake_graph(method, path, token, payload=None, params=None):
        calls.append((path, payload))
        if path == '/oauth/access_token': return {'access_token': 'EAAbiz' + 'y' * 30}
        return {'success': True, 'display_phone_number': '+55 11 90000-1111'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setenv('META_APP_ID', '1'); monkeypatch.setenv('META_ES_CONFIG_ID', '2')
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Ana Dona', 'email': 'ana@x.com', 'password': 'senha-forte-1', 'company': 'Studio X'})
        r = c.post('/api/wa/signup', json={'code': 'codigo-da-meta-456', 'waba_id': '12345678', 'phone_number_id': '87654321'})
        assert r.status_code == 200 and r.json()['mode'] == 'cloud'
        reg = [p for p in calls if p[0].endswith('/register')]
        assert reg and len(reg[0][1]['pin']) == 6
        assert not any('smb_app_data' in p[0] for p in calls)
