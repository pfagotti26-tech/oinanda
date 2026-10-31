"""Etapa 2 — WhatsApp oficial (Cloud API da Meta).

- Cada empresa conecta o próprio número (phone_number_id + WABA). O token fica criptografado no banco.
- Webhook único: /api/wa/webhook. A assinatura da Meta (X-Hub-Signature-256) é conferida com META_APP_SECRET.
  Ao conectar, o número é inscrito no app com override_callback_uri apontando para este webhook, então
  outros sistemas que usem o mesmo app da Meta (ex.: Alta Pulse) continuam recebendo os deles.
- Conversas: contato + mensagens. Atendente vê as dele e as sem dono; dono e gestor veem todas.
- Janela de 24 h: fora dela só sai mensagem por modelo aprovado (chega na etapa de campanhas).

.env: META_APP_SECRET, WA_VERIFY_TOKEN, SECRET_KEY (criptografia), APP_PUBLIC_URL (padrão https://app.oinanda.com.br).
"""
import os, hmac, hashlib, json, logging
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
        r = await c.request(method, GRAPH + path, headers={'Authorization': 'Bearer ' + token}, json=payload, params=params)
    try: data = r.json()
    except ValueError: data = {}
    if r.status_code >= 400:
        err = (data.get('error') or {})
        raise HTTPException(400, 'A Meta recusou: ' + (err.get('error_user_msg') or err.get('message') or f'erro {r.status_code}'))
    return data

def aware(dt):
    if dt is None: return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

# ---------- conexão do número ----------
class ConnectIn(BaseModel):
    phone_number_id: str = Field(min_length=5, max_length=40, pattern=r'^\d+$')
    waba_id: str = Field(min_length=5, max_length=40, pattern=r'^\d+$')
    token: str = Field(min_length=20, max_length=1000)

def number_out(n):
    if not n: return None
    return {k: n.get(k) for k in ('phone_number_id', 'waba_id', 'display_phone', 'verified_name', 'quality', 'status', 'connected_at', 'webhook')}

@router.get('/wa/number')
async def get_number(user=Depends(current_user)):
    return number_out(await db.wa_numbers.find_one({'company_id': user['company_id']}, {'_id': 0}))

@router.post('/wa/connect')
async def connect(body: ConnectIn, request: Request, user=Depends(owners)):
    other = await db.wa_numbers.find_one({'phone_number_id': body.phone_number_id})
    if other and other['company_id'] != user['company_id']: raise HTTPException(409, 'Este número já está conectado em outra conta.')
    info = await graph('GET', f'/{body.phone_number_id}', body.token, params={'fields': 'display_phone_number,verified_name,quality_rating'})
    webhook = 'ok'
    try:
        await graph('POST', f'/{body.waba_id}/subscribed_apps', body.token,
                    {'override_callback_uri': PUBLIC_URL + '/api/wa/webhook', 'verify_token': os.environ.get('WA_VERIFY_TOKEN', '')})
    except HTTPException as e:
        webhook = e.detail  # número conecta mesmo assim; o aviso aparece no painel
        log.warning('inscrição do webhook falhou para %s: %s', body.waba_id, e.detail)
    doc = {'company_id': user['company_id'], 'phone_number_id': body.phone_number_id, 'waba_id': body.waba_id,
           'display_phone': info.get('display_phone_number'), 'verified_name': info.get('verified_name'),
           'quality': info.get('quality_rating'), 'token_enc': encrypt(body.token), 'status': 'connected',
           'webhook': webhook, 'connected_at': now()}
    await db.wa_numbers.update_one({'company_id': user['company_id']}, {'$set': doc}, upsert=True)
    await audit(request, user, 'Conectou o WhatsApp', doc['display_phone'])
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
    names = {c.get('wa_id'): (c.get('profile') or {}).get('name') for c in value.get('contacts') or []}
    for m in value.get('messages') or []:
        if await db.messages.find_one({'wa_msg_id': m.get('id')}): continue
        wa_id = m.get('from')
        at = datetime.fromtimestamp(int(m.get('timestamp', 0)) or now().timestamp(), tz=timezone.utc)
        text = text_of(m)
        contact = await db.contacts.find_one({'company_id': cid, 'wa_id': wa_id}, {'_id': 0})
        if not contact:
            contact = {'id': uid(), 'company_id': cid, 'wa_id': wa_id, 'name': names.get(wa_id) or wa_id, 'assigned_to': None,
                       'unread': 0, 'created_at': at, 'source': 'whatsapp'}
            await db.contacts.insert_one(contact.copy())
        upd = {}
        if not aware(contact.get('last_in_at')) or at >= aware(contact.get('last_in_at')): upd['last_in_at'] = at
        if not aware(contact.get('last_at')) or at >= aware(contact.get('last_at')): upd.update(last_at=at, last_text=text[:200], last_dir='in')
        if names.get(wa_id): upd['name'] = names[wa_id]
        await db.contacts.update_one({'id': contact['id']}, {'$set': upd, '$inc': {'unread': 1}})
        await db.messages.insert_one({'id': uid(), 'company_id': cid, 'contact_id': contact['id'], 'dir': 'in', 'type': m.get('type'),
                                      'text': text, 'wa_msg_id': m.get('id'), 'at': at})
    for s in value.get('statuses') or []:
        upd = {'status': s.get('status')}
        if s.get('errors'): upd['error'] = (s['errors'][0].get('title') or s['errors'][0].get('message') or 'falhou')
        await db.messages.update_one({'wa_msg_id': s.get('id')}, {'$set': upd})

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
            if change.get('field') == 'messages':
                try: await ingest(change.get('value') or {})
                except Exception: log.exception('falha ao processar webhook')
    return {'ok': True}

# ---------- conversas ----------
def can_see(user, contact):
    return user['role'] in ('owner', 'manager') or contact.get('assigned_to') in (None, user['id'])

async def get_contact(user, contact_id):
    c = await db.contacts.find_one({'id': contact_id, 'company_id': user['company_id']}, {'_id': 0})
    if not c or not can_see(user, c): raise HTTPException(404, 'Conversa não encontrada.')
    return c

@router.get('/conversations')
async def conversations(filter: str = 'all', user=Depends(current_user)):
    q = {'company_id': user['company_id']}
    if filter == 'mine': q['assigned_to'] = user['id']
    elif filter == 'unassigned': q['assigned_to'] = None
    elif filter == 'waiting': q['last_dir'] = 'in'
    if user['role'] == 'agent' and filter not in ('mine', 'unassigned'):
        q['assigned_to'] = {'$in': [None, user['id']]}
    rows = await db.contacts.find(q, {'_id': 0}).sort('last_at', -1).to_list(300)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    for r in rows:
        r['assigned_name'] = names.get(r.get('assigned_to'))
        li = aware(r.get('last_in_at'))
        r['window_open'] = bool(li and now() - li < WINDOW)
    return rows

@router.get('/conversations/{contact_id}')
async def conversation(contact_id: str, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    msgs = await db.messages.find({'contact_id': contact_id, 'company_id': user['company_id']}, {'_id': 0}).sort('at', -1).to_list(200)
    if c.get('unread'): await db.contacts.update_one({'id': contact_id}, {'$set': {'unread': 0}})
    li = aware(c.get('last_in_at'))
    c['window_open'] = bool(li and now() - li < WINDOW)
    c['window_ends_at'] = (li + WINDOW) if li else None
    return {'contact': c, 'messages': list(reversed(msgs))}

class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)

@router.post('/conversations/{contact_id}/send')
async def send(contact_id: str, body: SendIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    li = aware(c.get('last_in_at'))
    if not li or now() - li >= WINDOW:
        raise HTTPException(400, 'Passaram 24 horas desde a última mensagem do cliente. A Meta só permite responder com um modelo aprovado (chega na etapa de campanhas).')
    number = await db.wa_numbers.find_one({'company_id': user['company_id']}, {'_id': 0})
    if not number: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    res = await graph('POST', f'/{number["phone_number_id"]}/messages', decrypt(number['token_enc']),
                      {'messaging_product': 'whatsapp', 'to': c['wa_id'], 'type': 'text', 'text': {'body': body.text}})
    msg = {'id': uid(), 'company_id': user['company_id'], 'contact_id': contact_id, 'dir': 'out', 'type': 'text', 'text': body.text,
           'wa_msg_id': ((res.get('messages') or [{}])[0]).get('id'), 'status': 'sent', 'by_id': user['id'],
           'by': user.get('impersonated_by') or user['name'], 'at': now()}
    await db.messages.insert_one(msg.copy())
    upd = {'last_at': msg['at'], 'last_text': body.text[:200], 'last_dir': 'out'}
    if not c.get('assigned_to'): upd['assigned_to'] = user['id']  # quem responde primeiro assume
    await db.contacts.update_one({'id': contact_id}, {'$set': upd})
    msg.pop('_id', None)
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
