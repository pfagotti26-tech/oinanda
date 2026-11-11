"""Direct do Instagram na mesma caixa de conversas do WhatsApp.

Conexão pela API do Instagram com login unificado (a única disponível para apps novos da Meta): o cliente entra com
Instagram ou Facebook, escolhe o portfólio e a conta profissional, e não precisa de Página do Facebook. O Oi Nanda recebe
um token de usuário do sistema do portfólio do cliente e fala com graph.facebook.com usando o ID da conta do Instagram.
Usa o app "Oi Nanda" da Meta (IG_APP_ID / IG_APP_SECRET = ID e chave secreta do app, IG_CONFIG_ID = configuração do
login), gravados no servidor por deploy/set-instagram.sh.

Regras da Meta para o Direct:
- só dá para escrever para quem mandou mensagem antes;
- até 24 h depois da última mensagem do cliente: resposta normal (time ou Nanda);
- de 24 h a 7 dias: só uma pessoa do time responde (marcação HUMAN_AGENT); a Nanda não;
- não existe modelo aprovado: automações e campanhas fora da janela não saem pelo Instagram.

Extras: "comente QUERO" (palavra no comentário de um post ou anúncio manda uma mensagem no Direct, uma vez por
comentário) e respostas a stories e menções viram conversa.
"""
import os, hmac, hashlib, json, logging, secrets, re
from datetime import timedelta, datetime, timezone
from typing import Optional, List
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, owners, managers
from whatsapp import aware, encrypt, decrypt, note, WINDOW

router = APIRouter()
log = logging.getLogger('oinanda.ig')
VER = os.environ.get('IG_API_VERSION', 'v27.0')
GRAPH = 'https://graph.facebook.com/' + VER
# Responder de 24 h a 7 dias (tag HUMAN_AGENT) só depois que a Meta aprovar o recurso "Human Agent": ligue com IG_HUMAN_AGENT=1
def human_window(): return timedelta(days=7) if os.environ.get("IG_HUMAN_AGENT") == "1" else WINDOW
FIELDS = 'messages,messaging_postbacks,messaging_seen,comments'

def ready(): return bool(os.environ.get('IG_APP_ID') and os.environ.get('IG_APP_SECRET') and os.environ.get('IG_CONFIG_ID'))
def app_url(request=None):
    o = (os.environ.get('APP_ORIGINS') or '').split(',')[0].strip()
    return o or (str(request.base_url).rstrip('/') if request else '')
def redirect_uri(request=None): return app_url(request) + '/api/ig/callback'

# ---------- chamadas à Meta (isoladas para os testes) ----------
async def ig(method, path, token=None, params=None, data=None, payload=None, base=None):
    async with httpx.AsyncClient(timeout=20) as c:
        h = {'Authorization': 'Bearer ' + token} if token else {}  # token no cabeçalho, nunca na URL
        r = await c.request(method, (base or GRAPH) + path, params=params or None, data=data, json=payload, headers=h)
    try: d = r.json()
    except ValueError: d = {}
    if r.status_code >= 400:
        e = d.get('error') or {}
        msg = e.get('error_user_msg') or e.get('message') or d.get('error_message') or f'erro {r.status_code}'
        x = HTTPException(400, 'O Instagram recusou: ' + str(msg)[:300])
        x.revoked = e.get('code') == 190
        raise x
    return d

async def account_of(company_id): return await db.ig_accounts.find_one({'company_id': company_id}, {'_id': 0})

# ---------- conectar a conta ----------
@router.get('/ig/status')
async def status(user=Depends(current_user)):
    a = await account_of(user['company_id'])
    pend = None if a else await db.ig_pending.find_one({'company_id': user['company_id']}, {'_id': 0, 'gave_up': 1, 'reason': 1, 'created_at': 1})
    return {'ready': ready(), 'account': {k: a.get(k) for k in ('username', 'name', 'picture', 'connected_at', 'expires_at', 'comment_words', 'comment_dm', 'comment_reply', 'status')} if a else None,
            'pending': ({'gave_up': bool(pend.get('gave_up')), 'reason': pend.get('reason')} if pend else None),
            'page': {'ready': page_ready(a), 'name': (a or {}).get('page_name')} if a else None,
            'redirect_uri': redirect_uri(), 'webhook': app_url() + '/api/ig/webhook'}

@router.get('/ig/connect')
async def connect(user=Depends(owners)):
    if not ready(): raise HTTPException(503, 'O Instagram ainda não foi configurado no servidor.')
    state = secrets.token_urlsafe(24)
    await db.oauth_states.insert_one({'state': state, 'kind': 'ig', 'company_id': user['company_id'], 'user_id': user['id'], 'expires_at': now() + timedelta(minutes=15)})
    q = urlencode({'client_id': os.environ['IG_APP_ID'], 'config_id': os.environ['IG_CONFIG_ID'], 'redirect_uri': redirect_uri(), 'response_type': 'code',
                   'override_default_response_type': 'true', 'state': state, 'prompt': 'login'})
    return {'url': f'https://www.facebook.com/{VER}/dialog/oauth?' + q}

@router.get('/ig/callback')
async def callback(request: Request, code: str = '', state: str = '', error: str = '', error_reason: str = ''):
    back = app_url(request) + '/empresa'
    st = await db.oauth_states.find_one_and_delete({'state': state, 'kind': 'ig'}) if state else None
    if not st or aware(st['expires_at']) < now(): return RedirectResponse(back + '?ig=expirado')
    if error or not code: return RedirectResponse(back + '?ig=cancelado')
    try:
        t = await ig('GET', '/oauth/access_token', params={'client_id': os.environ['IG_APP_ID'], 'client_secret': os.environ['IG_APP_SECRET'],
                                                          'redirect_uri': redirect_uri(request), 'code': code.split('#')[0]})
        tok = t['access_token']
        r = await via_page(st['company_id'], st['user_id'], tok)
        if r: return RedirectResponse(back + '?ig=' + r)
        me = await ig('GET', '/me', tok, {'fields': 'client_business_id'})
        biz = str(me.get('client_business_id') or '')
        exp = now() + timedelta(seconds=int(t['expires_in'])) if t.get('expires_in') else None
        r = await finish(st['company_id'], st['user_id'], tok, biz, exp)
        if r == 'vazio':
            # a Meta às vezes leva um tempo para delegar a conta: guarda a autorização e tenta de novo por alguns minutos
            await db.ig_pending.update_one({'company_id': st['company_id']}, {'$set': {'company_id': st['company_id'], 'user_id': st['user_id'], 'token_enc': encrypt(tok),
                                            'business_id': biz, 'expires_at': exp, 'tries': 0, 'created_at': now()}}, upsert=True)
            from push import later
            later(retry_pending(st['company_id']))
            return RedirectResponse(back + '?ig=aguardando')
        return RedirectResponse(back + '?ig=' + r)
    except HTTPException as e:
        log.warning('falha ao conectar instagram: %s', e.detail)
        return RedirectResponse(back + '?ig=erro')

PAGE_FIELDS = ('messages,messaging_postbacks,message_reactions', 'messages,messaging_postbacks', 'messages')

async def via_page(company_id, user_id, tok):
    """Conexão pelo login do Facebook: o Instagram do cliente fica ligado a uma Página, e o Oi Nanda fala com ele usando o
    token da Página (não vence quando vem de um token longo de usuário). Devolve None se o token não é desse tipo."""
    try:
        long = await ig('GET', '/oauth/access_token', params={'grant_type': 'fb_exchange_token', 'client_id': os.environ['IG_APP_ID'],
                                                               'client_secret': os.environ['IG_APP_SECRET'], 'fb_exchange_token': tok})
        tok = long.get('access_token') or tok
    except HTTPException as e: log.info('instagram: troca por token longo recusada: %s', e.detail)
    try: pages = (await ig('GET', '/me/accounts', tok, {'fields': 'id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}', 'limit': '100'})).get('data') or []
    except HTTPException as e:
        log.info('instagram: /me/accounts recusado (não é login do Facebook?): %s', e.detail); return None
    with_ig = [pg for pg in pages if pg.get('instagram_business_account') and pg.get('access_token')]
    log.info('instagram: login do Facebook da empresa %s: %d página(s), %d com Instagram', company_id, len(pages), len(with_ig))
    if not pages: return None
    if not with_ig: return 'sem_pagina'
    pg = with_ig[0]; acc = pg['instagram_business_account']
    if await db.ig_accounts.find_one({'ig_id': str(acc['id']), 'company_id': {'$ne': company_id}}):
        log.info('instagram: @%s já está em outra empresa', acc.get('username')); return 'outra_empresa'
    sub = None
    for f in PAGE_FIELDS:
        try: await ig('POST', f'/{pg["id"]}/subscribed_apps', pg['access_token'], {'subscribed_fields': f}); sub = f; break
        except HTTPException as e: log.info('instagram: assinatura da página com %s recusada: %s', f, e.detail)
    log.info('instagram: página %s assinada com: %s', pg['id'], sub or 'nada')
    await db.ig_accounts.update_one({'company_id': company_id}, {'$set': {
        'company_id': company_id, 'ig_id': str(acc['id']), 'username': acc.get('username'), 'name': acc.get('name') or pg.get('name'), 'picture': acc.get('profile_picture_url'),
        'page_id': str(pg['id']), 'page_name': pg.get('name'), 'mode': 'page', 'token_enc': encrypt(pg['access_token']), 'expires_at': None,
        'connected_at': now(), 'connected_by': user_id, 'status': 'connected', 'pages_with_ig': len(with_ig)}}, upsert=True)
    await db.ig_pending.delete_one({'company_id': company_id})
    await db.audit.insert_one({'id': uid(), 'company_id': company_id, 'actor_id': user_id, 'action': 'Conectou o Instagram', 'target': '@' + str(acc.get('username')),
                               'at': now(), 'expires_at': now() + timedelta(days=180)})
    return 'ok'

def msg_path(a, ch='instagram'): return '/me/messages' if (ch == 'messenger' or a.get('mode') == 'page') else f'/{a["ig_id"]}/messages'

# O Messenger usa a MESMA conexão: página do Facebook em modo 'page', mesmo token, mesmo /me/messages.
# Muda só quem é o cliente (PSID em vez de IGSID) e onde o id fica na ficha (fb_id em vez de ig_id).
SELF = {'instagram': 'ig_id', 'messenger': 'page_id'}
CID = {'instagram': 'ig_id', 'messenger': 'fb_id'}

def page_ready(a): return bool(a and a.get('mode') == 'page' and a.get('page_id'))

async def messenger_on(company_id):
    from channels import on
    co = await db.companies.find_one({'id': company_id}, {'_id': 0, 'channels': 1}) or {}
    return on(co, 'messenger')

async def finish(company_id, user_id, tok, biz, exp):
    """Acha a conta compartilhada, assina os avisos e grava. Devolve 'ok', 'outra_empresa' ou 'vazio'."""
    accs = await ig_accounts(tok, biz)
    log.info('instagram: conexão da empresa %s, portfólio %s, %d conta(s)', company_id, biz or '-', len(accs))
    if not accs: return 'vazio'
    acc = accs[0]  # o Oi Nanda trabalha com uma conta por empresa
    if await db.ig_accounts.find_one({'ig_id': str(acc['id']), 'company_id': {'$ne': company_id}}):
        log.info('instagram: @%s já está em outra empresa', acc.get('username'))
        return 'outra_empresa'
    await ig('POST', f'/{acc["id"]}/subscribed_apps', tok, {'subscribed_fields': FIELDS})
    await db.ig_accounts.update_one({'company_id': company_id}, {'$set': {
        'company_id': company_id, 'ig_id': str(acc['id']), 'username': acc.get('username'), 'name': acc.get('name'), 'picture': acc.get('profile_picture_url'),
        'business_id': biz, 'config_id': os.environ['IG_CONFIG_ID'], 'token_enc': encrypt(tok), 'expires_at': exp,
        'connected_at': now(), 'connected_by': user_id, 'status': 'connected'}}, upsert=True)
    await db.ig_pending.delete_one({'company_id': company_id})
    await db.audit.insert_one({'id': uid(), 'company_id': company_id, 'actor_id': user_id, 'action': 'Conectou o Instagram', 'target': '@' + str(acc.get('username')),
                               'at': now(), 'expires_at': now() + timedelta(days=180)})
    return 'ok'

PENDING_TRIES = 20  # ~10 min: 6 tentativas rápidas aqui, o resto a cada volta do relógio das automações

async def pending_try(p):
    try: r = await finish(p['company_id'], p['user_id'], decrypt(p['token_enc']), p.get('business_id') or '', p.get('expires_at'))
    except HTTPException as e:
        log.info('instagram: nova tentativa recusada: %s', e.detail); r = 'vazio'
    if r == 'vazio':
        n = (p.get('tries') or 0) + 1
        if n >= PENDING_TRIES:
            await db.ig_pending.update_one({'company_id': p['company_id']}, {'$set': {'tries': n, 'gave_up': True}})
            log.info('instagram: a Meta não liberou nenhuma conta para a empresa %s depois de %d tentativas', p['company_id'], n)
        else: await db.ig_pending.update_one({'company_id': p['company_id']}, {'$set': {'tries': n}})
    elif r == 'outra_empresa': await db.ig_pending.update_one({'company_id': p['company_id']}, {'$set': {'gave_up': True, 'reason': r}})
    return r

async def retry_pending(company_id):
    import asyncio
    for wait in (10, 20, 30, 45, 60, 90):
        await asyncio.sleep(wait)
        p = await db.ig_pending.find_one({'company_id': company_id, 'gave_up': {'$ne': True}}, {'_id': 0})
        if not p or await pending_try(p) != 'vazio': return

async def ig_accounts(tok, biz):
    """Contas do Instagram que o cliente compartilhou. O caminho oficial é /me/assigned_instagram_accounts; se ele
    vier vazio (a Meta às vezes demora a refletir o compartilhamento), tenta pelo portfólio do cliente."""
    tries = [('/me/assigned_instagram_accounts', 'id,username')]
    if biz: tries += [(f'/{biz}/instagram_accounts', 'id,username'), (f'/{biz}/owned_instagram_accounts', 'id,username')]
    for path, fields in tries:
        try: data = (await ig('GET', path, tok, {'fields': fields})).get('data') or []
        except HTTPException as e:
            log.info('instagram: %s recusado: %s', path, e.detail); continue
        log.info('instagram: %s trouxe %d conta(s)', path, len(data))
        if data: return await enrich(tok, data)
    # último recurso: a própria Meta diz quais contas a autorização cobre (granular_scopes do token)
    try:
        dbg = (await ig('GET', '/debug_token', params={'input_token': tok, 'access_token': os.environ['IG_APP_ID'] + '|' + os.environ['IG_APP_SECRET']})).get('data') or {}
        ids = []
        for g in dbg.get('granular_scopes') or []:
            if g.get('scope', '').startswith('instagram_'):
                ids += [str(i) for i in g.get('target_ids') or [] if str(i) not in ids]
        log.info('instagram: debug_token tipo %s, escopos %s, %d conta(s)', dbg.get('type'), ','.join(g.get('scope', '') for g in dbg.get('granular_scopes') or []), len(ids))
        if ids:
            out = []
            for i in ids:
                try: out.append(await ig('GET', f'/{i}', tok, {'fields': 'id,username,name,profile_picture_url'}))
                except HTTPException as e: log.info('instagram: conta %s recusada: %s', i, e.detail)
            return out
    except HTTPException as e: log.info('instagram: debug_token recusado: %s', e.detail)
    return []

async def enrich(tok, data):
    for a in data:
        try: a.update({k: v for k, v in (await ig('GET', f'/{a["id"]}', tok, {'fields': 'name,profile_picture_url'})).items() if k != 'id'})
        except HTTPException: pass
    return data

class ProbeIn(BaseModel):
    ig_id: str = Field(pattern=r'^[0-9]{6,25}$')

@router.post('/ig/probe')
async def probe(body: ProbeIn, user=Depends(owners)):
    """Diagnóstico: com a autorização pendente, tenta a conta pelo número que a Meta mostrou na tela de escolha.
    Se a Meta der acesso, termina a conexão com ela."""
    p = await db.ig_pending.find_one({'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(400, 'Nenhuma conexão esperando.')
    tok = decrypt(p['token_enc']); out = {}
    for fields in ('id,username,name,profile_picture_url', 'id,username', 'id'):
        try: out = await ig('GET', f'/{body.ig_id}', tok, {'fields': fields}); break
        except HTTPException as e: out = {'erro': e.detail}
    log.info('instagram: teste pela conta %s: %s', body.ig_id, 'ok' if 'id' in out else out.get('erro'))
    if 'id' not in out: return {'ok': False, 'detail': out.get('erro')}
    try:
        await ig('POST', f'/{out["id"]}/subscribed_apps', tok, {'subscribed_fields': FIELDS}); sub = 'ok'
    except HTTPException as e: sub = e.detail
    log.info('instagram: assinatura dos avisos da conta %s: %s', out['id'], sub)
    if sub != 'ok': return {'ok': False, 'account': {k: out.get(k) for k in ('id', 'username')}, 'detail': sub}
    await db.ig_accounts.update_one({'company_id': user['company_id']}, {'$set': {
        'company_id': user['company_id'], 'ig_id': str(out['id']), 'username': out.get('username'), 'name': out.get('name'), 'picture': out.get('profile_picture_url'),
        'business_id': p.get('business_id') or '', 'config_id': os.environ['IG_CONFIG_ID'], 'token_enc': p['token_enc'], 'expires_at': p.get('expires_at'),
        'connected_at': now(), 'connected_by': user['id'], 'status': 'connected'}}, upsert=True)
    await db.ig_pending.delete_one({'company_id': user['company_id']})
    return {'ok': True, 'account': {k: out.get(k) for k in ('id', 'username')}}

@router.delete('/ig/account')
async def disconnect(request: Request, user=Depends(owners)):
    a = await account_of(user['company_id'])
    await db.ig_pending.delete_one({'company_id': user['company_id']})
    if a:
        await db.ig_accounts.delete_one({'company_id': user['company_id']})
        await audit(request, user, 'Desconectou o Instagram', '@' + str(a.get('username')))
    return {'ok': True}

class IgSettings(BaseModel):
    comment_words: List[str] = Field(default_factory=list, max_length=10)
    comment_dm: str = Field(default='', max_length=900)
    comment_reply: str = Field(default='', max_length=300)

@router.put('/ig/settings')
async def settings(body: IgSettings, request: Request, user=Depends(managers)):
    if not await account_of(user['company_id']): raise HTTPException(400, 'Conecte o Instagram primeiro.')
    words = [w.strip().lower()[:30] for w in body.comment_words if w.strip()]
    if words and not body.comment_dm.strip(): raise HTTPException(400, 'Escreva a mensagem que vai no Direct de quem comentar.')
    await db.ig_accounts.update_one({'company_id': user['company_id']}, {'$set': {'comment_words': words, 'comment_dm': body.comment_dm.strip(), 'comment_reply': body.comment_reply.strip()}})
    await audit(request, user, 'Alterou a resposta automática a comentários do Instagram', ', '.join(words))
    return {'ok': True}

async def refresh_tokens():
    """O token do login unificado é do portfólio do cliente e, na configuração do Oi Nanda, não vence.
    Se um dia a configuração tiver validade, marca a conta como vencida para o dono reconectar.
    Aproveita a volta para tentar de novo as conexões em que a Meta ainda não tinha liberado a conta."""
    for p in await db.ig_pending.find({'gave_up': {'$ne': True}, 'created_at': {'$gt': now() - timedelta(minutes=30)}}, {'_id': 0}).to_list(100):
        await pending_try(p)
    await db.ig_accounts.update_many({'status': 'connected', 'expires_at': {'$ne': None, '$lt': now()}}, {'$set': {'status': 'expired'}})

async def revoked(a, e):
    if getattr(e, 'revoked', False):
        await db.ig_accounts.update_one({'company_id': a['company_id']}, {'$set': {'status': 'expired'}})

# ---------- receber (webhook) ----------
@router.get('/ig/webhook')
async def verify(request: Request):
    q = request.query_params; tok = os.environ.get('IG_VERIFY_TOKEN', '')
    if q.get('hub.mode') == 'subscribe' and tok and hmac.compare_digest(q.get('hub.verify_token') or '', tok):
        return Response(q.get('hub.challenge', ''), media_type='text/plain')
    raise HTTPException(403, 'Token de verificação inválido.')

@router.post('/ig/webhook')
async def webhook(request: Request):
    raw = await request.body()
    sec = os.environ.get('IG_APP_SECRET', '')
    sig = request.headers.get('x-hub-signature-256', '')
    if not sec or not hmac.compare_digest(sig, 'sha256=' + hmac.new(sec.encode(), raw, hashlib.sha256).hexdigest()):
        raise HTTPException(403, 'Assinatura inválida.')
    try: body = json.loads(raw)
    except ValueError: return {'ok': True}
    page = body.get('object') == 'page'
    for e in body.get('entry') or []:
        try:
            eid = str(e.get('id'))
            a = await db.ig_accounts.find_one({'page_id': eid} if page else {'ig_id': eid}, {'_id': 0})
            if not a: continue
            if page:  # Messenger: mesma página, só entra se a empresa ligou o canal
                if not await messenger_on(a['company_id']): continue
                for m in e.get('messaging') or []: await ingest(a, m, 'messenger')
                continue
            for m in e.get('messaging') or []: await ingest(a, m)
            for ch in e.get('changes') or []:
                if ch.get('field') == 'comments': await on_comment(a, ch.get('value') or {})
        except Exception: log.exception('falha no webhook da meta')
    return {'ok': True}

LABEL = {'image': '📷 Foto', 'video': '🎬 Vídeo', 'audio': '🎤 Áudio', 'file': '📄 Arquivo', 'share': '🔗 Compartilhou uma publicação', 'story_mention': '📣 Mencionou você no story',
         'ig_reel': '🎬 Compartilhou um reel', 'reel': '🎬 Compartilhou um reel', 'ig_post': '🔗 Compartilhou um post', 'like_heart': '❤️'}
KIND = {'image': 'image', 'video': 'video', 'audio': 'audio', 'file': 'document'}

async def profile(a, sid, ch='instagram'):
    f = 'name,profile_pic' if ch == 'messenger' else 'name,username,profile_pic'
    try: return await ig('GET', f'/{sid}', decrypt(a['token_enc']), {'fields': f})
    except HTTPException: return {}

async def upsert(a, sid, username=None, source='instagram', ch='instagram'):
    cid = a['company_id']
    c = await db.contacts.find_one({'company_id': cid, CID[ch]: sid}, {'_id': 0})
    if c: return c, False
    p = await profile(a, sid, ch)
    un = p.get('username') or username
    if ch == 'messenger':
        c = {'id': uid(), 'company_id': cid, 'channel': 'messenger', 'fb_id': sid, 'name': p.get('name') or 'Messenger',
             'ig_pic': p.get('profile_pic'), 'assigned_to': None, 'unread': 0, 'created_at': now(), 'source': source}
    else:
        c = {'id': uid(), 'company_id': cid, 'channel': 'instagram', 'ig_id': sid, 'ig_username': un, 'name': p.get('name') or (('@' + un) if un else 'Instagram'),
             'ig_pic': p.get('profile_pic'), 'assigned_to': None, 'unread': 0, 'created_at': now(), 'source': source}
    await db.contacts.insert_one(c.copy())
    try:
        from origins import origins_of, set_origin
        company = await db.companies.find_one({'id': cid}, {'_id': 0})
        oid = 'instagram' if ch == 'instagram' else 'facebook'
        if oid in {o['id'] for o in await origins_of(company)}:
            await set_origin(company, c, oid, 'Comentário em post' if source == 'instagram_comentario' else 'Direct', 'link')
    except Exception: log.exception('falha ao marcar a origem de %s', ch)
    return c, True

async def ingest(a, m, ch='instagram'):
    cid = a['company_id']
    msg = m.get('message') or {}
    if not msg or msg.get('is_deleted'): return
    mid = msg.get('mid')
    if mid and await db.messages.find_one({'wa_msg_id': mid}): return
    echo = bool(msg.get('is_echo'))
    igsid = str((m.get('recipient') if echo else m.get('sender') or {}).get('id') or '')
    if not igsid or igsid == str(a.get(SELF[ch]) or ''): return
    at = datetime.fromtimestamp(int(m.get('timestamp') or 0) / 1000, tz=timezone.utc) if m.get('timestamp') else now()
    if now() - at < timedelta(seconds=60): at = now()
    contact, _ = await upsert(a, igsid, ch=ch, source=ch)
    att = (msg.get('attachments') or [{}])[0] if msg.get('attachments') else None
    t = (att or {}).get('type')
    text = msg.get('text') or LABEL.get(t, '📎 Anexo' if att else '')
    if att and t not in LABEL:  # tipo novo: guarda o formato para conferir depois
        log.info('anexo do instagram de tipo %s', t)
        try:
            from whatsapp import sample
            await sample('ig_attachment', {'type': t, 'keys': sorted((att.get('payload') or {}).keys())})
        except Exception: pass
    if (msg.get('reply_to') or {}).get('story'): text = '↩️ Respondeu seu story: ' + (msg.get('text') or LABEL.get(t, ''))
    kind = KIND.get(t, 'text')
    doc = {'id': uid(), 'company_id': cid, 'contact_id': contact['id'], 'channel': ch, 'dir': 'out' if echo else 'in', 'type': kind, 'text': text,
           'wa_msg_id': mid, 'at': at, 'received_at': now()}
    url = ((att or {}).get('payload') or {}).get('url')
    if url and kind != 'text': doc.update(media_url=url, pending=not echo)
    elif url: doc['link'] = url  # anexo sem tipo conhecido: pelo menos dá para abrir
    if echo: doc.update(by='Instagram' if ch == 'instagram' else 'Messenger', by_id=None, status='sent')
    await db.messages.insert_one(doc.copy())
    upd = {'last_at': at, 'last_text': text[:200], 'last_dir': doc['dir']}
    if not echo:
        from channels import on_inbound_set, phones_in
        upd['last_in_at'] = at
        upd.update(on_inbound_set(contact, ch, at))
        ph = [p for p in phones_in(msg.get('text')) if p != contact.get('phone_hint_no')] if not contact.get('wa_id') else []
        if ph: upd['phone_hint'] = ph[0]  # mandou o número: a tela oferece salvar como WhatsApp
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd, **({} if echo else {'$inc': {'unread': 1}})})
    import whatsapp
    if echo:
        if whatsapp.ON_HUMAN_SEND: await whatsapp.ON_HUMAN_SEND({'company_id': cid, 'name': 'Alguém pelo ' + ('Instagram' if ch == 'instagram' else 'Messenger'), 'id': None}, contact)
        return
    if url and kind != 'text':
        from media import schedule
        schedule(doc['id'])  # baixa (e transcreve) antes de chamar a Nanda
    elif whatsapp.ON_INBOUND: whatsapp.ON_INBOUND(cid, contact['id'], doc['id'])
    from push import later, on_inbound as push_inbound
    later(push_inbound(cid, contact['id'], text))

def word_hit(words, text):
    t = ' ' + re.sub(r'[^\w#]+', ' ', (text or '').lower()) + ' '
    return any(' ' + w + ' ' in t for w in words)

async def on_comment(a, v):
    """Comentou a palavra-chave num post ou anúncio: manda a mensagem no Direct (uma vez por comentário)."""
    words = a.get('comment_words') or []
    frm = v.get('from') or {}
    if not words or not a.get('comment_dm') or str(frm.get('id')) == a['ig_id']: return
    if not word_hit(words, v.get('text')): return
    cmt = str(v.get('id') or '')
    if not cmt or await db.ig_comments.find_one({'comment_id': cmt}): return
    await db.ig_comments.insert_one({'comment_id': cmt, 'company_id': a['company_id'], 'at': now()})
    tok = decrypt(a['token_enc'])
    contact, _ = await upsert(a, str(frm.get('id')), frm.get('username'), 'instagram_comentario')
    first = (contact.get('name') or '').lstrip('@').split(' ')[0]
    dm = a['comment_dm'].replace('{primeiro_nome}', first).replace('{nome}', contact.get('name') or '')
    try:
        r = await ig('POST', msg_path(a), tok, payload={'recipient': {'comment_id': cmt}, 'message': {'text': dm}})
        if a.get('comment_reply'): await ig('POST', f'/{cmt}/replies', tok, {'message': a['comment_reply']})
    except HTTPException as e:
        await note(a['company_id'], contact['id'], f'Comentou "{(v.get("text") or "")[:80]}", mas o Instagram não deixou mandar o Direct: {e.detail}')
        return
    await note(a['company_id'], contact['id'], f'Comentou "{(v.get("text") or "")[:80]}" num post · a mensagem automática foi para o Direct')
    await db.messages.insert_one({'id': uid(), 'company_id': a['company_id'], 'contact_id': contact['id'], 'channel': 'instagram', 'dir': 'out', 'type': 'text', 'text': dm,
                                  'wa_msg_id': r.get('message_id'), 'status': 'sent', 'by': 'Automação · comentário', 'by_id': None, 'at': now()})
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'last_at': now(), 'last_text': dm[:200], 'last_dir': 'out'}})

# ---------- enviar ----------
def window(contact):
    """(pode_responder_normal, pode_responder_humano)"""
    li = aware(contact.get('last_in_at'))
    if not li: return False, False
    age = now() - li
    return age < WINDOW, age < human_window()

async def send(company_id, contact, message, human, ch='instagram'):
    a = await account_of(company_id)
    nome = 'Instagram' if ch == 'instagram' else 'Messenger'
    if not a or a.get('status') != 'connected': raise HTTPException(400, f'O {nome} da empresa não está conectado.')
    if ch == 'messenger' and not page_ready(a): raise HTTPException(400, 'O Messenger precisa da conexão pela Página do Facebook. Reconecte em Empresa › Instagram.')
    normal, hum = window(contact)
    body = {'recipient': {'id': contact[CID[ch]]}, 'message': message}
    if not normal:
        if not (human and hum):
            raise HTTPException(400, f'No {nome} só dá para responder até 24 h depois da última mensagem do cliente (ou até 7 dias, por uma pessoa do time).')
        body.update(messaging_type='MESSAGE_TAG', tag='HUMAN_AGENT')
    try: return await ig('POST', msg_path(a, ch), decrypt(a['token_enc']), payload=body)
    except HTTPException as e:
        await revoked(a, e); raise

async def deliver(company_id, contact, text, by_id, by_name, assign_to=None, ch='instagram'):
    human = bool(by_id) and '(IA)' not in (by_name or '') and not (by_name or '').startswith(('Automação', 'Campanha'))
    res = await send(company_id, contact, {'text': text[:1000]}, human, ch)
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': contact['id'], 'channel': ch, 'dir': 'out', 'type': 'text', 'text': text,
           'wa_msg_id': res.get('message_id'), 'status': 'sent', 'by_id': by_id, 'by': by_name, 'at': now()}
    await db.messages.insert_one(msg.copy())
    upd = {'last_at': msg['at'], 'last_text': text[:200], 'last_dir': 'out'}
    if assign_to and not contact.get('assigned_to'): upd['assigned_to'] = assign_to
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd})
    msg.pop('_id', None)
    return msg

def media_link(msg_id):
    """Endereço público e assinado de um arquivo, para o Instagram baixar (vale 1 dia)."""
    exp = int((now() + timedelta(days=1)).timestamp())
    sig = hmac.new((os.environ.get('SECRET_KEY') or '').encode(), f'{msg_id}.{exp}'.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{app_url()}/api/media/pub/{msg_id}?e={exp}&s={sig}'

def link_ok(msg_id, e, s):
    try: exp = int(e)
    except ValueError: return False
    if exp < now().timestamp(): return False
    good = hmac.new((os.environ.get('SECRET_KEY') or '').encode(), f'{msg_id}.{exp}'.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, s or '')
