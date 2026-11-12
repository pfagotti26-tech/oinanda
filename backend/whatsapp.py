"""Etapa 2 — WhatsApp oficial (Cloud API da Meta).

- Cada empresa conecta o próprio número (phone_number_id + WABA). O token fica criptografado no banco.
- Webhook único: /api/wa/webhook. A assinatura da Meta (X-Hub-Signature-256) é conferida com META_APP_SECRET.
  Ao conectar, o número é inscrito no app com override_callback_uri apontando para este webhook, então
  outros sistemas que usem o mesmo app da Meta (ex.: Alta Pulse) continuam recebendo os deles.
- Conversas: contato + mensagens. Atendente vê as dele e as sem dono; dono e gestor veem todas.
- Janela de 24 h: fora dela só sai mensagem por modelo aprovado (chega na etapa de campanhas).

.env: META_APP_SECRET, WA_VERIFY_TOKEN, SECRET_KEY (criptografia), APP_PUBLIC_URL (padrão https://app.oinanda.com.br).
"""
import os, re, hmac, hashlib, json, logging, secrets
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Request, Response, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, owners

log = logging.getLogger('oinanda.wa')
router = APIRouter()
GRAPH = 'https://graph.facebook.com/' + os.environ.get('WA_API_VERSION', 'v21.0')
PUBLIC_URL = os.environ.get('APP_PUBLIC_URL', 'https://app.oinanda.com.br').rstrip('/')
WINDOW = timedelta(hours=24)
ON_INBOUND = None  # a Nanda se pendura aqui (ai.py) para responder mensagens novas
ON_HUMAN_SEND = None  # e aqui, para sair de cena quando alguém do time responde
STOP = re.compile(r'^\s*(parar|pare|sair|stop|descadastrar|n[aã]o quero (mais )?receber( mensagens?)?)\s*[.!]*\s*$', re.I)

def ad_of(m):
    """Clique em anúncio de 'mandar mensagem': a Meta manda junto de qual anúncio veio."""
    r = m.get('referral') or {}
    if not r: return None
    return {k: r.get(k) for k in ('source_type', 'source_id', 'source_url', 'headline', 'body', 'ctwa_clid') if r.get(k)} | {'at': now()}

def _fernet():
    key = os.environ.get('SECRET_KEY', '')
    if not key: raise HTTPException(500, 'SECRET_KEY não configurada no servidor.')
    return Fernet(key.encode())
def encrypt(v): return _fernet().encrypt(v.encode()).decode()
def decrypt(v):
    try: return _fernet().decrypt(v.encode()).decode()
    except InvalidToken: raise HTTPException(500, 'Não foi possível ler o token do WhatsApp. Conecte o número de novo.')

async def graph(method, path, token, payload=None, params=None):
    """Chamada à API da Meta. Isolada aqui para os testes poderem substituir."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, GRAPH + path, headers={'Authorization': 'Bearer ' + token} if token else {}, json=payload, params=params)
    try: data = r.json()
    except ValueError: data = {}
    if r.status_code >= 400:
        err = (data.get('error') or {})
        raise HTTPException(400, 'A Meta recusou: ' + (err.get('error_user_msg') or err.get('message') or f'erro {r.status_code}'))
    return data

def aware(dt):
    if dt is None: return None
    if isinstance(dt, str): dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

# ---------- conexão do número ----------
class ConnectIn(BaseModel):
    phone_number_id: str = Field(min_length=5, max_length=40, pattern=r'^\d+$')
    waba_id: str = Field(min_length=5, max_length=40, pattern=r'^\d+$')
    token: str = Field(min_length=20, max_length=1000)

def number_out(n):
    if not n: return None
    return {k: n.get(k) for k in ('phone_number_id', 'waba_id', 'display_phone', 'verified_name', 'quality', 'status', 'connected_at', 'webhook', 'mode', 'register_error', 'sync', 'token_kind', 'token_expires_at')}

@router.get('/wa/number')
async def get_number(user=Depends(current_user)):
    return number_out(await db.wa_numbers.find_one({'company_id': user['company_id']}, {'_id': 0}))

async def save_number(user, request, pnid, waba_id, token, extra=None):
    other = await db.wa_numbers.find_one({'phone_number_id': pnid})
    if other and other['company_id'] != user['company_id']: raise HTTPException(409, 'Este número já está conectado em outra conta.')
    info = await graph('GET', f'/{pnid}', token, params={'fields': 'display_phone_number,verified_name,quality_rating'})
    webhook = 'ok'
    try:
        await graph('POST', f'/{waba_id}/subscribed_apps', token,
                    {'override_callback_uri': PUBLIC_URL + '/api/wa/webhook', 'verify_token': os.environ.get('WA_VERIFY_TOKEN', '')})
    except HTTPException as e:
        webhook = e.detail  # número conecta mesmo assim; o aviso aparece no painel
        log.warning('inscrição do webhook falhou para %s: %s', waba_id, e.detail)
    doc = {'company_id': user['company_id'], 'phone_number_id': pnid, 'waba_id': waba_id,
           'display_phone': info.get('display_phone_number'), 'verified_name': info.get('verified_name'),
           'quality': info.get('quality_rating'), 'token_enc': encrypt(token), 'status': 'connected',
           'webhook': webhook, 'connected_at': now(), 'mode': 'cloud', **(extra or {})}
    await db.wa_numbers.update_one({'company_id': user['company_id']}, {'$set': doc}, upsert=True)
    await audit(request, user, 'Conectou o WhatsApp' + (' (junto com o app do celular)' if doc.get('mode') == 'coexistence' else ''), doc['display_phone'])
    return doc

@router.post('/wa/connect')
async def connect(body: ConnectIn, request: Request, user=Depends(owners)):
    return number_out(await save_number(user, request, body.phone_number_id, body.waba_id, body.token))

# ---------- "Entrar com a Meta" (Embedded Signup) ----------
def es_config():
    return {'app_id': os.environ.get('META_APP_ID', ''), 'config_id': os.environ.get('META_ES_CONFIG_ID', ''),
            'enabled': bool(os.environ.get('META_APP_ID') and os.environ.get('META_ES_CONFIG_ID') and os.environ.get('META_APP_SECRET'))}

@router.get('/wa/signup-config')
async def signup_config(user=Depends(current_user)):
    return es_config()

class SignupIn(BaseModel):
    code: str = Field(min_length=10, max_length=2000)
    waba_id: str = Field(pattern=r'^\d{5,40}$')
    phone_number_id: str = Field(pattern=r'^\d{5,40}$')
    coexistence: bool = False  # número que continua no app WhatsApp Business do celular

@router.post('/wa/signup')
async def signup(body: SignupIn, request: Request, user=Depends(owners)):
    cfg = es_config()
    if not cfg['enabled']: raise HTTPException(400, 'O botão da Meta ainda não foi ativado no servidor.')
    tok = await graph('GET', '/oauth/access_token', None, params={'client_id': cfg['app_id'], 'client_secret': os.environ['META_APP_SECRET'], 'code': body.code})
    token = tok.get('access_token')
    if not token: raise HTTPException(400, 'A Meta não devolveu o acesso. Tente de novo.')
    extra = {'mode': 'coexistence' if body.coexistence else 'cloud', 'via': 'embedded_signup'}
    if not body.coexistence:  # número novo na API: registra com um PIN de verificação em duas etapas
        pin = f'{secrets.randbelow(10**6):06d}'
        try:
            await graph('POST', f'/{body.phone_number_id}/register', token, {'messaging_product': 'whatsapp', 'pin': pin})
            extra['pin_enc'] = encrypt(pin)
        except HTTPException as e:
            extra['register_error'] = str(e.detail)[:200]
    # o acesso do cadastro vence em 60 dias: se houver um usuário do sistema do Oi Nanda (token permanente),
    # ele ganha acesso à conta do cliente e passa a ser o acesso usado
    sys_tok, sys_uid = os.environ.get('META_SYSTEM_TOKEN', ''), os.environ.get('META_SYSTEM_USER_ID', '')
    if sys_tok and sys_uid:
        try:
            await graph('POST', f'/{body.waba_id}/assigned_users', token, params={'user': sys_uid, 'tasks': "['MANAGE']"})
        except HTTPException as e:
            log.warning('não deu para dar acesso ao usuário do sistema: %s', e.detail)
        try:
            await graph('GET', f'/{body.phone_number_id}', sys_tok, params={'fields': 'display_phone_number'})
            token, extra['token_kind'] = sys_tok, 'system'
        except HTTPException as e:
            log.warning('usuário do sistema ainda sem acesso ao número: %s', e.detail)
    if extra.get('token_kind') != 'system':
        extra.update(token_kind='business', token_expires_at=now() + timedelta(days=int(tok.get('expires_in') or 0) // 86400 or 60))
    doc = await save_number(user, request, body.phone_number_id, body.waba_id, token, extra)
    if body.coexistence:  # traz contatos e as conversas dos últimos 6 meses do app (só pode pedir uma vez, em até 24 h)
        sync = {}
        for kind in ('smb_app_state_sync', 'history'):
            try:
                await graph('POST', f'/{body.phone_number_id}/smb_app_data', token, {'messaging_product': 'whatsapp', 'sync_type': kind})
                sync[kind] = 'pedido'
            except HTTPException as e:
                sync[kind] = str(e.detail)[:200]
        await db.wa_numbers.update_one({'company_id': user['company_id']}, {'$set': {'sync': sync}})
    return number_out(doc)

@router.delete('/wa/number')
async def disconnect(request: Request, user=Depends(owners)):
    n = await db.wa_numbers.find_one({'company_id': user['company_id']}, {'_id': 0})
    if n:
        await db.wa_numbers.delete_one({'company_id': user['company_id']})
        await audit(request, user, 'Desconectou o WhatsApp', n.get('display_phone'))
    return {'ok': True}

# ---------- webhook da Meta ----------
@router.get('/wa/webhook')
async def verify(request: Request):
    q = request.query_params
    tok = os.environ.get('WA_VERIFY_TOKEN', '')
    if q.get('hub.mode') == 'subscribe' and tok and hmac.compare_digest(q.get('hub.verify_token') or '', tok):
        return Response(q.get('hub.challenge', ''), media_type='text/plain')
    raise HTTPException(403, 'Token de verificação inválido.')

def text_of(m):
    t = m.get('type')
    if t == 'text': return (m.get('text') or {}).get('body', '')
    if t in ('image', 'video', 'document'): return (m.get(t) or {}).get('caption') or {'image': '📷 Foto', 'video': '🎬 Vídeo', 'document': '📄 Documento'}[t]
    if t == 'audio': return '🎤 Áudio'
    if t == 'sticker': return 'Figurinha'
    if t == 'location': return '📍 Localização'
    if t == 'button': return (m.get('button') or {}).get('text', '')
    if t == 'interactive':
        i = m.get('interactive') or {}
        return (i.get('button_reply') or i.get('list_reply') or {}).get('title', '')
    if t == 'reaction': return 'Reagiu ' + (m.get('reaction') or {}).get('emoji', '')
    return f'[{t}]'

async def ingest(value):
    pnid = (value.get('metadata') or {}).get('phone_number_id')
    number = await db.wa_numbers.find_one({'phone_number_id': pnid}, {'_id': 0, 'company_id': 1})
    if not number:
        log.info('webhook de número desconhecido: %s', pnid); return
    cid = number['company_id']
    if value.get('messages'):  # mensagem chegando = recebimento funcionando
        await db.wa_numbers.update_one({'phone_number_id': pnid}, {'$set': {'webhook': 'ok', 'last_in_at': now()}})
    names = {c.get('wa_id'): (c.get('profile') or {}).get('name') for c in value.get('contacts') or []}
    for m in value.get('messages') or []:
        if await db.messages.find_one({'wa_msg_id': m.get('id')}): continue
        wa_id = m.get('from')
        at = datetime.fromtimestamp(int(m.get('timestamp', 0)) or now().timestamp(), tz=timezone.utc)
        if now() - at < timedelta(seconds=60): at = now()  # a Meta manda em segundos; a hora de chegada mantém a ordem certa
        text = text_of(m)
        from channels import variants, claim_from_instagram, on_inbound_set
        contact = await db.contacts.find_one({'company_id': cid, 'wa_id': {'$in': variants(wa_id)}}, {'_id': 0})
        if not contact: contact = await claim_from_instagram(cid, wa_id, text)
        ad = ad_of(m)
        if not contact:
            contact = {'id': uid(), 'company_id': cid, 'wa_id': wa_id, 'name': names.get(wa_id) or wa_id, 'assigned_to': None,
                       'unread': 0, 'created_at': at, 'source': 'anuncio' if ad else 'whatsapp'}
            if ad: contact['ad'] = ad
            await db.contacts.insert_one(contact.copy())
        upd = {'wa_id': wa_id} if contact.get('wa_id') != wa_id else {}  # guarda o número do jeito que a Meta manda
        if ad and not contact.get('ad'): upd['ad'] = ad
        if STOP.match(text or '') and not contact.get('opt_out'):
            upd['opt_out'] = True
            await note(cid, contact['id'], 'O cliente pediu para não receber mensagens automáticas. Automações e modelos ficam desligados para ele.')
        if not aware(contact.get('last_in_at')) or at >= aware(contact.get('last_in_at')): upd['last_in_at'] = at
        if not aware(contact.get('wa_last_in_at')) or at >= aware(contact.get('wa_last_in_at')): upd.update(on_inbound_set(contact, 'whatsapp', at))
        if not aware(contact.get('last_at')) or at >= aware(contact.get('last_at')): upd.update(last_at=at, last_text=text[:200], last_dir='in')
        if names.get(wa_id) and (contact.get('channel') != 'instagram' or not contact.get('name') or contact['name'].startswith('@')): upd['name'] = names[wa_id]
        await db.contacts.update_one({'id': contact['id']}, {'$set': upd, '$inc': {'unread': 1}})
        try:
            from origins import on_inbound as origin_hook
            await origin_hook(cid, contact, text, ad)
        except Exception: log.exception('falha ao marcar a origem')
        mid = uid()
        from media import meta_of, schedule
        mm = meta_of(m)
        await db.messages.insert_one({'id': mid, 'company_id': cid, 'contact_id': contact['id'], 'dir': 'in', 'type': m.get('type'),
                                      'text': text, 'wa_msg_id': m.get('id'), 'at': at, 'received_at': now(), **mm, **({'pending': True} if mm else {})})
        if mm: schedule(mid)  # baixa (e transcreve o áudio) antes de chamar a Nanda
        elif ON_INBOUND and now() - at < timedelta(minutes=10): ON_INBOUND(cid, contact['id'], mid)
        if now() - at < timedelta(minutes=10):
            from push import later, on_inbound as push_inbound
            later(push_inbound(cid, contact['id'], text))
    for s in value.get('statuses') or []:
        upd = {'status': s.get('status')}
        if s.get('errors'): upd['error'] = (s['errors'][0].get('title') or s['errors'][0].get('message') or 'falhou')
        await db.messages.update_one({'wa_msg_id': s.get('id')}, {'$set': upd})

# ---------- número que continua no app do celular (coexistência) ----------
async def sample(field, value):
    """Guarda os últimos avisos de cada tipo, para conferir o formato quando algo não aparece."""
    await db.webhook_samples.insert_one({'field': field, 'value': json.dumps(value)[:20000], 'at': now()})
    old = await db.webhook_samples.find({}, {'_id': 1}).sort('at', -1).skip(100).to_list(1000)
    if old: await db.webhook_samples.delete_many({'_id': {'$in': [o['_id'] for o in old]}})

async def company_of(value):
    pnid = (value.get('metadata') or {}).get('phone_number_id')
    n = await db.wa_numbers.find_one({'phone_number_id': pnid}, {'_id': 0, 'company_id': 1, 'display_phone': 1})
    return n

async def upsert_contact(cid, wa_id, name=None, source='whatsapp'):
    from channels import variants
    c = await db.contacts.find_one({'company_id': cid, 'wa_id': {'$in': variants(wa_id)}}, {'_id': 0})
    if c:
        if name and (not c.get('name') or c['name'] == wa_id): await db.contacts.update_one({'id': c['id']}, {'$set': {'name': name}}); c['name'] = name
        return c
    c = {'id': uid(), 'company_id': cid, 'wa_id': wa_id, 'name': name or wa_id, 'assigned_to': None, 'unread': 0, 'created_at': now(), 'source': source}
    try: await db.contacts.insert_one(c.copy())
    except Exception: return await db.contacts.find_one({'company_id': cid, 'wa_id': wa_id}, {'_id': 0})
    return c

async def store(cid, contact, m, direction, by=None, imported=False):
    if m.get('id') and await db.messages.find_one({'wa_msg_id': m['id']}): return False
    at = datetime.fromtimestamp(int(m.get('timestamp', 0)) or now().timestamp(), tz=timezone.utc)
    text = text_of(m)
    from media import meta_of
    doc = {'id': uid(), 'company_id': cid, 'contact_id': contact['id'], 'dir': direction, 'type': m.get('type'), 'text': text,
           'wa_msg_id': m.get('id'), 'at': at, 'received_at': now() if not imported else at, **meta_of(m)}
    if direction == 'out': doc.update(by=by, by_id=None, status='sent')
    if imported: doc['imported'] = True
    await db.messages.insert_one(doc)
    upd = {}
    if not aware(contact.get('last_at')) or at >= aware(contact.get('last_at')): upd.update(last_at=at, last_text=text[:200], last_dir=direction)
    if direction == 'in' and (not aware(contact.get('last_in_at')) or at >= aware(contact.get('last_in_at'))): upd['last_in_at'] = at
    if direction == 'in' and contact.get('ig_id') and (not aware(contact.get('wa_last_in_at')) or at >= aware(contact.get('wa_last_in_at'))): upd['wa_last_in_at'] = at
    if upd:
        await db.contacts.update_one({'id': contact['id']}, {'$set': upd}); contact.update(upd)
    return True

async def ingest_echoes(value):
    """Mensagem que alguém mandou pelo app do celular: aparece na conversa e tira a Nanda do automático."""
    n = await company_of(value)
    if not n: return
    for m in value.get('message_echoes') or []:
        if not m.get('to'): continue
        c = await upsert_contact(n['company_id'], m['to'])
        if await store(n['company_id'], c, m, 'out', by='Celular') and ON_HUMAN_SEND:
            await ON_HUMAN_SEND({'company_id': n['company_id'], 'name': 'Alguém pelo celular', 'id': None}, c)

async def ingest_history(value):
    """Conversas dos últimos 6 meses do app, enviadas pela Meta logo depois da conexão."""
    n = await company_of(value)
    if not n: return
    biz = ''.join(ch for ch in (n.get('display_phone') or '') if ch.isdigit())
    for chunk in value.get('history') or []:
        for th in chunk.get('threads') or []:
            wa_id = th.get('id')
            if not wa_id: continue
            c = await upsert_contact(n['company_id'], wa_id, source='historico')
            for m in sorted(th.get('messages') or [], key=lambda x: int(x.get('timestamp', 0))):
                outgoing = m.get('from') != wa_id or (biz and m.get('from') == biz)
                await store(n['company_id'], c, m, 'out' if outgoing else 'in', by='Celular' if outgoing else None, imported=True)

async def ingest_contacts(value):
    n = await company_of(value)
    if not n: return
    for it in value.get('state_sync') or []:
        ct = it.get('contact') or {}
        phone = ''.join(ch for ch in (ct.get('phone_number') or '') if ch.isdigit())
        if it.get('type') == 'contact' and phone and it.get('action', 'add') != 'remove':
            await upsert_contact(n['company_id'], phone, ct.get('full_name') or ct.get('first_name'), source='agenda do celular')

@router.get('/admin/webhook-samples')
async def webhook_samples(user=Depends(current_user)):
    if not user.get('is_admin'): raise HTTPException(403, 'Só o admin do Oi Nanda.')
    return await db.webhook_samples.find({}, {'_id': 0}).sort('at', -1).to_list(30)

@router.post('/wa/webhook')
async def webhook(request: Request):
    raw = await request.body()
    secret = os.environ.get('META_APP_SECRET', '')
    sig = request.headers.get('x-hub-signature-256', '')
    if not secret or not hmac.compare_digest(sig, 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()):
        log.warning('webhook com assinatura inválida')
        raise HTTPException(403, 'Assinatura inválida.')
    try: body = json.loads(raw or b'{}')
    except ValueError: return {'ok': True}
    for entry in body.get('entry') or []:
        for change in entry.get('changes') or []:
            field, value = change.get('field'), change.get('value') or {}
            try:
                if field == 'messages': await ingest(value)
                elif field == 'smb_message_echoes': await ingest_echoes(value)
                elif field == 'history': await ingest_history(value)
                elif field == 'smb_app_state_sync': await ingest_contacts(value)
                if field != 'messages': await sample(field, value)
            except Exception: log.exception('falha ao processar webhook (%s)', field)
    return {'ok': True}

# ---------- conversas ----------
def can_see(user, contact):
    return user['role'] in ('owner', 'manager') or contact.get('assigned_to') in (None, user['id'])

async def get_contact(user, contact_id):
    c = await db.contacts.find_one({'id': contact_id, 'company_id': user['company_id']}, {'_id': 0})
    if not c or not can_see(user, c): raise HTTPException(404, 'Conversa não encontrada.')
    return c

@router.get('/conversations')
async def conversations(filter: str = 'all', q: str = '', kind: str = '', origin: str = '', user=Depends(current_user)):
    from inbox import waiting
    qq = {'company_id': user['company_id']}
    if filter == 'mine': qq['assigned_to'] = user['id']
    elif filter == 'unassigned': qq['assigned_to'] = None
    elif filter == 'waiting': qq['last_dir'] = 'in'
    elif filter == 'team':
        from teams import my_team_ids
        qq['team_id'] = {'$in': await my_team_ids(user)}
    if user['role'] == 'agent' and filter not in ('mine', 'unassigned'):
        qq['assigned_to'] = {'$in': [None, user['id']]}
    term = q.strip()
    if term:
        digits = re.sub(r'\D', '', term)
        ors = [{'name': {'$regex': re.escape(term), '$options': 'i'}}, {'tags': {'$regex': re.escape(term), '$options': 'i'}}]
        if len(digits) >= 3: ors.append({'wa_id': {'$regex': digits}})
        qq['$or'] = ors
    if kind == 'prospect': qq['sales_count'] = {'$in': [None, 0]}
    elif kind in ('cliente', 'inativo'): qq['sales_count'] = {'$gt': 0}
    if origin: qq['origin'] = None if origin == '_none' else origin
    rows = await db.contacts.find(qq, {'_id': 0}).sort('last_at', -1).to_list(300)
    rows.sort(key=lambda r: 0 if r.get('needs_human') and r.get('handoff_urgent') else 1)  # urgente primeiro, o resto na ordem de chegada
    if kind in ('cliente', 'inativo'):
        from origins import status_of
        days = user['company'].get('inactive_days')
        rows = [r for r in rows if status_of(r, days) == kind]
    if filter == 'waiting': rows = [r for r in rows if waiting(r)]
    from inbox import wait_ctx, wait_info
    wctx = await wait_ctx(user['company'])
    for r in rows:
        r['waiting'] = waiting(r)
        wf, lim = wait_info(r, wctx)
        r['wait_from'], r['wait_limit'] = (wf.isoformat() if wf else None), lim
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    from channels import window_of
    for r in rows:
        r['assigned_name'] = names.get(r.get('assigned_to'))
        li = aware(r.get('last_in_at'))
        w = window_of(r.get('channel'))
        r['window_open'] = True if w is None else bool(li and now() - li < w)  # Telegram e e-mail não têm janela
    return rows

@router.get('/conversations/{contact_id}')
async def conversation(contact_id: str, user=Depends(current_user)):
    from channels import view, has, last_in, out_channel, fmt, window_of
    raw = await get_contact(user, contact_id)
    c = view(raw)
    msgs = await db.messages.find({'contact_id': contact_id, 'company_id': user['company_id']}, {'_id': 0}).sort('at', -1).to_list(200)
    if c.get('unread'): await db.contacts.update_one({'id': contact_id}, {'$set': {'unread': 0}})
    if c.get('assigned_to') == user['id'] and c.get('transfer_seen') is False:  # quem recebeu a transferência abriu
        await db.contacts.update_one({'id': contact_id}, {'$set': {'transfer_seen': True}})
    from channels import window_of
    li = aware(c.get('last_in_at'))
    w = window_of(c.get('channel'))
    c['window_open'] = True if w is None else bool(li and now() - li < w)
    c['window_ends_at'] = (li + w) if (li and w) else None
    if c.get('channel') in ('instagram', 'messenger'):  # da Meta: de 24 h a 7 dias só uma pessoa do time responde
        from instagram import human_window
        c['human_only'] = bool(li and WINDOW <= now() - li < human_window())
        if c['human_only']: c['window_open'], c['window_ends_at'] = True, li + human_window()
    from inbox import wait_ctx, wait_info, waiting as is_waiting
    c['waiting'] = is_waiting(c)
    wf, lim = wait_info(c, await wait_ctx(user['company']))
    c['wait_from'], c['wait_limit'] = (wf.isoformat() if wf else None), lim
    from channels import channels_of
    c['channels'] = channels_of(raw)
    c['reply_channel'] = out_channel(raw)
    if len(c['channels']) > 1:  # janela de cada canal, para o seletor
        from instagram import human_window
        wins = {}
        for ch in c['channels']:
            li2 = aware(last_in(raw, ch))
            lim2 = human_window() if ch in ('instagram', 'messenger') else window_of(ch)
            wins[ch] = {'open': True if lim2 is None else bool(li2 and now() - li2 < lim2), 'last_in_at': li2}
        c['windows'] = wins
    if c.get('phone_hint'): c['phone_hint_fmt'] = fmt(c['phone_hint'])
    if c.get('wa_id'): c['wa_fmt'] = fmt(c['wa_id'])
    return {'contact': c, 'messages': list(reversed(msgs))}

class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)

async def deliver(company_id, contact, text, by_id, by_name, assign_to=None):
    """Envia texto pelo canal da conversa e registra. Usado pelo time e pela Nanda."""
    from channels import view
    contact = view(contact)
    ch = contact.get('channel')
    if ch in ('instagram', 'messenger'):
        from instagram import deliver as meta_deliver
        return await meta_deliver(company_id, contact, text, by_id, by_name, assign_to, ch)
    if ch == 'telegram':
        from telegram import deliver as tg_deliver
        return await tg_deliver(company_id, contact, text, by_id, by_name, assign_to)
    li = aware(contact.get('last_in_at'))
    if not li or now() - li >= WINDOW:
        raise HTTPException(400, 'Passaram 24 horas desde a última mensagem do cliente. A Meta só permite responder com um modelo aprovado (chega na etapa de campanhas).')
    number = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0})
    if not number: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    if not contact.get('wa_id'): raise HTTPException(400, 'Este cliente não tem WhatsApp salvo.')
    res = await graph('POST', f'/{number["phone_number_id"]}/messages', decrypt(number['token_enc']),
                      {'messaging_product': 'whatsapp', 'to': contact['wa_id'], 'type': 'text', 'text': {'body': text}})
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': contact['id'], 'dir': 'out', 'type': 'text', 'text': text,
           'wa_msg_id': ((res.get('messages') or [{}])[0]).get('id'), 'status': 'sent', 'by_id': by_id, 'by': by_name, 'at': now()}
    await db.messages.insert_one(msg.copy())
    upd = {'last_at': msg['at'], 'last_text': text[:200], 'last_dir': 'out'}
    if assign_to and not contact.get('assigned_to'): upd['assigned_to'] = assign_to
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd})
    msg.pop('_id', None)
    return msg

async def note(company_id, contact_id, text):
    """Aviso do sistema dentro da conversa (não vai para o cliente)."""
    await db.messages.insert_one({'id': uid(), 'company_id': company_id, 'contact_id': contact_id, 'dir': 'sys', 'type': 'note', 'text': text, 'at': now()})

@router.post('/conversations/{contact_id}/send')
async def send(contact_id: str, body: SendIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    msg = await deliver(user['company_id'], c, body.text, user['id'], user.get('impersonated_by') or user['name'], assign_to=user['id'])
    if c.get('needs_human'): await db.contacts.update_one({'id': contact_id}, {'$set': {'needs_human': False, 'handoff_urgent': False}})
    if ON_HUMAN_SEND: await ON_HUMAN_SEND(user, c)
    return msg

class AssignIn(BaseModel):
    user_id: Optional[str] = None

@router.post('/conversations/{contact_id}/assign')
async def assign(contact_id: str, body: AssignIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    target = body.user_id
    if target and target != user['id']:
        if user['role'] == 'agent': raise HTTPException(403, 'Só dono e gestor passam conversas para outra pessoa.')
        if not await db.users.find_one({'id': target, 'company_id': user['company_id'], 'active': True}): raise HTTPException(404, 'Pessoa não encontrada.')
    await db.contacts.update_one({'id': contact_id}, {'$set': {'assigned_to': target}})
    who = (await db.users.find_one({'id': target}, {'name': 1}) or {}).get('name') if target else 'ninguém'
    await audit(request, user, f'Passou a conversa de {c["name"]} para {who}')
    return {'ok': True}
