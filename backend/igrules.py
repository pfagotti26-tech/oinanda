"""Automações de comentário do Instagram: várias regras, cada uma com o post onde vale, as palavras, o Direct e
respostas públicas que alternam. Substitui a regra única antiga (comment_words/comment_dm/comment_reply na conta),
que vira a primeira regra na primeira leitura.

Regra da Meta: uma resposta privada por comentário, até 7 dias depois dele (em Live, só durante a transmissão).
"""
import random, logging, re
from datetime import timedelta
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import aware, decrypt

router = APIRouter()
log = logging.getLogger('oinanda.igrules')
MAX_RULES = 20

class RuleIn(BaseModel):
    name: str = Field(default='', max_length=60)
    scope: Literal['all', 'post', 'next'] = 'all'
    media_id: str = Field(default='', max_length=40)
    media_caption: str = Field(default='', max_length=140)
    media_thumb: str = Field(default='', max_length=1000)
    words: List[str] = Field(default_factory=list, max_length=15)
    dm: str = Field(default='', max_length=900)
    replies: List[str] = Field(default_factory=list, max_length=5)
    link_url: str = Field(default='', max_length=500)
    link_label: str = Field(default='', max_length=20)
    follow_gate: bool = False
    active: Optional[bool] = None

def clean(body: RuleIn):
    words = []
    for w in body.words:
        w = w.strip().lower()[:30]
        if w and w not in words: words.append(w)
    if not words: raise HTTPException(400, 'Coloque pelo menos uma palavra.')
    if not body.dm.strip(): raise HTTPException(400, 'Escreva a mensagem que vai no Direct.')
    if body.scope == 'post' and not body.media_id: raise HTTPException(400, 'Escolha o post.')
    replies = [r.strip()[:300] for r in body.replies if r.strip()]
    url = body.link_url.strip()
    if url and not url.lower().startswith(('http://', 'https://')): url = 'https://' + url
    if url and ('.' not in url.split('//', 1)[1] or ' ' in url): raise HTTPException(400, 'O link não parece certo. Ex.: seusite.com.br/oferta')
    return {'link_url': url, 'link_label': (body.link_label.strip() or 'Quero o link')[:20], 'follow_gate': bool(body.follow_gate and url),'name': body.name.strip() or ('Comentou ' + words[0].upper()), 'scope': body.scope,
            'media_id': body.media_id if body.scope == 'post' else '', 'media_caption': body.media_caption.strip() if body.scope == 'post' else '',
            'media_thumb': body.media_thumb if body.scope == 'post' else '', 'words': words, 'dm': body.dm.strip(), 'replies': replies}

async def migrate(company_id):
    """A regra única antiga vira a primeira automação (uma vez)."""
    a = await db.ig_accounts.find_one({'company_id': company_id}, {'_id': 0})
    if not a or a.get('rules_migrated'): return
    if a.get('comment_words') and a.get('comment_dm'):
        await db.ig_rules.insert_one({'id': uid(), 'company_id': company_id, 'name': 'Comentou ' + a['comment_words'][0].upper(), 'scope': 'all', 'media_id': '',
                                      'media_caption': '', 'media_thumb': '', 'words': a['comment_words'], 'dm': a['comment_dm'],
                                      'replies': [a['comment_reply']] if a.get('comment_reply') else [], 'active': True, 'created_at': now()})
    await db.ig_accounts.update_one({'company_id': company_id}, {'$set': {'rules_migrated': True}})

async def stats(company_id, rules, days=30):
    since = now() - timedelta(days=days)
    rows = await db.ig_comments.find({'company_id': company_id, 'at': {'$gte': since}, 'rule_id': {'$exists': True}}, {'_id': 0}).to_list(20000)
    by = {}
    for r in rows: by.setdefault(r['rule_id'], []).append(r)
    out = {}
    for ru in rules:
        rs = by.get(ru['id'], [])
        sent = [r for r in rs if r.get('status') == 'sent']
        ids = list({r['contact_id'] for r in sent if r.get('contact_id')})
        first = {}
        for r in sent: first[r['contact_id']] = min(first.get(r['contact_id'], r['at']), r['at'])
        replied = 0
        for cid, at in first.items():
            if await db.messages.find_one({'contact_id': cid, 'dir': 'in', 'at': {'$gt': at}}, {'_id': 1}): replied += 1
        sales = await db.sales.find({'company_id': company_id, 'contact_id': {'$in': ids}, 'status': 'ok', 'date': {'$gte': since.strftime('%Y-%m-%d')}}, {'_id': 0, 'total': 1, 'contact_id': 1}).to_list(5000) if ids else []
        clicked = await db.ig_links.count_documents({'rule_id': ru['id'], 'clicks': {'$gt': 0}, 'at': {'$gte': since}})
        out[ru['id']] = {'clicked': clicked, 'matched': len(rs), 'sent': len(sent), 'failed': len(rs) - len(sent), 'replied': replied,
                         'bought': len({s['contact_id'] for s in sales}), 'revenue': round(sum(s['total'] for s in sales), 2)}
    return out

@router.get('/ig/rules')
async def list_rules(user=Depends(current_user)):
    cid = user['company_id']
    await migrate(cid)
    rules = await db.ig_rules.find({'company_id': cid}, {'_id': 0}).sort('created_at', 1).to_list(MAX_RULES)
    st = await stats(cid, rules)
    for r in rules: r['stats'] = st.get(r['id'], {})
    # resumo do Instagram nos últimos 30 dias
    since = now() - timedelta(days=30)
    ig_ids = [c['id'] for c in await db.contacts.find({'company_id': cid, 'source': {'$in': ['instagram', 'instagram_comentario']}}, {'_id': 0, 'id': 1}).to_list(20000)]
    talks = len(await db.messages.distinct('contact_id', {'company_id': cid, 'channel': 'instagram', 'dir': 'in', 'at': {'$gte': since}}))
    sales = await db.sales.find({'company_id': cid, 'contact_id': {'$in': ig_ids}, 'status': 'ok', 'date': {'$gte': since.strftime('%Y-%m-%d')}}, {'_id': 0, 'total': 1}).to_list(5000) if ig_ids else []
    summary = {'answered': sum(s.get('sent', 0) for s in st.values()), 'talks': talks, 'sales': len(sales), 'revenue': round(sum(s['total'] for s in sales), 2)}
    a = await db.ig_accounts.find_one({'company_id': cid}, {'_id': 0, 'welcome': 1}) or {}
    return {'items': rules, 'summary': summary, 'welcome': welcome_of(a)}

@router.post('/ig/rules')
async def add_rule(body: RuleIn, request: Request, user=Depends(managers)):
    cid = user['company_id']
    if not await db.ig_accounts.find_one({'company_id': cid}, {'_id': 1}): raise HTTPException(400, 'Conecte o Instagram primeiro.')
    if await db.ig_rules.count_documents({'company_id': cid}) >= MAX_RULES: raise HTTPException(400, f'Dá para ter até {MAX_RULES} automações de comentário.')
    r = {'id': uid(), 'company_id': cid, **clean(body), 'active': True if body.active is None else body.active, 'created_at': now()}
    await db.ig_rules.insert_one(r.copy())
    await audit(request, user, 'Criou automação de comentário do Instagram', r['name'])
    return {k: v for k, v in r.items() if k != '_id'}

@router.put('/ig/rules/{rid}')
async def edit_rule(rid: str, body: RuleIn, request: Request, user=Depends(managers)):
    r = await db.ig_rules.find_one({'id': rid, 'company_id': user['company_id']}, {'_id': 0})
    if not r: raise HTTPException(404, 'Automação não encontrada.')
    upd = clean(body)
    if body.active is not None: upd['active'] = body.active
    if upd['scope'] == 'next' and r.get('scope') != 'next': upd['created_at'] = now()  # "o próximo post" conta a partir de agora
    await db.ig_rules.update_one({'id': rid}, {'$set': upd})
    await audit(request, user, 'Alterou automação de comentário do Instagram', upd['name'])
    return {**r, **upd}

class ActiveIn(BaseModel):
    active: bool

@router.patch('/ig/rules/{rid}')
async def toggle_rule(rid: str, body: ActiveIn, user=Depends(managers)):
    res = await db.ig_rules.update_one({'id': rid, 'company_id': user['company_id']}, {'$set': {'active': body.active}})
    if not res.matched_count: raise HTTPException(404, 'Automação não encontrada.')
    return {'ok': True}

@router.delete('/ig/rules/{rid}')
async def del_rule(rid: str, request: Request, user=Depends(managers)):
    r = await db.ig_rules.find_one_and_delete({'id': rid, 'company_id': user['company_id']})
    if not r: raise HTTPException(404, 'Automação não encontrada.')
    await audit(request, user, 'Apagou automação de comentário do Instagram', r.get('name', ''))
    return {'ok': True}

@router.get('/ig/media')
async def media(user=Depends(managers)):
    """Últimos posts da conta, para escolher onde a automação vale."""
    from instagram import account_of, ig
    a = await account_of(user['company_id'])
    if not a or a.get('status') != 'connected': raise HTTPException(400, 'Conecte o Instagram primeiro.')
    try:
        r = await ig('GET', f'/{a["ig_id"]}/media', decrypt(a['token_enc']), params={'fields': 'id,caption,media_type,media_url,thumbnail_url,permalink,timestamp', 'limit': 18})
    except HTTPException as e:
        raise HTTPException(400, 'O Instagram não mostrou os posts agora. Tente de novo em instantes.')
    return [{'id': m['id'], 'caption': (m.get('caption') or '')[:140], 'thumb': m.get('thumbnail_url') or m.get('media_url') or '',
             'type': m.get('media_type'), 'link': m.get('permalink'), 'at': m.get('timestamp')} for m in r.get('data', [])]

def hit(words, text):
    from instagram import word_hit
    return word_hit(words, text)

async def pick(a, media_id, text):
    """A primeira regra ligada que vale para este post e tem a palavra."""
    rules = await db.ig_rules.find({'company_id': a['company_id'], 'active': True}, {'_id': 0}).sort('created_at', 1).to_list(MAX_RULES)
    for r in [x for x in rules if x['scope'] == 'post'] + [x for x in rules if x['scope'] == 'next'] + [x for x in rules if x['scope'] == 'all']:
        if not hit(r['words'], text): continue
        if r['scope'] == 'post' and r['media_id'] != media_id: continue
        if r['scope'] == 'next':
            if not media_id: continue
            if r.get('media_id'):
                if r['media_id'] != media_id: continue
            else:
                from instagram import ig
                try: m = await ig('GET', f'/{media_id}', decrypt(a['token_enc']), params={'fields': 'timestamp,caption,thumbnail_url,media_url'})
                except Exception: continue
                from datetime import datetime
                try: ts = datetime.fromisoformat((m.get('timestamp') or '').replace('+0000', '+00:00'))
                except Exception: continue
                if ts < aware(r['created_at']): continue
                await db.ig_rules.update_one({'id': r['id']}, {'$set': {'media_id': media_id, 'media_caption': (m.get('caption') or '')[:140],
                                                                       'media_thumb': m.get('thumbnail_url') or m.get('media_url') or ''}})
        return r
    return None

def reply_of(r):
    return random.choice(r['replies']) if r.get('replies') else ''


# ---------- botão com link, seguir para liberar e boas-vindas ----------
# A resposta privada a um comentário é só texto pela documentação da Meta; tentamos com botão rápido e,
# se a Meta recusar, mandamos só o texto pedindo para responder. Quando a pessoa toca no botão (ou responde),
# abre a janela de 24 h e o link sai num botão, contado pelo nosso endereço curto.
GREET = re.compile(r'^(oi+e?|ol[aá]+|opa|e a[ií]|eai|bom dia|boa tarde|boa noite|hey|hi|hello|tudo bem|td bem|oi tudo bem|ola tudo bem)[\s!?.,😊🙂👋]*$', re.I)
WAIT = timedelta(days=7)

class WelcomeBtn(BaseModel):
    title: str = Field(max_length=20)
    kind: Literal['reply', 'whatsapp', 'human'] = 'reply'
    reply: str = Field(default='', max_length=900)

class WelcomeIn(BaseModel):
    on: bool = False
    text: str = Field(default='', max_length=640)
    buttons: List[WelcomeBtn] = Field(default_factory=list, max_length=4)

WELCOME_TXT = 'Oi {primeiro_nome}! Que bom te ver por aqui 😊 Como posso te ajudar?'
def welcome_of(a):
    w = (a or {}).get('welcome') or {}
    return {'on': bool(w.get('on')), 'text': w.get('text') or WELCOME_TXT,
            'buttons': w.get('buttons') or [{'title': 'Ver preços', 'kind': 'reply', 'reply': ''}, {'title': 'Falar no WhatsApp', 'kind': 'whatsapp', 'reply': ''}, {'title': 'Falar com alguém', 'kind': 'human', 'reply': ''}]}

@router.put('/ig/welcome')
async def put_welcome(body: WelcomeIn, request: Request, user=Depends(managers)):
    cid = user['company_id']
    if not await db.ig_accounts.find_one({'company_id': cid}, {'_id': 1}): raise HTTPException(400, 'Conecte o Instagram primeiro.')
    btns = []
    for b in body.buttons:
        t = b.title.strip()
        if not t: continue
        if b.kind == 'reply' and not b.reply.strip(): raise HTTPException(400, f'Escreva a resposta do botão "{t}".')
        if b.kind == 'whatsapp' and not await db.wa_numbers.find_one({'company_id': cid}, {'_id': 1}): raise HTTPException(400, 'Para o botão do WhatsApp, conecte o WhatsApp primeiro.')
        btns.append({'title': t, 'kind': b.kind, 'reply': b.reply.strip()})
    if body.on and not body.text.strip(): raise HTTPException(400, 'Escreva a mensagem de boas-vindas.')
    w = {'on': body.on, 'text': body.text.strip() or WELCOME_TXT, 'buttons': btns}
    await db.ig_accounts.update_one({'company_id': cid}, {'$set': {'welcome': w}})
    await audit(request, user, 'Alterou as boas-vindas do Instagram', 'ligada' if body.on else 'desligada')
    return w

def first_of(contact):
    f = (contact.get('name') or '').lstrip('@').split(' ')[0]
    return '' if not f or f.isdigit() else f

def fill(t, contact):
    return t.replace('{primeiro_nome}', first_of(contact)).replace('{nome}', contact.get('name') or '').replace('Oi !', 'Oi!').replace('Oi , ', 'Oi, ')

async def out(a, contact, message, by, text_for_log):
    """Manda no Direct (dentro da janela) e registra na conversa."""
    from instagram import ig, msg_path
    r = await ig('POST', msg_path(a), decrypt(a['token_enc']), payload={'recipient': {'id': contact['ig_id']}, 'message': message})
    await db.messages.insert_one({'id': uid(), 'company_id': a['company_id'], 'contact_id': contact['id'], 'channel': 'instagram', 'dir': 'out', 'type': 'text',
                                  'text': text_for_log, 'wa_msg_id': r.get('message_id'), 'status': 'sent', 'by': by, 'by_id': None, 'at': now()})
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'last_at': now(), 'last_text': text_for_log[:200], 'last_dir': 'out'}})
    return r

async def private_reply(a, comment_id, rule, contact, dm):
    """Resposta ao comentário: com botão quando a regra tem link (e a Meta aceita); senão, só texto."""
    from instagram import ig, msg_path
    tok = decrypt(a['token_enc'])
    if rule.get('link_url'):
        try:
            return await ig('POST', msg_path(a), tok, payload={'recipient': {'comment_id': comment_id}, 'message': {'text': dm, 'quick_replies': [
                {'content_type': 'text', 'title': rule.get('link_label') or 'Quero o link', 'payload': 'igr:' + rule['id']}]}}), dm
        except HTTPException:
            dm = dm + '\n\nResponda esta mensagem que eu te mando o link 😉'
            await db.contacts.update_one({'id': contact['id']}, {'$set': {'ig_wait': {'rule_id': rule['id'], 'at': now()}}})
    return await ig('POST', msg_path(a), tok, payload={'recipient': {'comment_id': comment_id}, 'message': {'text': dm}}), dm

async def follows(a, contact):
    from instagram import ig
    try:
        p = await ig('GET', '/' + contact['ig_id'], decrypt(a['token_enc']), params={'fields': 'is_user_follow_business'})
        return p.get('is_user_follow_business')
    except Exception: return None  # sem como saber: não trava a pessoa

async def send_link(a, contact, rule):
    if rule.get('follow_gate') and await follows(a, contact) is False:
        t = f'Falta só um passo: segue a @{a.get("username") or "gente"} e toca no botão de novo que o link chega na hora 😊'
        await out(a, contact, {'text': t, 'quick_replies': [{'content_type': 'text', 'title': 'Já segui', 'payload': 'igr:' + rule['id']}]}, 'Automação · ' + rule['name'], t)
        return 'gate'
    from instagram import app_url
    tk = await db.ig_links.find_one({'rule_id': rule['id'], 'contact_id': contact['id']}, {'_id': 0})
    if not tk:
        tk = {'token': uid()[:10], 'rule_id': rule['id'], 'company_id': a['company_id'], 'contact_id': contact['id'], 'url': rule['link_url'], 'clicks': 0, 'at': now()}
        await db.ig_links.insert_one(tk.copy())
    short = app_url() + '/api/ig/l/' + tk['token']
    label = rule.get('link_label') or 'Abrir o link'
    try:
        await out(a, contact, {'attachment': {'type': 'template', 'payload': {'template_type': 'button', 'text': 'Aqui está 👇', 'buttons': [{'type': 'web_url', 'url': short, 'title': label}]}}},
                  'Automação · ' + rule['name'], f'{label}: {rule["link_url"]}')
    except HTTPException:
        await out(a, contact, {'text': f'Aqui está 👉 {short}'}, 'Automação · ' + rule['name'], f'{label}: {rule["link_url"]}')
    await db.contacts.update_one({'id': contact['id']}, {'$unset': {'ig_wait': ''}})
    return 'sent'

async def wa_link(company_id):
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0, 'display_phone': 1}) or {}
    d = re.sub(r'\D', '', n.get('display_phone') or '')
    return f'https://wa.me/{d}' if d else ''

async def on_message(a, contact, msg, text):
    """Chamado em cada mensagem nova do Direct. Devolve True quando a automação já respondeu (a Nanda não responde por cima)."""
    from whatsapp import note
    pl = ((msg.get('quick_reply') or {}).get('payload') or '')
    try:
        if pl.startswith('igr:') or (contact.get('ig_wait') and now() - aware(contact['ig_wait']['at']) < WAIT and not pl):
            rid = pl[4:] if pl else contact['ig_wait']['rule_id']
            rule = await db.ig_rules.find_one({'id': rid, 'company_id': a['company_id']}, {'_id': 0})
            if rule and rule.get('link_url'):
                await send_link(a, contact, rule)
                return True
        if pl.startswith('igw:'):
            w = welcome_of(a); i = int(pl[4:] or -1)
            b = w['buttons'][i] if 0 <= i < len(w['buttons']) else None
            if not b: return False
            if b['kind'] == 'reply': await out(a, contact, {'text': fill(b['reply'], contact)}, 'Boas-vindas do Instagram', fill(b['reply'], contact)); return True
            if b['kind'] == 'whatsapp':
                link = await wa_link(a['company_id'])
                if link:
                    t = f'É só tocar aqui que a conversa continua no WhatsApp 👉 {link}'
                    await out(a, contact, {'text': t}, 'Boas-vindas do Instagram', t); return True
                return False
            if b['kind'] == 'human':
                t = b['reply'] or 'Combinado! Já chamei alguém do time para falar com você 😊'
                await out(a, contact, {'text': fill(t, contact)}, 'Boas-vindas do Instagram', fill(t, contact))
                await db.contacts.update_one({'id': contact['id']}, {'$set': {'needs_human': True, 'needs_human_at': now(), 'handoff_reason': 'pediu uma pessoa nas boas-vindas do Instagram'}})
                try:
                    from ai import route
                    await route(a['company_id'], contact, 'pediu uma pessoa nas boas-vindas do Instagram')
                except Exception: log.exception('falha ao chamar o time')
                return True
        w = welcome_of(a)
        if w['on'] and not contact.get('ig_welcomed') and GREET.match((text or '').strip()):
            prior = await db.messages.count_documents({'contact_id': contact['id']})
            if prior <= 1:  # é a primeira conversa: chegou só com um oi
                await db.contacts.update_one({'id': contact['id']}, {'$set': {'ig_welcomed': now()}})
                t = fill(w['text'], contact)
                m = {'text': t}
                if w['buttons']: m['quick_replies'] = [{'content_type': 'text', 'title': b['title'][:20], 'payload': f'igw:{i}'} for i, b in enumerate(w['buttons'])]
                await out(a, contact, m, 'Boas-vindas do Instagram', t + (' · ' + ' | '.join(b['title'] for b in w['buttons']) if w['buttons'] else ''))
                return True
    except HTTPException as e:
        await note(a['company_id'], contact['id'], f'A automação do Instagram não conseguiu responder: {e.detail}')
    return False

@router.get('/ig/l/{token}')
async def link_click(token: str):
    from fastapi.responses import RedirectResponse
    tk = await db.ig_links.find_one({'token': token}, {'_id': 0})
    if not tk: raise HTTPException(404, 'Link não encontrado.')
    await db.ig_links.update_one({'token': token}, {'$inc': {'clicks': 1}, '$set': {'last_click': now()}})
    return RedirectResponse(tk['url'], status_code=302)
