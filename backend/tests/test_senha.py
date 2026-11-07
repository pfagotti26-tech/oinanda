import os, re
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import mailer
from core import db, now
from main import app

def link_from(mail):
    return re.search(r'nova-senha\?t=([\w-]+)', mail['text']).group(1)

def test_esqueci_a_senha():
    mailer.SENT.clear()
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Rita Gomes', 'email': 'rita@x.com', 'password': 'senha-forte-1', 'company': 'Studio R', 'segment': 'studio'})
        c.portal.call(lambda: __import__('asyncio').sleep(0.05))
        assert any(m['subject'] == 'Bem-vindo ao Oi Nanda' for m in mailer.SENT)
        c.post('/api/auth/logout')
        # e-mail sem conta: mesma resposta, nada enviado
        n = len(mailer.SENT)
        assert c.post('/api/auth/forgot', json={'email': 'ninguem@x.com'}).json() == {'ok': True}
        assert len(mailer.SENT) == n
        assert c.post('/api/auth/forgot', json={'email': 'Rita@x.com'}).json() == {'ok': True}
        mail = mailer.SENT[-1]
        assert mail['to'] == 'rita@x.com' and 'Crie sua nova senha' in mail['subject']
        tok = link_from(mail)
        assert c.get('/api/auth/reset', params={'token': tok}).json() == {'valid': True, 'name': 'Rita', 'invite': False}
        assert c.get('/api/auth/reset', params={'token': 'x' * 40}).json() == {'valid': False}
        r = c.post('/api/auth/reset', json={'token': tok, 'password': 'nova-senha-123'})
        assert r.status_code == 200 and c.get('/api/auth/me').status_code == 200  # já entra
        assert c.post('/api/auth/reset', json={'token': tok, 'password': 'outra-senha-123'}).status_code == 400  # uma vez só
        c.post('/api/auth/logout')
        assert c.post('/api/auth/login', json={'email': 'rita@x.com', 'password': 'senha-forte-1'}).status_code == 401
        assert c.post('/api/auth/login', json={'email': 'rita@x.com', 'password': 'nova-senha-123'}).status_code == 200
        # link vencido
        c.post('/api/auth/forgot', json={'email': 'rita@x.com'})
        tok2 = link_from(mailer.SENT[-1])
        c.portal.call(lambda: db.password_resets.update_many({}, {'$set': {'expires_at': now() - timedelta(minutes=1)}}))
        assert c.post('/api/auth/reset', json={'token': tok2, 'password': 'nova-senha-456'}).status_code == 400
        # muitas tentativas para o mesmo e-mail
        for _ in range(3): c.post('/api/auth/forgot', json={'email': 'rita@x.com'})
        assert c.post('/api/auth/forgot', json={'email': 'rita@x.com'}).status_code == 429

def test_convite_por_email():
    mailer.SENT.clear()
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dono T', 'email': 'donot@x.com', 'password': 'senha-forte-1', 'company': 'Loja T', 'segment': 'studio'})
        r = c.post('/api/team', json={'name': 'Ana Lima', 'email': 'ana.t@x.com', 'role': 'agent'}).json()
        assert r['emailed'] is True
        mail = mailer.SENT[-1]
        assert mail['to'] == 'ana.t@x.com' and 'Loja T' in mail['html']
        tok = link_from(mail)
        assert c.get('/api/auth/reset', params={'token': tok}).json()['invite'] is True
        c.post('/api/auth/logout')
        assert c.post('/api/auth/reset', json={'token': tok, 'password': 'senha-da-ana-1'}).status_code == 200
        me = c.get('/api/auth/me').json()['user']
        assert me['email'] == 'ana.t@x.com' and not me.get('must_change_password')

def test_aviso_fim_do_teste():
    mailer.SENT.clear()
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona F', 'email': 'donaf@x.com', 'password': 'senha-forte-1', 'company': 'Loja F', 'segment': 'studio'})
        cid = c.get('/api/auth/me').json()['company']['id']
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'trial_ends_at': now() + timedelta(days=2, hours=12)}}))
        c.portal.call(mailer.trial_tick); c.portal.call(mailer.trial_tick)
        assert [m['subject'] for m in mailer.SENT if m['to'] == 'donaf@x.com'].count('Seu teste do Oi Nanda acaba em 3 dias') == 1
