"""Encerrar conversa com motivo e pesquisa de satisfação (0 a 10) pelo WhatsApp."""
import re
from datetime import timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, current_user, managers

router = APIRouter()

REASONS = ['Comprou', 'Agendou', 'Tirou dúvida', 'Sem interesse', 'Parou de responder', 'Outro']
ASK = 'Oi, {primeiro_nome}! Como foi o seu atendimento hoje? Responda com uma nota de 0 a 10. 😊'
THANKS = 'Obrigada pela nota, {primeiro_nome}! Ela ajuda a gente a atender cada vez melhor. 💛'
WAIT_H = 24
NOTE = re.compile(r'^\s*(10|[0-9])(?:\s*(?:/\s*10)?)?(?:[\s,.!-]+(.*))?$', re.S)

def first(c): return ((c.get('name') or '').split(' ') or [''])[0]
def fill(t, c, co): return t.replace('{primeiro_nome}', first(c)).replace('{nome}', c.get('name') or '').replace('{empresa}', co.get('name') or '')

def cfg_of(company):
    s = company.get('closing') or {}
    return {'reasons': s.get('reasons') or REASONS, 'ask_reason': s.get('ask_reason', True),
            'csat': bool(s.get('csat')), 'ask_text': s.get('ask_text') or ASK, 'thanks_text': s.get('thanks_text') or THANKS}

@router.get('/closing/settings')
async def get_settings(user=Depends(current_user)):
    return cfg_of(user['company'])

class SettingsIn(BaseModel):
    reasons: Optional[List[str]] = Field(default=None, max_length=12)
    ask_reason: Optional[bool] = None
    csat: Optional[bool] = None
    ask_text: Optional[str] = Field(default=None, max_length=500)
    thanks_text: Optional[str] = Field(default=None, max_length=500)

@router.put('/closing/settings')
async def put_settings(body: SettingsIn, user=Depends(managers)):
    cur = cfg_of(user['company'])
    d = body.model_dump(exclude_none=True)
    if 'reasons' in d:
        rs = [r.strip()[:40] for r in d['reasons'] if r.strip()]
        if not rs: raise HTTPException(400, 'Deixe pelo menos um motivo.')
        d['reasons'] = list(dict.fromkeys(rs))
    for k in ('ask_text', 'thanks_text'):
        if k in d and not d[k].strip(): d.pop(k)
    new = {**cur, **d}
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'closing': new}})
    return new

async def on_resolve(user, contact, reason: Optional[str], survey: bool):
    """Chamado ao encerrar: guarda o motivo e, se pedido, manda a pesquisa. Devolve o que aconteceu com a pesquisa."""
    from whatsapp import note, deliver
    co = user['company']; cfg = cfg_of(co); who = user.get('impersonated_by') or user['name']
    agent = contact.get('assigned_to') or user['id']
    if reason:
        await db.closes.insert_one({'id': uid(), 'company_id': co['id'], 'contact_id': contact['id'], 'reason': reason[:40],
                                    'by_id': user['id'], 'agent_id': agent, 'at': now()})
        await db.contacts.update_one({'id': contact['id']}, {'$set': {'resolve_reason': reason[:40]}})
        await note(co['id'], contact['id'], f'{who} encerrou: {reason[:40]}')
    if not (survey and cfg['csat']): return None
    try:
        await deliver(co['id'], contact, fill(cfg['ask_text'], contact, co), user['id'], who)
    except HTTPException as e:
        await note(co['id'], contact['id'], f'Pesquisa de satisfação não enviada: {e.detail}')
        return 'falhou'
    except Exception:
        await note(co['id'], contact['id'], 'Pesquisa de satisfação não enviada.')
        return 'falhou'
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'csat_wait': now(), 'csat_agent': agent}})
    await db.csat_sent.insert_one({'company_id': co['id'], 'contact_id': contact['id'], 'at': now()})
    return 'enviada'

async def on_inbound(cid, contact, text):
    """Resposta da pesquisa: guarda a nota e agradece. True quando a mensagem era a nota."""
    from whatsapp import note, deliver
    w = contact.get('csat_wait')
    if not w: return False
    from whatsapp import aware
    if now() - aware(w) > timedelta(hours=WAIT_H):
        await db.contacts.update_one({'id': contact['id']}, {'$unset': {'csat_wait': 1}}); return False
    m = NOTE.match(text or '')
    if not m: return False
    score = int(m.group(1)); comment = (m.group(2) or '').strip()[:500]
    await db.ratings.insert_one({'id': uid(), 'company_id': cid, 'contact_id': contact['id'], 'score': score, 'comment': comment,
                                 'agent_id': contact.get('csat_agent'), 'at': now()})
    await db.contacts.update_one({'id': contact['id']}, {'$unset': {'csat_wait': 1}, '$set': {'last_rating': score}})
    await note(cid, contact['id'], f'Nota do cliente: {score} de 10' + (f' · "{comment}"' if comment else ''))
    co = await db.companies.find_one({'id': cid}, {'_id': 0})
    fresh = await db.contacts.find_one({'id': contact['id']}, {'_id': 0})
    try: await deliver(cid, fresh, fill(cfg_of(co)['thanks_text'], fresh, co), None, 'Oi Nanda')
    except Exception: pass
    return True

@router.get('/reports/closing')
async def report(days: int = 30, user=Depends(managers)):
    cid = user['company_id']; since = now() - timedelta(days=max(1, min(days, 365)))
    rs = await db.ratings.find({'company_id': cid, 'at': {'$gte': since}}, {'_id': 0}).sort('at', -1).to_list(5000)
    cl = await db.closes.find({'company_id': cid, 'at': {'$gte': since}}, {'_id': 0, 'reason': 1}).to_list(20000)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': cid}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    n = len(rs); pro = sum(1 for r in rs if r['score'] >= 9); det = sum(1 for r in rs if r['score'] <= 6)
    by = {}
    for r in rs:
        a = by.setdefault(r.get('agent_id') or '', {'name': names.get(r.get('agent_id'), 'Sem responsável'), 'n': 0, 'sum': 0})
        a['n'] += 1; a['sum'] += r['score']
    reasons = {}
    for c in cl: reasons[c['reason']] = reasons.get(c['reason'], 0) + 1
    cts = {c['id']: c['name'] for c in await db.contacts.find({'id': {'$in': [r['contact_id'] for r in rs[:8]]}}, {'_id': 0, 'id': 1, 'name': 1}).to_list(8)}
    sent = await db.csat_sent.count_documents({'company_id': cid, 'at': {'$gte': since}})
    return {'days': days, 'ratings': n, 'avg': round(sum(r['score'] for r in rs) / n, 1) if n else None,
            'nps': round(100 * (pro - det) / n) if n else None, 'promoters': pro, 'neutral': n - pro - det, 'detractors': det,
            'by_agent': sorted([{'name': v['name'], 'n': v['n'], 'avg': round(v['sum'] / v['n'], 1)} for v in by.values()], key=lambda x: -x['avg']),
            'recent': [{'name': cts.get(r['contact_id'], 'Cliente'), 'score': r['score'], 'comment': r.get('comment') or '', 'at': r['at']} for r in rs[:8]],
            'closes': len(cl), 'reasons': sorted([{'reason': k, 'n': v} for k, v in reasons.items()], key=lambda x: -x['n']), 'sent': sent}
