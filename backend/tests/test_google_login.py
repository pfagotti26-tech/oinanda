"""Entrar e criar conta com o Google: estado de uso único, e-mail confirmado, conta existente entra, conta nova termina o cadastro."""
import os
from urllib.parse import urlparse, parse_qs
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  GOOGLE_CLIENT_ID='x.apps.googleusercontent.com', GOOGLE_CLIENT_SECRET='segredo-google-1234567890')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import googleauth
from main import app

def mk(): return TestClient(app, base_url='https://testserver', follow_redirects=False)

def state_of(c, mode='login'):
    r = c.get('/api/auth/google?mode=' + mode); assert r.status_code == 303
    q = parse_qs(urlparse(r.headers['location']).query)
    assert q['scope'] == ['openid email profile'] and q['redirect_uri'][0].endswith('/api/auth/google/callback')
    return q['state'][0]

def test_cadastro_e_login_com_google(monkeypatch):
    who = {'email': 'Livia.G@gmail.com', 'email_verified': True, 'name': 'Lívia Gomes', 'sub': 'g-123'}
    async def fake(code): return dict(who)
    monkeypatch.setattr(googleauth, 'profile', fake)
    with mk() as c:
        assert c.get('/api/auth/google/ready').json() == {'on': True}
        # estado inventado não passa
        assert 'gerr=expired' in c.get('/api/auth/google/callback?code=x&state=inventado').headers['location']
        s = state_of(c, 'signup')
        loc = c.get(f'/api/auth/google/callback?code=abc&state={s}').headers['location']
        assert loc.startswith('/cadastro?g=')
        assert 'gerr=expired' in c.get(f'/api/auth/google/callback?code=abc&state={s}').headers['location']  # estado só vale uma vez
        g = parse_qs(urlparse(loc).query)['g'][0]
        assert c.get('/api/auth/google/pending?g=' + g).json()['email'] == 'livia.g@gmail.com'
        r = c.post('/api/auth/google/signup', json={'g': g, 'name': 'Lívia Gomes', 'company': 'Clínica Lívia', 'segment': 'clinica', 'phone': '11987654321', 'team': '2-4'})
        assert r.status_code == 200
        me = c.get('/api/auth/me').json(); assert me['user']['email'] == 'livia.g@gmail.com' and me['company']['name'] == 'Clínica Lívia'
        assert c.post('/api/auth/google/signup', json={'g': g, 'name': 'X Y', 'company': 'Outra', 'segment': 'clinica'}).status_code == 410  # cadastro só uma vez
    with mk() as c2:  # outro aparelho: entra direto com o Google
        s = state_of(c2)
        assert c2.get(f'/api/auth/google/callback?code=abc&state={s}').headers['location'] == '/'
        assert c2.get('/api/auth/me').json()['user']['email'] == 'livia.g@gmail.com'
    who['email_verified'] = False
    with mk() as c3:
        s = state_of(c3); assert 'gerr=email' in c3.get(f'/api/auth/google/callback?code=abc&state={s}').headers['location']
    who.update(email_verified=True, sub='g-outra-conta')
    with mk() as c4:  # mesmo e-mail, outra conta Google: não entra
        s = state_of(c4); assert 'gerr=other' in c4.get(f'/api/auth/google/callback?code=abc&state={s}').headers['location']

def test_cadastro_por_email_guarda_tamanho_do_time():
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Ana', 'email': 'ana-t@x.com', 'password': 'senha-forte-1', 'company': 'Salão Ana', 'segment': 'salao', 'team': '5-10'})
        from core import db
        assert c.portal.call(lambda: db.companies.find_one({'name': 'Salão Ana'}))['team_size'] == '5-10'
