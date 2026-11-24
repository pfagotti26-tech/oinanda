import os, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import core, mfa
from main import app

def mk(): return TestClient(app, base_url='https://testserver')
def code(secret, dt=0): return mfa._hotp(secret.replace(' ', ''), int((time.time() + dt) // 30))
ADM = {'email': 'admin@oinanda.com.br', 'password': 'admin-forte-1'}

def test_totp_janela_e_replay():
    s = mfa.new_secret(); st = int(time.time() // 30)
    assert mfa.check_totp(s, code(s)) == st
    assert mfa.check_totp(s, code(s, -30)) == st - 1          # relógio do celular atrasado
    assert mfa.check_totp(s, code(s, -90)) is None             # código velho
    assert mfa.check_totp(s, code(s), last_step=st) is None    # o mesmo código não vale duas vezes
    assert mfa.check_totp(s, '12345') is None and mfa.check_totp(s, 'abcdef') is None

def test_admin_exige_duas_etapas(monkeypatch):
    monkeypatch.setattr(core, 'ADMIN_MFA', True)
    with mk() as a:
        a.post('/api/auth/signup', json={'name': 'Paulo', 'company': 'Oi Nanda', **ADM})
        r = a.get('/api/admin/stats'); assert r.status_code == 403 and 'duas etapas' in r.json()['detail']
        assert a.get('/api/auth/me').json()['mfa'] == {'on': False, 'ok': False, 'need': True}
        st = a.post('/api/admin/2fa/start').json(); assert st['qr'].startswith('<?xml') or '<svg' in st['qr']
        assert st['uri'].startswith('otpauth://totp/')
        assert a.post('/api/admin/2fa/confirm', json={'code': '000000'}).status_code == 400
        ok = a.post('/api/admin/2fa/confirm', json={'code': code(st['secret'])}).json()
        rec = ok['recovery']; assert len(rec) == 8 and len(rec[0]) == 11
        assert a.get('/api/admin/stats').status_code == 200  # a sessão em que ligou já vale
        assert a.get('/api/admin/2fa').json() == {'on': True, 'ok': True, 'recovery_left': 8}
        me = a.get('/api/auth/me').json(); assert 'mfa_secret' not in me['user'] and 'mfa_recovery' not in me['user']
        assert all('mfa_secret' not in u and 'mfa_recovery' not in u for u in a.get('/api/team').json())
        sec = st['secret']
    with mk() as b:  # outro aparelho: senha certa não basta
        r = b.post('/api/auth/login', json=ADM); assert r.status_code == 428
        assert b.post('/api/auth/login', json={**ADM, 'code': '000000'}).status_code == 401
        time.sleep(0)  # o código usado na ativação não vale de novo
        assert b.post('/api/auth/login', json={**ADM, 'code': code(sec)}).status_code == 401
        assert b.post('/api/auth/login', json={**ADM, 'code': code(sec, 30)}).status_code == 200
        assert b.get('/api/admin/stats').status_code == 200
    with mk() as d:  # código de recuperação vale uma vez só
        assert d.post('/api/auth/login', json={**ADM, 'code': rec[0]}).status_code == 200
        assert d.get('/api/admin/2fa').json()['recovery_left'] == 7
    with mk() as e:
        assert e.post('/api/auth/login', json={**ADM, 'code': rec[0]}).status_code == 401

def test_cliente_comum_nao_muda(monkeypatch):
    monkeypatch.setattr(core, 'ADMIN_MFA', True)
    with mk() as c:
        c.post('/api/auth/signup', json={'name': 'Renata', 'email': 're@bella.com', 'password': 'senha-forte-1', 'company': 'Clínica Bella'})
        assert c.post('/api/auth/logout', json={}).status_code == 200
        assert c.post('/api/auth/login', json={'email': 're@bella.com', 'password': 'senha-forte-1'}).status_code == 200
        assert c.get('/api/auth/me').json()['mfa'] is None
        assert c.post('/api/admin/2fa/start').status_code == 403
