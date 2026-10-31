"""Núcleo da API do Oi Nanda: banco, sessão, papéis, auditoria e registro de erros.

Multiempresa: todo dado de cliente carrega company_id e toda consulta filtra por ele.
Papéis dentro da empresa: owner (dono), manager (gestor), agent (atendente).
Admin da plataforma: o usuário cujo e-mail está em ADMIN_EMAIL (Paulo). Pode entrar como qualquer empresa para suporte.
"""
import os, secrets, hashlib, traceback
from datetime import datetime, timezone, timedelta
from fastapi import Request, HTTPException, Depends

MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
if MONGO_URL == 'mock':  # só para testes locais
    from mongomock_motor import AsyncMongoMockClient as _Client
else:
    from motor.motor_asyncio import AsyncIOMotorClient as _Client
client = _Client(MONGO_URL, tz_aware=True) if MONGO_URL != 'mock' else _Client(tz_aware=True)
db = client[os.environ.get('DB_NAME', 'oinanda')]
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip().lower()
ORIGINS = [o.strip() for o in os.environ.get('APP_ORIGINS', 'https://app.oinanda.com.br').split(',') if o.strip()]
TRIAL_DAYS = 7
COOKIE = 'oinanda_session'
ROLES = {'owner': 'Dono', 'manager': 'Gestor', 'agent': 'Atendente'}

def now(): return datetime.now(timezone.utc)
def iso(dt=None): return (dt or now()).isoformat()
def uid(): return secrets.token_hex(12)
def digest(v): return hashlib.sha256(v.encode()).hexdigest()
def client_ip(request: Request):
    return request.headers.get('x-forwarded-for', '').split(',')[0].strip() or (request.client.host if request.client else '')

def hash_password(value):
    salt = secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
def verify_password(value, encoded):
    try: salt, hashed = encoded.split(':')
    except ValueError: return False
    return secrets.compare_digest(hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex(), hashed)

def public_user(u):
    return {k: v for k, v in u.items() if k not in ('_id', 'password_hash')}

async def audit(request, actor, action, target=None, company_id=None, detail=None):
    """Registro de atividade: quem fez o quê, quando e de onde. Guardado por 180 dias."""
    await db.audit.insert_one({'id': uid(), 'company_id': company_id or (actor or {}).get('company_id'),
        'actor_id': (actor or {}).get('id'), 'actor': (actor or {}).get('name', 'sistema'),
        'impersonated_by': (actor or {}).get('impersonated_by'), 'action': action, 'target': target, 'detail': detail,
        'ip': client_ip(request) if request else None, 'at': now(), 'expires_at': now() + timedelta(days=180)})

async def log_error(request, exc):
    await db.errors.insert_one({'id': uid(), 'method': request.method, 'path': request.url.path,
        'error': f'{type(exc).__name__}: {exc}', 'trace': traceback.format_exc()[-4000:], 'at': now(),
        'expires_at': now() + timedelta(days=30)})

async def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token: raise HTTPException(401, 'Entre na sua conta para continuar.')
    session = await db.sessions.find_one({'token_hash': digest(token), 'expires_at': {'$gt': now()}}, {'_id': 0})
    user = await db.users.find_one({'id': session['user_id'], 'active': True}, {'_id': 0, 'password_hash': 0}) if session else None
    if not user: raise HTTPException(401, 'Sessão expirada. Entre novamente.')
    if session.get('auth_version', 0) != user.get('auth_version', 0):
        raise HTTPException(401, 'Sua senha foi alterada. Entre novamente.')
    user['is_admin'] = bool(ADMIN_EMAIL) and user['email'] == ADMIN_EMAIL
    # suporte: o admin "entra como" uma empresa; age como dono dela, e tudo fica registrado com o nome dele
    if user['is_admin'] and session.get('acting_company_id'):
        user['impersonated_by'] = user['name']
        user['company_id'] = session['acting_company_id']
        user['role'] = 'owner'
    company = await db.companies.find_one({'id': user['company_id']}, {'_id': 0})
    if not company: raise HTTPException(401, 'Empresa não encontrada.')
    if company['status'] == 'suspended' and not user['is_admin']:
        raise HTTPException(403, 'Esta conta está suspensa. Fale com o suporte do Oi Nanda.')
    if user.get('must_change_password') and request.url.path not in {'/api/auth/me', '/api/auth/password', '/api/auth/logout'}:
        raise HTTPException(403, 'Defina sua senha pessoal para continuar.')
    user['company'] = company
    user['session_hash'] = session['token_hash']
    return user

def role_at_least(*roles):
    async def dep(user=Depends(current_user)):
        if user['role'] not in roles: raise HTTPException(403, 'Seu papel não permite esta ação.')
        return user
    return dep
managers = role_at_least('owner', 'manager')
owners = role_at_least('owner')
async def admin(user=Depends(current_user)):
    if not user['is_admin']: raise HTTPException(403, 'Área exclusiva do admin do Oi Nanda.')
    return user
