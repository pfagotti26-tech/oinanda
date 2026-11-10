import os, json, hmac, hashlib, time, asyncio
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  IG_APP_ID='111', IG_HUMAN_AGENT='1', IG_APP_SECRET='igsecreto', IG_CONFIG_ID='777', IG_VERIFY_TOKEN='igverif', APP_ORIGINS='https://app.test')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, instagram, media
from core import db, now
from main import app

def post(c, body):
    raw = json.dumps(body).encode()
    return c.post('/api/ig/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'igsecreto', raw, hashlib.sha256).hexdigest()})

def dm(sender, text=None, mid=None, echo=False, att=None, ig_id='17841400000000001'):
    m = {'mid': mid or 'm' + str(time.time_ns())}
    if text: m['text'] = text
    if echo: m['is_echo'] = True
    if att: m['attachments'] = [att]
    s, r = ({'id': ig_id}, {'id': sender}) if echo else ({'id': sender}, {'id': ig_id})
    return {'object': 'instagram', 'entry': [{'id': ig_id, 'time': 1, 'messaging': [{'sender': s, 'recipient': r, 'timestamp': int(time.time() * 1000), 'message': m}]}]}

def test_direct(monkeypatch):
    calls, inbound = [], []
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        calls.append((method, path, params, payload))
        if path == '/oauth/access_token': return {'access_token': 'suat', 'token_type': 'bearer'}
        if path == '/me': return {'client_business_id': '555', 'id': '99'}
        if path == '/me/assigned_instagram_accounts': return {'data': [{'id': '17841400000000001', 'username': 'alta_creators', 'name': 'Alta Creators'}]}
        if path.endswith('/messages'): return {'recipient_id': 'x', 'message_id': 'out' + str(len(calls))}
        if path == '/17841400000000001': return {'name': 'Alta Creators', 'profile_picture_url': 'x'}
        if path.startswith('/9'): return {'name': 'Bia Souza', 'username': 'bia.souza'}
        return {'success': True}
    monkeypatch.setattr(instagram, 'ig', fake_ig)
    monkeypatch.setattr(whatsapp, 'ON_INBOUND', lambda cid, kid, mid: inbound.append(mid))
    async def fake_dl(url): return b'JPEGDATA', 'image/jpeg'
    monkeypatch.setattr(media, 'download_url', fake_dl)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona I', 'email': 'di@x.com', 'password': 'senha-forte-1', 'company': 'Studio I', 'segment': 'studio'})
        assert c.get('/api/ig/status').json()['ready'] is True
        url = c.get('/api/ig/connect').json()['url']
        assert url.startswith('https://www.facebook.com/') and '/dialog/oauth?' in url and 'config_id=777' in url
        state = url.split('state=')[1].split('&')[0]
        r = c.get(f'/api/ig/callback?code=abc%23_&state={state}', follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers['location'].endswith('/empresa?ig=ok')
        assert c.get('/api/ig/status').json()['account']['username'] == 'alta_creators'
        assert any(p == '/17841400000000001/subscribed_apps' for _, p, _, _ in calls)
        assert c.get(f'/api/ig/callback?code=x&state={state}', follow_redirects=False).headers['location'].endswith('ig=expirado')  # estado só vale uma vez
        # assinatura errada é recusada
        assert c.post('/api/ig/webhook', content=b'{}', headers={'x-hub-signature-256': 'sha256=errado'}).status_code == 403
        assert c.get('/api/ig/webhook?hub.mode=subscribe&hub.verify_token=igverif&hub.challenge=42').text == '42'
        # mensagem no Direct vira conversa, com origem Instagram
        post(c, dm('901', 'Oi! Quanto custa a aula?'))
        conv = c.get('/api/conversations').json()
        bia = next(x for x in conv if x['name'] == 'Bia Souza')
        assert bia['channel'] == 'instagram' and bia['ig_username'] == 'bia.souza' and bia['origin'] == 'instagram' and bia['waiting']
        assert len(inbound) == 1
        post(c, dm('902', 'oi'))  # dois contatos sem WhatsApp convivem
        assert len([x for x in c.get('/api/conversations').json() if x.get('channel') == 'instagram']) == 2
        # resposta do time
        r = c.post(f"/api/conversations/{bia['id']}/send", json={'text': 'Oi Bia! R$ 289 por mês.'})
        assert r.status_code == 200, r.text
        sent = [p for m, pth, _, p in calls if pth.endswith('/messages')][-1]
        assert sent == {'recipient': {'id': '901'}, 'message': {'text': 'Oi Bia! R$ 289 por mês.'}}
        # mensagem mandada pelo app do Instagram (eco) aparece e não duplica a nossa
        post(c, dm('901', 'mandei pelo celular', echo=True))
        msgs = c.get(f"/api/conversations/{bia['id']}").json()['messages']
        assert msgs[-1]['by'] == 'Instagram' and msgs[-1]['dir'] == 'out'
        # foto no Direct é baixada
        post(c, dm('901', att={'type': 'image', 'payload': {'url': 'https://cdn.ig/x.jpg'}}))
        c.portal.call(asyncio.sleep, 0.2)
        ph = c.get(f"/api/conversations/{bia['id']}").json()['messages'][-1]
        assert ph['type'] == 'image' and c.get(f"/api/media/{ph['id']}").content == b'JPEGDATA'
        # depois de 24 h: só pessoa do time, com a marcação de atendimento humano
        async def velho(): await db.contacts.update_one({'id': bia['id']}, {'$set': {'last_in_at': now() - timedelta(days=2)}})
        c.portal.call(velho)
        cv = c.get(f"/api/conversations/{bia['id']}").json()['contact']
        assert cv['window_open'] and cv['human_only']
        c.post(f"/api/conversations/{bia['id']}/send", json={'text': 'Conseguiu ver?'})
        sent = [p for m, pth, _, p in calls if pth.endswith('/messages')][-1]
        assert sent['tag'] == 'HUMAN_AGENT' and sent['messaging_type'] == 'MESSAGE_TAG'
        fresh = c.portal.call(lambda: db.contacts.find_one({'id': bia['id']}, {'_id': 0}))
        try:
            c.portal.call(lambda: whatsapp.deliver(fresh['company_id'], fresh, 'oi', None, 'Nanda (IA)'))
            assert False
        except Exception as e: assert '24 h' in str(e.detail)
        # enviar foto: vai por link assinado
        r = c.post(f"/api/conversations/{bia['id']}/media", files={'file': ('f.jpg', b'\xff\xd8x', 'image/jpeg')})
        assert r.status_code == 200, r.text
        att = [p for m, pth, _, p in calls if pth.endswith('/messages')][-1]['message']['attachment']
        link = att['payload']['url']
        assert att['type'] == 'image' and link.startswith('https://app.test/api/media/pub/')
        assert c.get(link.replace('https://app.test', '')).content == b'\xff\xd8x'
        assert c.get(link.replace('https://app.test', '').replace('&s=', '&s=0')).status_code == 404
        # comente QUERO: manda o Direct uma vez por comentário
        assert c.put('/api/ig/settings', json={'comment_words': ['QUERO'], 'comment_dm': 'Oi {primeiro_nome}! Te mando os detalhes aqui.', 'comment_reply': 'Te chamei no Direct!'}).status_code == 200
        cm = {'object': 'instagram', 'entry': [{'id': '17841400000000001', 'time': 1, 'changes': [{'field': 'comments', 'value': {'from': {'id': '903', 'username': 'carla'}, 'id': 'c1', 'text': 'Quero!', 'media': {'id': 'p1'}}}]}]}
        post(c, cm); post(c, cm)
        priv = [p for m, pth, _, p in calls if pth.endswith('/messages') and p and 'comment_id' in p['recipient']]
        assert len(priv) == 1 and priv[0]['recipient'] == {'comment_id': 'c1'} and priv[0]['message']['text'].startswith('Oi ')
        assert any(pth == '/c1/replies' for _, pth, _, _ in calls)
        cm2 = json.loads(json.dumps(cm)); cm2['entry'][0]['changes'][0]['value'].update(id='c2', text='que lindo')
        post(c, cm2)
        assert len([p for m, pth, _, p in calls if pth.endswith('/messages') and p and 'comment_id' in p['recipient']]) == 1

def test_meta_demora_a_liberar_a_conta(monkeypatch):
    """A Meta autoriza, mas só libera a conta depois: o Oi Nanda guarda e conecta sozinho na próxima tentativa."""
    state = {'liberada': False}
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        if path == '/oauth/access_token': return {'access_token': 'suat2'}
        if path == '/me': return {'client_business_id': '777'}
        if path == '/me/assigned_instagram_accounts': return {'data': [{'id': '17841400000000077', 'username': 'cliente.lenta'}] if state['liberada'] else []}
        if path == '/debug_token': return {'data': {'type': 'SYSTEM_USER', 'granular_scopes': [{'scope': 'instagram_basic'}]}}
        if path.startswith('/777/'): raise instagram.HTTPException(400, 'O Instagram recusou: (#200) Requires business_management')
        return {'success': True}
    monkeypatch.setattr(instagram, 'ig', fake_ig)
    async def no_retry(cid): return None
    monkeypatch.setattr(instagram, 'retry_pending', no_retry)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona L', 'email': 'dl@x.com', 'password': 'senha-forte-1', 'company': 'Studio L', 'segment': 'studio'})
        url = c.get('/api/ig/connect').json()['url']
        state_q = url.split('state=')[1].split('&')[0]
        r = c.get(f'/api/ig/callback?code=abc&state={state_q}', follow_redirects=False)
        assert r.headers['location'].endswith('/empresa?ig=aguardando')
        s = c.get('/api/ig/status').json()
        assert s['account'] is None and s['pending'] == {'gave_up': False, 'reason': None}
        state['liberada'] = True
        c.portal.call(instagram.refresh_tokens)
        s = c.get('/api/ig/status').json()
        assert s['account']['username'] == 'cliente.lenta' and s['pending'] is None

def test_conexao_pela_pagina_do_facebook(monkeypatch):
    calls = []
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        calls.append((method, path, token, payload))
        if path == '/oauth/access_token' and (params or {}).get('grant_type') == 'fb_exchange_token': return {'access_token': 'user-longo'}
        if path == '/oauth/access_token': return {'access_token': 'user-curto'}
        if path == '/me/accounts': return {'data': [{'id': 'pg1', 'name': 'Página sem IG', 'access_token': 'x'},
                                                    {'id': 'pg2', 'name': 'Studio P', 'access_token': 'page-tok', 'instagram_business_account': {'id': '17841400000000088', 'username': 'studio.p'}}]}
        if path.endswith('/messages'): return {'message_id': 'mid-out'}
        return {'success': True}
    monkeypatch.setattr(instagram, 'ig', fake_ig)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona P', 'email': 'dp@x.com', 'password': 'senha-forte-1', 'company': 'Studio P', 'segment': 'studio'})
        url = c.get('/api/ig/connect').json()['url']
        st = url.split('state=')[1].split('&')[0]
        r = c.get(f'/api/ig/callback?code=abc&state={st}', follow_redirects=False)
        assert r.headers['location'].endswith('/empresa?ig=ok')
        assert c.get('/api/ig/status').json()['account']['username'] == 'studio.p'
        assert any(p == '/pg2/subscribed_apps' and t == 'page-tok' for _, p, t, _ in calls)
        assert not any(p == '/me' for _, p, _, _ in calls)  # não caiu no caminho do login unificado
        # mensagem chegando e resposta saindo pela Página
        post(c, dm('905', 'Oi!', ig_id='17841400000000088'))
        conv = next(x for x in c.get('/api/conversations').json() if x.get('ig_username') or x.get('channel') == 'instagram')
        assert c.post(f'/api/conversations/{conv["id"]}/send', json={'text': 'Oi, tudo bem?'}).status_code == 200
        assert any(p == '/me/messages' and t == 'page-tok' for _, p, t, _ in calls)
