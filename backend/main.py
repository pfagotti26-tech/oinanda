"""API do Oi Nanda — etapa 1: contas, empresas, equipe, admin, auditoria e saúde do sistema."""
import secrets, logging
from contextlib import asynccontextmanager
from datetime import timedelta
from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import JSONResponse
from core import (db, client, now, iso, uid, digest, client_ip, hash_password, verify_password, public_user, audit,
                  log_error, current_user, managers, owners, admin, ADMIN_EMAIL, ORIGINS, TRIAL_DAYS, COOKIE, ROLES)
from whatsapp import router as wa_router
from ai import router as ai_router
from funnel import router as funnel_router
from agenda import router as agenda_router
from gcal import router as gcal_router
from niches import router as niches_router
from automations import router as auto_router
from reports import router as reports_router
from inbox import router as inbox_router
import automations, asyncio, os
from schemas import Signup, Login, PasswordChange, Invite, MemberUpdate, CompanyUpdate, AdminCompanyUpdate

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('oinanda')

@asynccontextmanager
async def lifespan(app):
    await db.users.create_index('email', unique=True)
    await db.users.create_index('company_id')
    await db.companies.create_index('id', unique=True)
    await db.sessions.create_index('token_hash', unique=True)
    await db.sessions.create_index('expires_at', expireAfterSeconds=0)
    await db.audit.create_index([('company_id', 1), ('at', -1)])
    await db.audit.create_index('expires_at', expireAfterSeconds=0)
    await db.errors.create_index('expires_at', expireAfterSeconds=0)
    await db.login_limits.create_index('expires_at', expireAfterSeconds=0)
    await db.wa_numbers.create_index('company_id', unique=True)
    await db.wa_numbers.create_index('phone_number_id', unique=True)
    await db.contacts.create_index([('company_id', 1), ('wa_id', 1)], unique=True)
    await db.contacts.create_index([('company_id', 1), ('last_at', -1)])
    await db.messages.create_index([('contact_id', 1), ('at', -1)])
    await db.messages.create_index('wa_msg_id', sparse=True)
    await db.contacts.create_index([('company_id', 1), ('stage', 1)])
    await db.appointments.create_index([('company_id', 1), ('start', 1)])
    await db.appointments.create_index([('contact_id', 1), ('start', -1)])
    await db.agenda_settings.create_index('company_id', unique=True)
    await db.google_accounts.create_index('company_id', unique=True)
    await db.oauth_states.create_index('state', unique=True)
    await db.oauth_states.create_index('expires_at', expireAfterSeconds=0)
    await db.ai_settings.create_index('company_id', unique=True)
    await db.ai_usage.create_index([('company_id', 1), ('month', 1)], unique=True)
    await db.stage_events.create_index([('company_id', 1), ('at', -1)])
    await db.templates.create_index([('company_id', 1), ('name', 1)])
    await db.automations.create_index('company_id')
    await db.automation_runs.create_index('key', unique=True)
    await db.automation_runs.create_index([('company_id', 1), ('at', -1)])
    await db.automation_runs.create_index('expires_at', expireAfterSeconds=0)
    await db.ai_tests.create_index('company_id')
    await db.quick_replies.create_index([('company_id', 1), ('shortcut', 1)])
    await db.quality.create_index([('company_id', 1), ('at', -1)])
    await db.quality.create_index('contact_id')
    worker = None
    if os.environ.get('MONGO_URL') != 'mock' and os.environ.get('AUTOMATIONS_OFF') != '1':
        worker = asyncio.create_task(automations.loop())
    log.info('Oi Nanda API pronta. Admin: %s', ADMIN_EMAIL or '(ADMIN_EMAIL não definido)')
    yield
    if worker: worker.cancel()
    client.close()

app = FastAPI(title='Oi Nanda', lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(wa_router, prefix='/api')
app.include_router(ai_router, prefix='/api')
app.include_router(funnel_router, prefix='/api')
app.include_router(agenda_router, prefix='/api')
app.include_router(gcal_router, prefix='/api')
app.include_router(niches_router, prefix='/api')
app.include_router(auto_router, prefix='/api')
app.include_router(reports_router, prefix='/api')
app.include_router(inbox_router, prefix='/api')

@app.middleware('http')
async def guard(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method in ('POST', 'PATCH', 'PUT', 'DELETE') and origin and origin not in ORIGINS:
        return JSONResponse({'detail': 'Origem não autorizada.'}, status_code=403)
    try:
        response = await call_next(request)
    except Exception as exc:  # erro inesperado: registra e responde sem expor detalhes
        log.exception('erro em %s %s', request.method, request.url.path)
        try: await log_error(request, exc)
        except Exception: pass
        return JSONResponse({'detail': 'Algo deu errado do nosso lado. Já fomos avisados.'}, status_code=500)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def out(user):
    u = public_user(user)
    u.pop('session_hash', None)
    return u

async def open_session(user, response, acting_company_id=None):
    token = secrets.token_urlsafe(40)
    await db.sessions.insert_one({'token_hash': digest(token), 'user_id': user['id'], 'auth_version': user.get('auth_version', 0),
        'acting_company_id': acting_company_id, 'created_at': now(), 'expires_at': now() + timedelta(days=7)})
    response.set_cookie(COOKIE, token, httponly=True, secure=True, samesite='lax', max_age=7 * 86400, path='/api')

async def throttle(key, limit=10, minutes=15):
    doc = await db.login_limits.find_one({'id': key})
    if doc and doc['count'] >= limit and doc['expires_at'] > now():
        raise HTTPException(429, f'Muitas tentativas. Aguarde {minutes} minutos.')
    return doc
async def strike(key, doc, minutes=15):
    count = (doc['count'] if doc and doc['expires_at'] > now() else 0) + 1
    await db.login_limits.update_one({'id': key}, {'$set': {'count': count, 'expires_at': now() + timedelta(minutes=minutes)}}, upsert=True)

# ---------- saúde ----------
@app.get('/api/health')
async def health():
    await db.command('ping')
    return {'ok': True, 'at': iso()}

# ---------- conta ----------
@app.post('/api/auth/signup')
async def signup(body: Signup, request: Request, response: Response):
    key = digest('signup:' + client_ip(request))
    doc = await throttle(key, limit=5, minutes=60)
    email = str(body.email).lower()
    if await db.users.find_one({'email': email}): raise HTTPException(409, 'Já existe uma conta com este e-mail. Entre ou recupere a senha.')
    company = {'id': uid(), 'name': body.company.strip(), 'segment': body.segment, 'phone': body.phone.strip(), 'timezone': 'America/Sao_Paulo',
               'plan': 'trial', 'status': 'active', 'trial_ends_at': now() + timedelta(days=TRIAL_DAYS), 'created_at': now()}
    user = {'id': uid(), 'company_id': company['id'], 'name': body.name.strip(), 'email': email, 'role': 'owner', 'active': True,
            'password_hash': hash_password(body.password), 'auth_version': 0, 'created_at': now(), 'last_login_at': now()}
    await db.companies.insert_one(company.copy())
    await db.users.insert_one(user.copy())
    await strike(key, doc, minutes=60)
    await audit(request, user, 'Criou a conta da empresa', company['name'], company['id'])
    log.info('nova empresa: %s (%s)', company['name'], email)
    await open_session(user, response)
    return {'ok': True}

@app.post('/api/auth/login')
async def login(body: Login, request: Request, response: Response):
    email = str(body.email).lower()
    key = digest('login:' + email + ':' + client_ip(request))
    doc = await throttle(key)
    user = await db.users.find_one({'email': email, 'active': True}, {'_id': 0})
    if not user or not verify_password(body.password, user['password_hash']):
        await strike(key, doc)
        raise HTTPException(401, 'E-mail ou senha incorretos.')
    await db.login_limits.delete_one({'id': key})
    await db.users.update_one({'id': user['id']}, {'$set': {'last_login_at': now()}})
    await audit(request, user, 'Entrou no sistema')
    await open_session(user, response)
    return {'ok': True}

@app.post('/api/auth/logout')
async def logout(request: Request, response: Response):
    await db.sessions.delete_many({'token_hash': digest(request.cookies.get(COOKIE, ''))})
    response.delete_cookie(COOKIE, path='/api')
    return {'ok': True}

@app.get('/api/auth/me')
async def me(user=Depends(current_user)):
    c = user['company']
    trial_left = max(0, (c['trial_ends_at'] - now()).days + 1) if c['plan'] == 'trial' else None
    return {'user': out({k: v for k, v in user.items() if k != 'company'}), 'company': c, 'trial_days_left': trial_left, 'roles': ROLES}

@app.post('/api/auth/password')
async def change_password(body: PasswordChange, request: Request, response: Response, user=Depends(current_user)):
    key = digest('pw:' + user['id'])
    doc = await throttle(key, limit=5)
    stored = await db.users.find_one({'id': user['id']}, {'_id': 0})
    if not verify_password(body.current_password, stored['password_hash']):
        await strike(key, doc)
        raise HTTPException(400, 'A senha atual não está correta.')
    if body.current_password == body.new_password: raise HTTPException(400, 'Escolha uma senha diferente da atual.')
    version = stored.get('auth_version', 0) + 1
    await db.users.update_one({'id': user['id']}, {'$set': {'password_hash': hash_password(body.new_password), 'must_change_password': False, 'auth_version': version}})
    await db.sessions.delete_many({'user_id': user['id']})
    await audit(request, user, 'Trocou a própria senha')
    await open_session({**stored, 'auth_version': version}, response)
    return {'ok': True}

# ---------- empresa ----------
@app.patch('/api/company')
async def update_company(body: CompanyUpdate, request: Request, user=Depends(owners)):
    changes = {k: v.strip() if isinstance(v, str) else v for k, v in body.model_dump(exclude_none=True).items()}
    if changes:
        await db.companies.update_one({'id': user['company_id']}, {'$set': changes})
        await audit(request, user, 'Alterou os dados da empresa', ', '.join(changes))
    return await db.companies.find_one({'id': user['company_id']}, {'_id': 0})

# ---------- equipe ----------
@app.get('/api/team')
async def team(user=Depends(current_user)):
    return await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'password_hash': 0}).sort('created_at', 1).to_list(500)

@app.post('/api/team')
async def invite(body: Invite, request: Request, user=Depends(managers)):
    email = str(body.email).lower()
    if await db.users.find_one({'email': email}): raise HTTPException(409, 'Este e-mail já tem acesso ao Oi Nanda.')
    temp = secrets.token_urlsafe(9)
    member = {'id': uid(), 'company_id': user['company_id'], 'name': body.name.strip(), 'email': email, 'role': body.role, 'active': True,
              'password_hash': hash_password(temp), 'must_change_password': True, 'auth_version': 0, 'created_at': now(), 'invited_by': user['id']}
    await db.users.insert_one(member.copy())
    await audit(request, user, f'Convidou {member["name"]} como {ROLES[body.role]}', email)
    return {'member': out(member), 'temp_password': temp}

@app.patch('/api/team/{member_id}')
async def update_member(member_id: str, body: MemberUpdate, request: Request, user=Depends(managers)):
    member = await db.users.find_one({'id': member_id, 'company_id': user['company_id']}, {'_id': 0})
    if not member: raise HTTPException(404, 'Pessoa não encontrada.')
    if member['role'] == 'owner': raise HTTPException(400, 'O dono da conta não pode ser alterado aqui.')
    if member['id'] == user['id']: raise HTTPException(400, 'Você não pode alterar o próprio acesso.')
    changes = body.model_dump(exclude_none=True)
    if 'role' in changes and user['role'] != 'owner': raise HTTPException(403, 'Só o dono muda papéis.')
    if changes:
        await db.users.update_one({'id': member_id}, {'$set': changes})
        if changes.get('active') is False: await db.sessions.delete_many({'user_id': member_id})
        await audit(request, user, 'Alterou o acesso de ' + member['name'], ', '.join(f'{k}={v}' for k, v in changes.items()))
    return out({**member, **changes})

@app.post('/api/team/{member_id}/reset')
async def reset_member(member_id: str, request: Request, user=Depends(managers)):
    member = await db.users.find_one({'id': member_id, 'company_id': user['company_id']}, {'_id': 0})
    if not member or member['role'] == 'owner': raise HTTPException(404, 'Pessoa não encontrada.')
    temp = secrets.token_urlsafe(9)
    await db.users.update_one({'id': member_id}, {'$set': {'password_hash': hash_password(temp), 'must_change_password': True},
                                                  '$inc': {'auth_version': 1}})
    await db.sessions.delete_many({'user_id': member_id})
    await audit(request, user, 'Gerou senha provisória para ' + member['name'])
    return {'temp_password': temp}

# ---------- atividade da empresa ----------
@app.get('/api/audit')
async def company_audit(user=Depends(managers)):
    return await db.audit.find({'company_id': user['company_id']}, {'_id': 0, 'expires_at': 0}).sort('at', -1).to_list(200)

# ---------- admin da plataforma ----------
@app.get('/api/admin/stats')
async def admin_stats(user=Depends(admin)):
    week = now() - timedelta(days=7)
    return {'companies': await db.companies.count_documents({}),
            'active': await db.companies.count_documents({'status': 'active'}),
            'trial': await db.companies.count_documents({'plan': 'trial'}),
            'paying': await db.companies.count_documents({'plan': {'$in': ['oferta', 'completo', 'rede']}}),
            'suspended': await db.companies.count_documents({'status': 'suspended'}),
            'users': await db.users.count_documents({}),
            'new_week': await db.companies.count_documents({'created_at': {'$gte': week}}),
            'errors_day': await db.errors.count_documents({'at': {'$gte': now() - timedelta(days=1)}})}

@app.get('/api/admin/companies')
async def admin_companies(user=Depends(admin)):
    companies = await db.companies.find({}, {'_id': 0}).sort('created_at', -1).to_list(1000)
    for c in companies:
        c['users'] = await db.users.count_documents({'company_id': c['id']})
        owner = await db.users.find_one({'company_id': c['id'], 'role': 'owner'}, {'_id': 0, 'name': 1, 'email': 1, 'last_login_at': 1})
        c['owner'] = owner
    return companies

@app.patch('/api/admin/companies/{company_id}')
async def admin_update_company(company_id: str, body: AdminCompanyUpdate, request: Request, user=Depends(admin)):
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if not company: raise HTTPException(404, 'Empresa não encontrada.')
    changes = {k: v for k, v in body.model_dump(exclude_none=True).items() if k in ('status', 'plan', 'note')}
    if body.extend_trial_days:
        changes['trial_ends_at'] = max(company['trial_ends_at'], now()) + timedelta(days=body.extend_trial_days)
    if changes:
        await db.companies.update_one({'id': company_id}, {'$set': changes})
        if changes.get('status') == 'suspended':
            ids = [u['id'] for u in await db.users.find({'company_id': company_id}, {'id': 1}).to_list(1000)]
            await db.sessions.delete_many({'user_id': {'$in': ids}})
        await audit(request, {**user, 'company_id': company_id}, 'Admin alterou a conta', ', '.join(changes), company_id)
    return await db.companies.find_one({'id': company_id}, {'_id': 0})

@app.post('/api/admin/companies/{company_id}/enter')
async def admin_enter(company_id: str, request: Request, response: Response, user=Depends(admin)):
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if not company: raise HTTPException(404, 'Empresa não encontrada.')
    await db.sessions.delete_many({'token_hash': user['session_hash']})
    await audit(request, {**user, 'company_id': company_id}, 'Admin entrou na conta para suporte', company['name'], company_id)
    await open_session(user, response, acting_company_id=company_id)
    return {'ok': True}

@app.post('/api/admin/exit')
async def admin_exit(request: Request, response: Response, user=Depends(admin)):
    await db.sessions.delete_many({'token_hash': user['session_hash']})
    await open_session(user, response)
    return {'ok': True}

@app.get('/api/admin/audit')
async def admin_audit(user=Depends(admin)):
    rows = await db.audit.find({}, {'_id': 0, 'expires_at': 0}).sort('at', -1).to_list(300)
    names = {c['id']: c['name'] for c in await db.companies.find({}, {'_id': 0, 'id': 1, 'name': 1}).to_list(5000)}
    for r in rows: r['company'] = names.get(r.get('company_id'), '—')
    return rows

@app.get('/api/admin/errors')
async def admin_errors(user=Depends(admin)):
    return await db.errors.find({}, {'_id': 0, 'expires_at': 0}).sort('at', -1).to_list(100)
