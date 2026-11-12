"""Notificação no celular e no computador (Web Push), sem depender de app de loja.

Cada pessoa liga em "Seu status e avisos" no aparelho que quiser (celular, notebook). O navegador entrega a notificação
mesmo com o Oi Nanda fechado. No iPhone funciona depois de "Adicionar à Tela de Início" (iOS 16.4 ou mais novo).

Avisa quando: chega mensagem de cliente para a pessoa (ou para a fila dela), alguém transfere uma conversa para ela,
a Nanda chama o time e quando um cliente passa do tempo de espera. Se a pessoa está com o Oi Nanda aberto na tela,
o próprio aparelho não mostra a notificação (sw.js decide), para não repetir o que ela já está vendo.

Criptografia: VAPID (RFC 8292) e aes128gcm (RFC 8291), feitas aqui com a biblioteca cryptography.
"""
import os, json, time, base64, logging, asyncio
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from core import db, now, uid, current_user
from whatsapp import aware, decrypt, _fernet

router = APIRouter()
log = logging.getLogger('oinanda.push')
_tasks = set()

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b'=').decode()
def unb64u(s): return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
def raw_pub(pub): return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

_vapid = None
async def vapid():
    """Par de chaves do servidor. Criado sozinho na primeira vez e guardado (a privada, criptografada) no banco."""
    global _vapid
    if _vapid: return _vapid
    d = await db.settings.find_one({'id': 'vapid'}, {'_id': 0})
    if not d:
        k = ec.generate_private_key(ec.SECP256R1())
        pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        d = {'id': 'vapid', 'private_enc': _fernet().encrypt(pem.encode()).decode(), 'public': b64u(raw_pub(k.public_key())), 'created_at': now()}
        try: await db.settings.insert_one(d.copy())
        except Exception: d = await db.settings.find_one({'id': 'vapid'}, {'_id': 0})  # o outro processo criou junto
    key = serialization.load_pem_private_key(decrypt(d['private_enc']).encode(), None)
    _vapid = (key, d['public'])
    return _vapid

def vapid_header(key, public, endpoint):
    u = urlparse(endpoint)
    head = b64u(json.dumps({'typ': 'JWT', 'alg': 'ES256'}).encode())
    claims = b64u(json.dumps({'aud': f'{u.scheme}://{u.netloc}', 'exp': int(time.time()) + 12 * 3600,
                              'sub': 'mailto:' + (os.environ.get('ACME_EMAIL') or 'contato@oinanda.com.br')}).encode())
    r, s = decode_dss_signature(key.sign(f'{head}.{claims}'.encode(), ec.ECDSA(hashes.SHA256())))
    return f'vapid t={head}.{claims}.{b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}, k={public}'

def encrypt(payload: bytes, p256dh: str, auth: str, salt: Optional[bytes] = None, as_key=None):
    """aes128gcm (RFC 8291): só o navegador da pessoa consegue ler."""
    ua_pub = unb64u(p256dh); secret = unb64u(auth)
    as_key = as_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = raw_pub(as_key.public_key())
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, secret, b'WebPush: info\x00' + ua_pub + as_pub).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b'Content-Encoding: aes128gcm\x00').derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b'Content-Encoding: nonce\x00').derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    return salt + (4096).to_bytes(4, 'big') + bytes([len(as_pub)]) + as_pub + body

async def deliver(sub, data: dict):
    """Manda para um aparelho. Devolve False se o aparelho não existe mais (aí a inscrição é apagada)."""
    key, public = await vapid()
    body = encrypt(json.dumps(data, ensure_ascii=False).encode()[:3000], sub['p256dh'], sub['auth'])
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(sub['endpoint'], content=body, headers={'Authorization': vapid_header(key, public, sub['endpoint']), 'Content-Encoding': 'aes128gcm',
                                                                  'Content-Type': 'application/octet-stream', 'TTL': '86400', 'Urgency': 'high'})
    if r.status_code in (404, 410): return False
    if r.status_code >= 400: log.warning('push recusado %s: %s', r.status_code, r.text[:200])
    return True

async def send_user(user_id, title, body, url='/conversas', tag=None):
    subs = await db.push_subs.find({'user_id': user_id}, {'_id': 0}).to_list(20)
    for s in subs:
        try:
            if not await deliver(s, {'title': title, 'body': body[:180], 'url': url, 'tag': tag or 'oinanda'}):
                await db.push_subs.delete_one({'endpoint': s['endpoint']})
        except Exception: log.exception('falha no push')
    return len(subs)

def later(coro):
    """Dispara sem segurar quem chamou (o webhook da Meta precisa responder rápido)."""
    try:
        t = asyncio.get_running_loop().create_task(coro); _tasks.add(t); t.add_done_callback(_tasks.discard)
    except RuntimeError: pass

async def recipients(company_id, contact):
    """Quem deve ser avisado: o responsável; sem responsável, a equipe da conversa; sem equipe, todo mundo disponível."""
    if contact.get('assigned_to'): ids = [contact['assigned_to']]
    elif contact.get('team_id'):
        t = await db.teams.find_one({'id': contact['team_id']}, {'_id': 0, 'member_ids': 1}) or {}
        ids = t.get('member_ids') or []
    else: ids = None
    q = {'company_id': company_id, 'active': True, 'away': {'$ne': True}}
    if ids is not None: q['id'] = {'$in': ids}
    return [u['id'] for u in await db.users.find(q, {'_id': 0, 'id': 1}).to_list(500)]

def short(t): return (t or '').replace('\n', ' ')[:140]

async def on_inbound(company_id, contact_id, text):
    """Chegou mensagem de cliente. Se a Nanda está respondendo sozinha, só avisa quando ela chamar o time."""
    c = await db.contacts.find_one({'id': contact_id}, {'_id': 0})
    if not c: return
    from ai import settings_for
    mode = c.get('ai_mode') or (await settings_for(company_id)).get('default_mode')
    if mode == 'auto' and not c.get('needs_human'): return
    if aware(c.get('push_at')) and now() - aware(c['push_at']) < timedelta(seconds=30): return  # várias mensagens seguidas: um aviso só
    await db.contacts.update_one({'id': contact_id}, {'$set': {'push_at': now()}})
    for u in await recipients(company_id, c):
        await send_user(u, c.get('name') or 'Cliente', short(text), f'/conversas/{contact_id}', 'c-' + contact_id)

async def on_transfer(user_id, contact, by_name, reason):
    await send_user(user_id, f'{(by_name or "Alguém").split(" ")[0]} transferiu {contact.get("name")} para você', short(reason) or 'Toque para abrir a conversa.',
                    f'/conversas/{contact["id"]}', 'c-' + contact['id'])

async def on_handoff(company_id, contact, reason, urgent=False):
    for u in await recipients(company_id, contact):
        await send_user(u, ('🚨 URGENTE: ' if urgent else '') + f'{contact.get("name")} quer falar com alguém do time', short(reason) or 'A Nanda passou a conversa para o time.',
                        f'/conversas/{contact["id"]}', 'c-' + contact['id'])

async def late_tick():
    """Cliente passou do tempo de espera: avisa uma vez por mensagem sem resposta."""
    from inbox import wait_ctx, wait_info
    n = 0
    for company in await db.companies.find({'status': 'active'}, {'_id': 0}).to_list(5000):
        if not await db.push_subs.find_one({'company_id': company['id']}, {'_id': 1}): continue
        ctx = await wait_ctx(company)
        for c in await db.contacts.find({'company_id': company['id'], 'last_dir': 'in', 'last_in_at': {'$gte': now() - timedelta(days=2)}}, {'_id': 0}).to_list(5000):
            wf, lim = wait_info(c, ctx)
            if not wf or now() - wf < timedelta(minutes=lim): continue
            key = aware(c['last_in_at']).isoformat()
            r = await db.contacts.update_one({'id': c['id'], 'late_push_for': {'$ne': key}}, {'$set': {'late_push_for': key}})
            if not r.modified_count: continue  # já avisado (ou o outro processo avisou)
            mins = int((now() - wf).total_seconds() // 60)
            for u in await recipients(company['id'], c):
                await send_user(u, f'{c.get("name")} espera resposta há {mins} min', short(c.get('last_text')), f'/conversas/{c["id"]}', 'c-' + c['id'])
            n += 1
    return n

# ---------- inscrição do aparelho ----------
class SubIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=60)
    device: str = Field(default='', max_length=80)

@router.get('/push/key')
async def key(user=Depends(current_user)):
    return {'public': (await vapid())[1], 'devices': await db.push_subs.count_documents({'user_id': user['id']})}

@router.post('/push/subscribe')
async def subscribe(body: SubIn, user=Depends(current_user)):
    if not body.endpoint.startswith('https://'): raise HTTPException(400, 'Endereço de notificação inválido.')
    await db.push_subs.update_one({'endpoint': body.endpoint}, {'$set': {'user_id': user['id'], 'company_id': user['company_id'], 'p256dh': body.p256dh, 'auth': body.auth,
                                                                        'device': body.device.strip(), 'at': now()}}, upsert=True)
    return {'ok': True, 'devices': await db.push_subs.count_documents({'user_id': user['id']})}

class UnsubIn(BaseModel):
    endpoint: str = Field(max_length=1000)

@router.post('/push/unsubscribe')
async def unsubscribe(body: UnsubIn, user=Depends(current_user)):
    await db.push_subs.delete_one({'endpoint': body.endpoint, 'user_id': user['id']})
    return {'ok': True}

@router.post('/push/test')
async def test(user=Depends(current_user)):
    n = await send_user(user['id'], 'Oi Nanda', 'Pronto! Os avisos vão chegar assim neste aparelho.', '/conversas', 'teste')
    if not n: raise HTTPException(400, 'Nenhum aparelho com notificação ligada.')
    return {'ok': True, 'devices': n}
