"""Automações de comentário do Instagram: várias regras, cada uma com o post onde vale, as palavras, o Direct e
respostas públicas que alternam. Substitui a regra única antiga (comment_words/comment_dm/comment_reply na conta),
que vira a primeira regra na primeira leitura.

Regra da Meta: uma resposta privada por comentário, até 7 dias depois dele (em Live, só durante a transmissão).
"""
import random, logging
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
    return {'name': body.name.strip() or ('Comentou ' + words[0].upper()), 'scope': body.scope,
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
        out[ru['id']] = {'matched': len(rs), 'sent': len(sent), 'failed': len(rs) - len(sent), 'replied': replied,
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
    return {'items': rules, 'summary': summary}

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
