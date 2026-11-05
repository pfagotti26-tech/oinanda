"""Etapa 4 — Funil de vendas.

Cada contato é um card no funil. As etapas são da empresa (vêm prontas pelo segmento e o dono ajusta).
Etapas têm um tipo: 'open' (em andamento), 'won' (ganhou) ou 'lost' (perdeu).
Contato sem etapa definida conta como a primeira etapa.
"""
import re
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, note, can_see, aware, WINDOW
from niches import niche

router = APIRouter()

def _st(*rows):
    return [{'id': uid()[:8], 'name': n, 'kind': k} for n, k in rows]

def default_stages(segment):
    return _st(*niche(segment)['stages'])

async def move_stage(company, contact, stage_id, by_name, stages=None, route=True):
    """Muda a etapa do contato, registra na conversa e no histórico de etapas (base dos relatórios)."""
    stages = stages or await stages_for(company)
    st = next((s for s in stages if s['id'] == stage_id), None)
    if not st: raise HTTPException(400, 'Etapa não existe.')
    if contact.get('stage') == stage_id: return False
    upd = {'stage': stage_id, 'stage_at': now()}
    if st['kind'] == 'won': upd['won_at'] = now()
    if st['kind'] == 'lost': upd['lost_at'] = now()
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd})
    await db.stage_events.insert_one({'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'from': contact.get('stage'),
                                      'to': stage_id, 'kind': st['kind'], 'value': contact.get('value'), 'by': by_name, 'at': now()})
    await note(company['id'], contact['id'], f'{by_name} moveu para {st["name"]}')
    contact.update(upd)
    if route:  # etapa ligada a uma equipe: a conversa vai para ela
        from teams import route_by_stage
        await route_by_stage(company, contact, stage_id)
    return True

async def stages_for(company):
    if company.get('stages'): return company['stages']
    st = default_stages(company.get('segment'))
    await db.companies.update_one({'id': company['id'], 'stages': {'$exists': False}}, {'$set': {'stages': st}})
    fresh = await db.companies.find_one({'id': company['id']}, {'_id': 0, 'stages': 1})
    return fresh.get('stages') or st

CARD_FIELDS = {'_id': 0, 'id': 1, 'name': 1, 'wa_id': 1, 'stage': 1, 'value': 1, 'tags': 1, 'assigned_to': 1, 'last_at': 1, 'last_text': 1,
               'last_dir': 1, 'needs_human': 1, 'unread': 1, 'stage_at': 1, 'created_at': 1, 'source': 1, 'last_in_at': 1, 'ad': 1}

@router.get('/funnel')
async def funnel(mine: bool = False, user=Depends(current_user)):
    stages = await stages_for(user['company'])
    ids = [s['id'] for s in stages]
    q = {'company_id': user['company_id']}
    if mine: q['assigned_to'] = user['id']
    elif user['role'] == 'agent': q['assigned_to'] = {'$in': [None, user['id']]}
    rows = await db.contacts.find(q, CARD_FIELDS).sort('last_at', -1).to_list(2000)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    for r in rows:
        if r.get('stage') not in ids: r['stage'] = ids[0]
        r['assigned_name'] = names.get(r.get('assigned_to'))
        li = aware(r.pop('last_in_at', None))
        r['window_open'] = bool(li and now() - li < WINDOW)
    totals = {i: {'count': 0, 'value': 0} for i in ids}
    for r in rows:
        totals[r['stage']]['count'] += 1; totals[r['stage']]['value'] += r.get('value') or 0
    return {'stages': stages, 'cards': rows, 'totals': totals}

class StageIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=40)
    kind: Literal['open', 'won', 'lost'] = 'open'

class StagesIn(BaseModel):
    stages: List[StageIn] = Field(min_length=2, max_length=12)

@router.put('/funnel/stages')
async def put_stages(body: StagesIn, request: Request, user=Depends(managers)):
    old = await stages_for(user['company'])
    new = [{'id': s.id or uid()[:8], 'name': s.name.strip(), 'kind': s.kind} for s in body.stages]
    if not any(s['kind'] == 'open' for s in new): raise HTTPException(400, 'O funil precisa de pelo menos uma etapa em andamento.')
    if len({s['id'] for s in new}) != len(new): raise HTTPException(400, 'Etapas repetidas.')
    gone = [s['id'] for s in old if s['id'] not in {n['id'] for n in new}]
    if gone:  # contatos de etapas apagadas voltam para a primeira
        await db.contacts.update_many({'company_id': user['company_id'], 'stage': {'$in': gone}}, {'$set': {'stage': new[0]['id']}})
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'stages': new}})
    await audit(request, user, 'Editou as etapas do funil', ' → '.join(s['name'] for s in new))
    return {'stages': new}

class ContactPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    stage: Optional[str] = None
    value: Optional[float] = Field(default=None, ge=0, le=100_000_000)
    notes: Optional[str] = Field(default=None, max_length=5000)
    tags: Optional[List[str]] = Field(default=None, max_length=12)
    email: Optional[str] = Field(default=None, max_length=120)
    opt_out: Optional[bool] = None

@router.patch('/contacts/{contact_id}')
async def patch_contact(contact_id: str, body: ContactPatch, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    upd = body.model_dump(exclude_none=True)
    if 'tags' in upd: upd['tags'] = sorted({t.strip()[:24] for t in upd['tags'] if t.strip()})
    if 'stage' in upd:
        if 'value' in upd: c['value'] = upd['value']
        await move_stage(user['company'], c, upd.pop('stage'), user.get('impersonated_by') or user['name'])
    if upd: await db.contacts.update_one({'id': contact_id}, {'$set': upd})
    return await db.contacts.find_one({'id': contact_id}, {'_id': 0})

class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=8, max_length=24)
    value: Optional[float] = Field(default=None, ge=0)
    notes: Optional[str] = Field(default=None, max_length=5000)
    stage: Optional[str] = None

def norm_phone(p):
    d = re.sub(r'\D', '', p)
    if len(d) in (10, 11): d = '55' + d  # número brasileiro sem DDI
    return d

@router.post('/contacts')
async def new_contact(body: ContactIn, request: Request, user=Depends(current_user)):
    wa_id = norm_phone(body.phone)
    if len(wa_id) < 10: raise HTTPException(400, 'Telefone inválido. Use DDD + número.')
    if await db.contacts.find_one({'company_id': user['company_id'], 'wa_id': wa_id}):
        raise HTTPException(409, 'Esse telefone já está no funil.')
    stages = await stages_for(user['company'])
    stage = body.stage if body.stage in {s['id'] for s in stages} else stages[0]['id']
    c = {'id': uid(), 'company_id': user['company_id'], 'wa_id': wa_id, 'name': body.name.strip(), 'assigned_to': user['id'] if user['role'] == 'agent' else None,
         'unread': 0, 'created_at': now(), 'last_at': now(), 'last_text': 'Contato adicionado manualmente', 'last_dir': 'sys', 'source': 'manual',
         'stage': stage, 'stage_at': now(), 'value': body.value, 'notes': body.notes}
    await db.contacts.insert_one(c.copy())
    await audit(request, user, 'Adicionou contato', c['name'])
    return {k: v for k, v in c.items() if k != '_id'}

@router.get('/funnel/summary')
async def summary(user=Depends(current_user)):
    """Números do mês para a tela inicial."""
    stages = await stages_for(user['company'])
    won = [s['id'] for s in stages if s['kind'] == 'won']
    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = {'company_id': user['company_id']}
    new = await db.contacts.count_documents({**q, 'created_at': {'$gte': start}})
    won_rows = await db.contacts.find({**q, 'stage': {'$in': won}, 'won_at': {'$gte': start}}, {'_id': 0, 'value': 1}).to_list(5000)
    waiting = await db.contacts.count_documents({**q, 'last_dir': 'in'})
    human = await db.contacts.count_documents({**q, 'needs_human': True})
    return {'new_contacts': new, 'won': len(won_rows), 'won_value': sum(r.get('value') or 0 for r in won_rows), 'waiting': waiting, 'needs_human': human}

@router.get('/funnel/stages')
async def get_stages(user=Depends(current_user)):
    return await stages_for(user['company'])
