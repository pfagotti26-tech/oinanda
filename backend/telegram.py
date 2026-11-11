"""Telegram — canal opcional, ligado em Empresa › Mais canais.

Cada empresa usa o próprio bot: cria no @BotFather, cola o token aqui e o Oi Nanda registra o webhook.
O token fica criptografado no banco, igual ao do WhatsApp.

Sem janela de 24 h: dá para escrever quando quiser, desde que a pessoa já tenha falado com o bot uma vez
(regra do próprio Telegram, que não deixa o bot começar conversa).

Webhook: /api/tg/webhook. Quem valida é o segredo que o Telegram devolve no cabeçalho
X-Telegram-Bot-Api-Secret-Token, sorteado por empresa na hora de conectar.
"""
import logging, secrets
from typing import Optional
import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, owners, managers
from whatsapp import encrypt, decrypt, aware

log = logging.getLogger('oinanda.tg')
router = APIRouter()
API = 'https://api.telegram.org/bot'


async def tg(token, method, payload=None):
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f'{API}{token}/{method}', json=payload or {})
    except httpx.HTTPError as e:
        log.warning('telegram %s falhou: %s', method, e)
        raise HTTPException(502, 'O Telegram não respondeu. Tente de novo em alguns instantes.')
    d = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
    if not d.get('ok'):
        desc = (d.get('description') or '')[:200]
        log.info('telegram %s recusado: %s', method, desc)
        raise HTTPException(400, 'O Telegram recusou: ' + (desc or 'token inválido.'))
    return d.get('result')


async def account_of(company_id):
    return await db.tg_accounts.find_one({'company_id': company_id}, {'_id': 0})


async def is_on(company_id):
    from channels import on
    co = await db.companies.find_one({'id': company_id}, {'_id': 0, 'channels': 1}) or {}
    return on(co, 'telegram')


def hook_url():
    import os
    return (os.environ.get('APP_PUBLIC_URL', 'https://app.oinanda.com.br').rstrip('/')) + '/api/tg/webhook'


# ---------- conectar ----------
class ConnectIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@router.post('/tg/connect')
async def connect(body: ConnectIn, request: Request, user=Depends(owners)):
    """Recebe o token do bot do @BotFather, confere com o Telegram e registra o webhook."""
    token = body.token.strip()
    me = await tg(token, 'getMe')
    if not me.get('is_bot'): raise HTTPException(400, 'Esse token não é de um bot. Crie um bot no @BotFather e copie o token que ele manda.')
    other = await db.tg_accounts.find_one({'bot_id': str(me['id']), 'company_id': {'$ne': user['company_id']}})
    if other: raise HTTPException(409, 'Este bot já está em uso por outra conta do Oi Nanda.')
    secret = secrets.token_urlsafe(24)
    await tg(token, 'setWebhook', {'url': hook_url(), 'secret_token': secret, 'allowed_updates': ['message'], 'drop_pending_updates': True})
    await db.tg_accounts.update_one({'company_id': user['company_id']}, {'$set': {
        'company_id': user['company_id'], 'bot_id': str(me['id']), 'username': me.get('username'), 'name': me.get('first_name'),
        'token_enc': encrypt(token), 'secret': secret, 'status': 'connected', 'connected_at': now(), 'connected_by': user['id']}}, upsert=True)
    await audit(request, user, 'Conectou o Telegram', '@' + str(me.get('username')))
    return await status(user)


@router.delete('/tg/account')
async def disconnect(request: Request, user=Depends(owners)):
    a = await account_of(user['company_id'])
    if a:
        try: await tg(decrypt(a['token_enc']), 'deleteWebhook', {'drop_pending_updates': False})
        except HTTPException: pass  # token já revogado no BotFather: segue e limpa daqui
        await db.tg_accounts.delete_one({'company_id': user['company_id']})
        await audit(request, user, 'Desconectou o Telegram', '@' + str(a.get('username') or ''))
    return {'ok': True}


@router.get('/tg/status')
async def status(user=Depends(current_user)):
    a = await account_of(user['company_id'])
    return {'account': {k: a.get(k) for k in ('username', 'name', 'status', 'connected_at')} if a else None,
            'link': f'https://t.me/{a["username"]}' if a and a.get('username') else None,
            'webhook': hook_url()}


# ---------- receber ----------
async def upsert(a, chat, frm):
    cid = a['company_id']
    tid = str(chat.get('id'))
    c = await db.contacts.find_one({'company_id': cid, 'tg_id': tid}, {'_id': 0})
    if c: return c
    nome = ' '.join(x for x in (frm.get('first_name'), frm.get('last_name')) if x).strip()
    un = frm.get('username') or chat.get('username')
    c = {'id': uid(), 'company_id': cid, 'channel': 'telegram', 'tg_id': tid, 'tg_username': un,
         'name': nome or (('@' + un) if un else 'Telegram'), 'assigned_to': None, 'unread': 0, 'created_at': now(), 'source': 'telegram'}
    await db.contacts.insert_one(c.copy())
    return c


LABEL = {'photo': '📷 Foto', 'video': '🎬 Vídeo', 'voice': '🎤 Áudio', 'audio': '🎤 Áudio', 'document': '📄 Arquivo', 'sticker': '🙂 Figurinha', 'location': '📍 Localização'}


@router.post('/tg/webhook')
async def webhook(request: Request):
    secret = request.headers.get('x-telegram-bot-api-secret-token') or ''
    if not secret: raise HTTPException(403, 'Sem o segredo do webhook.')
    a = await db.tg_accounts.find_one({'secret': secret}, {'_id': 0})
    if not a: raise HTTPException(403, 'Segredo do webhook desconhecido.')
    try: body = await request.json()
    except Exception: return {'ok': True}
    m = body.get('message') or {}
    chat, frm = m.get('chat') or {}, m.get('from') or {}
    if not chat.get('id') or frm.get('is_bot') or chat.get('type') != 'private': return {'ok': True}
    if not await is_on(a['company_id']): return {'ok': True}  # canal desligado: nem cria ficha
    try: await ingest(a, m, chat, frm)
    except Exception: log.exception('falha no webhook do telegram')
    return {'ok': True}


async def ingest(a, m, chat, frm):
    from channels import on_inbound_set
    import whatsapp
    cid = a['company_id']
    mid = f'tg{chat["id"]}:{m.get("message_id")}'
    if await db.messages.find_one({'wa_msg_id': mid}): return
    contact = await upsert(a, chat, frm)
    kind, text = 'text', (m.get('text') or m.get('caption') or '')
    for k in ('photo', 'video', 'voice', 'audio', 'document', 'sticker', 'location'):
        if m.get(k):
            kind = {'photo': 'image', 'video': 'video', 'voice': 'audio', 'audio': 'audio', 'document': 'document'}.get(k, 'text')
            text = text or LABEL[k]
            break
    if not text: return
    at = now()
    doc = {'id': uid(), 'company_id': cid, 'contact_id': contact['id'], 'channel': 'telegram', 'dir': 'in', 'type': kind,
           'text': text, 'wa_msg_id': mid, 'at': at, 'received_at': at}
    await db.messages.insert_one(doc.copy())
    upd = {'last_at': at, 'last_text': text[:200], 'last_dir': 'in', 'last_in_at': at}
    upd.update(on_inbound_set(contact, 'telegram', at))
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd, '$inc': {'unread': 1}})
    if whatsapp.ON_INBOUND: whatsapp.ON_INBOUND(cid, contact['id'], doc['id'])
    from push import later, on_inbound as push_inbound
    later(push_inbound(cid, contact['id'], text))


# ---------- enviar ----------
async def deliver(company_id, contact, text, by_id, by_name, assign_to=None):
    a = await account_of(company_id)
    if not a or a.get('status') != 'connected': raise HTTPException(400, 'O Telegram da empresa não está conectado.')
    if not contact.get('tg_id'): raise HTTPException(400, 'Este cliente não tem Telegram.')
    r = await tg(decrypt(a['token_enc']), 'sendMessage', {'chat_id': contact['tg_id'], 'text': text[:4000]})
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': contact['id'], 'channel': 'telegram', 'dir': 'out', 'type': 'text', 'text': text,
           'wa_msg_id': f'tg{contact["tg_id"]}:{r.get("message_id")}', 'status': 'sent', 'by_id': by_id, 'by': by_name, 'at': now()}
    await db.messages.insert_one(msg.copy())
    upd = {'last_at': msg['at'], 'last_text': text[:200], 'last_dir': 'out'}
    if assign_to and not contact.get('assigned_to'): upd['assigned_to'] = assign_to
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd})
    msg.pop('_id', None)
    return msg
