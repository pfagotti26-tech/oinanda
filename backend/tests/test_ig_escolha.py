"""Quem administra várias páginas escolhe qual Instagram é desta empresa (antes ia o primeiro da lista)."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  IG_APP_ID='111', IG_HUMAN_AGENT='1', IG_APP_SECRET='igsecreto', IG_CONFIG_ID='777', IG_VERIFY_TOKEN='igverif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import instagram
from main import app

PAGES = [{'id': 'pg1', 'name': 'Alta Creators', 'access_token': 'tok-alta', 'instagram_business_account': {'id': '111', 'username': 'alta_creators'}},
         {'id': 'pg2', 'name': 'Oi Nanda', 'access_token': 'tok-nanda', 'instagram_business_account': {'id': '222', 'username': 'oinanda.app'}}]

def test_escolhe_entre_paginas(monkeypatch):
    calls = []
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        calls.append((method, path, token))
        if path == '/oauth/access_token': return {'access_token': 'user-tok'}
        if path == '/me/accounts': return {'data': PAGES}
        return {'success': True}
    monkeypatch.setattr(instagram, 'ig', fake_ig)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Paulo', 'email': 'p@on.com', 'password': 'senha-forte-1', 'company': 'Oi Nanda', 'segment': 'outro'})
        cid = c.get('/api/auth/me').json()['company']['id']
        assert c.portal.call(instagram.via_page, cid, 'u1', 'tok') == 'escolher'
        assert c.get('/api/ig/status').json()['account'] is None  # nada ligado antes de escolher
        ch = c.get('/api/ig/choices').json()
        assert [x['username'] for x in ch] == ['alta_creators', 'oinanda.app'] and 'token_enc' not in str(ch)
        assert c.post('/api/ig/choose', json={'id': '999'}).status_code == 400
        assert c.post('/api/ig/choose', json={'id': '222'}).json() == {'ok': True}
        a = c.get('/api/ig/status').json()['account']; assert a['username'] == 'oinanda.app'
        assert ('POST', '/pg2/subscribed_apps', 'tok-nanda') in calls
        assert c.get('/api/ig/choices').status_code == 404  # escolha usada some
        # uma página só: liga direto, como antes
        PAGES.pop(0)
        assert c.portal.call(instagram.via_page, cid, 'u1', 'tok') == 'ok'
