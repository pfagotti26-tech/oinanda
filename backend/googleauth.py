"""Entrar e criar conta com o Google (OpenID Connect, fluxo pelo servidor).
Usa as mesmas credenciais do Google Agenda (GOOGLE_CLIENT_ID/SECRET). Redirect: {APP_PUBLIC_URL}/api/auth/google/callback.
Só pede nome e e-mail (escopos openid, email, profile). Admin com verificação em duas etapas continua entrando com senha e código."""
import os, secrets
from datetime import timedelta
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from core import db, now, digest
from schemas import Segment
from whatsapp import PUBLIC_URL

router = APIRouter()

def configured(): return bool(os.environ.get('GOOGLE_CLIENT_ID') and os.environ.get('GOOGLE_CLIENT_SECRET'))
def redirect_uri(): return PUBLIC_URL + '/api/auth/google/callback'

def back(path, **q): return RedirectResponse(path + ('?' + urlencode(q) if q else ''), status_code=303)

@router.get('/auth/google/ready')
async def ready(): return {'on': configured()}

@router.get('/auth/google')
async def start(mode: str = 'login'):
    if not configured(): return back('/cadastro' if mode == 'signup' else '/', gerr='off')
    state = secrets.token_urlsafe(24)
    await db.oauth_states.insert_one({'hash': digest(state), 'mode': 'signup' if mode == 'signup' else 'login', 'expires_at': now() + timedelta(minutes=10)})
    q = {'client_id': os.environ['GOOGLE_CLIENT_ID'], 'redirect_uri': redirect_uri(), 'response_type': 'code', 'scope': 'openid email profile',
         'state': state, 'prompt': 'select_account', 'access_type': 'online'}
    return RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(q), status_code=303)

async def profile(code):
    """Troca o código pelo token e lê nome e e-mail direto no Google (servidor a servidor)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post('https://oauth2.googleapis.com/token', data={'code': code, 'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
                                                                      'redirect_uri': redirect_uri(), 'grant_type': 'authorization_code'})
        if r.status_code != 200: return None
        tok = r.json().get('access_token')
        u = await c.get('https://openidconnect.googleapis.com/v1/userinfo', headers={'Authorization': f'Bearer {tok}'})
        return u.json() if u.status_code == 200 else None

@router.get('/auth/google/callback')
async def callback(request: Request, code: str = '', state: str = '', error: str = ''):
    st = await db.oauth_states.find_one_and_delete({'hash': digest(state or '-')}) if state else None
    exp = st and st.get('expires_at')
    if exp and exp.tzinfo is None: from datetime import timezone; exp = exp.replace(tzinfo=timezone.utc)
    if error or not st or not code or exp <= now(): return back('/', gerr='cancel' if error else 'expired')
    p = await profile(code)
    if not p or not p.get('email') or not p.get('email_verified'): return back('/', gerr='email')
    email, sub = p['email'].strip().lower(), str(p.get('sub') or '')
    import main
    user = await db.users.find_one({'email': email}, {'_id': 0})
    if user:
        if not user.get('active', True): return back('/', gerr='inactive')
        import core
        if user.get('mfa_on') and core.ADMIN_MFA: return back('/', gerr='mfa', email=email)  # admin: senha + código, sempre
        if user.get('google_sub') and sub and user['google_sub'] != sub: return back('/', gerr='other')
        resp = back('/')
        await db.users.update_one({'id': user['id']}, {'$set': {'last_login_at': now(), 'google_sub': sub}})
        await main.open_session(user, resp)
        await main.audit(request, user, 'Entrou no sistema com o Google')
        return resp
    # conta nova: guarda nome e e-mail confirmados pelo Google e segue para as 2 telas do cadastro
    tok = secrets.token_urlsafe(24)
    await db.google_pending.insert_one({'hash': digest(tok), 'email': email, 'name': (p.get('name') or p.get('given_name') or email.split('@')[0])[:80],
                                        'sub': sub, 'picture': str(p.get('picture') or '')[:400], 'expires_at': now() + timedelta(minutes=30)})
    return back('/cadastro', g=tok)

async def pending(tok):
    d = await db.google_pending.find_one({'hash': digest(tok or '-')}, {'_id': 0})
    exp = d and d.get('expires_at')
    if exp and exp.tzinfo is None: from datetime import timezone; exp = exp.replace(tzinfo=timezone.utc)
    if not d or exp <= now(): raise HTTPException(410, 'O acesso pelo Google expirou. Toque em "Continuar com Google" de novo.')
    return d

@router.get('/auth/google/pending')
async def get_pending(g: str):
    d = await pending(g)
    return {'email': d['email'], 'name': d['name'], 'picture': d['picture']}

class GoogleSignup(BaseModel):
    g: str = Field(min_length=10, max_length=100)
    name: str = Field(min_length=2, max_length=80)
    company: str = Field(min_length=2, max_length=80)
    segment: Segment = 'outro'
    phone: str = Field(default='', max_length=30)
    wa_optin: bool = True
    team: str = Field(default='', max_length=6)

@router.post('/auth/google/signup')
async def google_signup(body: GoogleSignup, request: Request, response: Response):
    import main
    d = await pending(body.g)
    if await db.users.find_one({'email': d['email']}, {'_id': 1}): raise HTTPException(409, 'Já existe uma conta com este e-mail. Entre com ele.')
    await main.create_account(request, response, name=body.name, email=d['email'], company=body.company, segment=body.segment, phone=body.phone,
                              wa_optin=body.wa_optin, team=body.team if body.team in ('1', '2-4', '5-10', '11+') else '', google_sub=d['sub'])
    await db.google_pending.delete_one({'hash': digest(body.g)})
    return {'ok': True}
