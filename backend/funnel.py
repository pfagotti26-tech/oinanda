"""Etapa 4 — Funil de vendas.

Cada contato é um card no funil. As etapas são da empresa (vêm prontas pelo segmento e o dono ajusta).
Etapas têm um tipo: 'open' (em andamento), 'won' (ganhou) ou 'lost' (perdeu).
Contato sem etapa definida conta como a primeira etapa.
"""
import re
from datetime import timezone
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

async def move_stage(company, contact, stage_id, by_name, stages=None, route=True, quiet=False):
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
    if not quiet: await note(company['id'], contact['id'], f'{by_name} moveu para {st["name"]}')
    contact.update(upd)
    if route:  # etapa ligada a uma equipe: a conversa vai para ela
        from teams import route_by_stage
        await route_by_stage(company, contact, stage_id)
    return True

AUTO_RX = re.compile(r'convers|atendimento|interessad|qualificad', re.I)

def auto_stage_of(company, stages):
    """Etapa para onde vai o contato novo quando o time responde. Sem escolha salva: a segunda etapa, se o nome dela for de conversa."""
    v = company.get('auto_stage')
    opn = [s for s in stages if s['kind'] == 'open']
    if v == '': return None
    if v and any(s['id'] == v for s in opn[1:]): return v
    if v is None and len(opn) > 1 and AUTO_RX.search(opn[1]['name']): return opn[1]['id']
    return None

async def on_team_reply(company_id, contact):
    """Alguém do time respondeu um contato que ainda está na primeira etapa: o card anda sozinho."""
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if not company: return
    stages = await stages_for(company)
    tgt = auto_stage_of(company, stages)
    c = await db.contacts.find_one({'id': contact['id']}, {'_id': 0})
    if tgt and c and c.get('stage') in (None, stages[0]['id']):
        await move_stage(company, c, tgt, 'Automático (o time respondeu)', stages, quiet=True)  # fica no histórico de etapas, sem poluir a conversa

async def stages_for(company):
    if company.get('stages'): return company['stages']
    st = default_stages(company.get('segment'))
    await db.companies.update_one({'id': company['id'], 'stages': {'$exists': False}}, {'$set': {'stages': st}})
    fresh = await db.companies.find_one({'id': company['id']}, {'_id': 0, 'stages': 1})
    return fresh.get('stages') or st

CARD_FIELDS = {'_id': 0, 'id': 1, 'name': 1, 'wa_id': 1, 'stage': 1, 'value': 1, 'tags': 1, 'assigned_to': 1, 'last_at': 1, 'last_text': 1,
               'last_dir': 1, 'needs_human': 1, 'unread': 1, 'stage_at': 1, 'created_at': 1, 'source': 1, 'last_in_at': 1, 'ad': 1,
               'last_by': 1, 'channel': 1, 'handoff_urgent': 1, 'resolved_at': 1, 'noreply_at': 1}

async def settle_won(company, stages=None, contact_id=None):
    """Quem pagou antes do horário vai para a etapa de venda quando o atendimento passa (ou é marcado como feito)."""
    stages = stages or await stages_for(company)
    won = next((s for s in stages if s['kind'] == 'won'), None)
    if not won: return
    q = {'company_id': company['id'], 'won_pending': True, **({'id': contact_id} if contact_id else {})}
    for c in await db.contacts.find(q, {'_id': 0}).to_list(500):
        left = await db.appointments.find_one({'company_id': company['id'], 'contact_id': c['id'], 'status': {'$in': ['scheduled', 'confirmed']}, 'start': {'$gt': now()}}, {'_id': 1})
        if left: continue
        await db.contacts.update_one({'id': c['id']}, {'$unset': {'won_pending': 1}})
        if c.get('stage') != won['id']: await move_stage(company, c, won['id'], 'Oi Nanda', stages)

@router.get('/funnel')
async def funnel(mine: bool = False, user=Depends(current_user)):
    stages = await stages_for(user['company'])
    await settle_won(user['company'], stages)
    ids = [s['id'] for s in stages]
    q = {'company_id': user['company_id']}
    if mine: q['assigned_to'] = user['id']
    elif user['role'] == 'agent': q['assigned_to'] = {'$in': [None, user['id']]}
    from wallet import BASE_FIELDS, in_base, own_numbers
    own = await own_numbers(user['company'])
    from wallet import cold_import
    rows = [r for r in await db.contacts.find(q, {**CARD_FIELDS, **BASE_FIELDS}).sort('last_at', -1).to_list(5000) if in_base(r, own) and not cold_import(r)][:2000]
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    for r in rows:
        if r.get('stage') not in ids: r['stage'] = ids[0]
        r['assigned_name'] = names.get(r.get('assigned_to'))
        from inbox import waiting
        r['waiting'] = waiting(r)
        r['bought'] = bool(r.get('sales_count'))  # valor do card = o que já comprou, não negociação
        for k in ('resolved_at', 'noreply_at', 'internal', 'sales_count', 'wa_last_in_at', 'ig_last_in_at', 'fb_last_in_at', 'tg_last_in_at', 'em_last_in_at'): r.pop(k, None)
        li = aware(r.pop('last_in_at', None))
        from channels import window_of
        w = window_of(r.get('channel'))
        r['window_open'] = True if w is None else bool(li and now() - li < w)
    totals = {i: {'count': 0, 'value': 0} for i in ids}
    kind = {s['id']: s['kind'] for s in stages}
    for r in rows:
        totals[r['stage']]['count'] += 1
        if not (r['bought'] and kind.get(r['stage']) == 'open'): totals[r['stage']]['value'] += r.get('value') or 0
    return {'stages': stages, 'cards': rows, 'totals': totals}

class StageIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=40)
    kind: Literal['open', 'won', 'lost'] = 'open'

class StagesIn(BaseModel):
    stages: List[StageIn] = Field(min_length=2, max_length=12)
    auto_stage: Optional[str] = Field(default=None, max_length=40)  # '' = não mover sozinho

@router.put('/funnel/stages')
async def put_stages(body: StagesIn, request: Request, user=Depends(managers)):
    old = await stages_for(user['company'])
    new = [{'id': s.id or uid()[:8], 'name': s.name.strip(), 'kind': s.kind} for s in body.stages]
    if not any(s['kind'] == 'open' for s in new): raise HTTPException(400, 'O funil precisa de pelo menos uma etapa em andamento.')
    if len({s['id'] for s in new}) != len(new): raise HTTPException(400, 'Etapas repetidas.')
    gone = [s['id'] for s in old if s['id'] not in {n['id'] for n in new}]
    if gone:  # contatos de etapas apagadas voltam para a primeira
        await db.contacts.update_many({'company_id': user['company_id'], 'stage': {'$in': gone}}, {'$set': {'stage': new[0]['id']}})
    st = {'stages': new}
    if body.auto_stage is not None: st['auto_stage'] = body.auto_stage if any(s['id'] == body.auto_stage and s['kind'] == 'open' for s in new[1:]) else ''
    await db.companies.update_one({'id': user['company_id']}, {'$set': st})
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
    internal: Optional[bool] = None
    bot_ok: Optional[bool] = None  # "é cliente sim": não sugerir de novo que é mensagem automática

@router.patch('/contacts/{contact_id}')
async def patch_contact(contact_id: str, body: ContactPatch, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    upd = body.model_dump(exclude_none=True)
    if 'tags' in upd: upd['tags'] = sorted({t.strip()[:24] for t in upd['tags'] if t.strip()})
    if 'stage' in upd:
        if 'value' in upd: c['value'] = upd['value']
        sid = upd.pop('stage')
        moved = await move_stage(user['company'], c, sid, user.get('impersonated_by') or user['name'])
        won = any(s['id'] == sid and s['kind'] == 'won' for s in await stages_for(user['company']))
        if moved and won and not user.get('impersonated_by'): upd['won_by_id'] = user['id']  # quem fechou: a venda conta para ele no relatório se o card não tiver responsável
    if upd: await db.contacts.update_one({'id': contact_id}, {'$set': upd})
    if 'stage' in (body.model_dump(exclude_none=True)) and (upd.get('value') or c.get('value')):
        from sales import migrate_legacy
        await migrate_legacy(user['company'])  # card fechado com valor vira venda na hora, não só quando alguém abre Vendas
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
    from wallet import BASE_FIELDS, in_base, own_numbers
    from inbox import waiting as is_waiting
    own = await own_numbers(user['company'])
    F = {'_id': 0, 'id': 1, 'value': 1, 'stage': 1, 'won_at': 1, 'created_at': 1, 'assigned_to': 1, 'last_dir': 1, 'resolved_at': 1, 'noreply_at': 1, 'needs_human': 1, **BASE_FIELDS}
    rows = [c for c in await db.contacts.find({**q, '$or': [{'created_at': {'$gte': start}}, {'won_at': {'$gte': start}}, {'last_dir': 'in'}, {'needs_human': True}]}, F).to_list(20000) if in_base(c, own)]
    new = sum(1 for c in rows if aware(c.get('created_at')) and aware(c['created_at']) >= start and c.get('source') != 'importado')
    # vendas do mês: as vendas registradas (as canceladas não contam); cards ganhos sem venda registrada entram pelo valor do card
    mine = user['role'] == 'agent'  # atendente vê só o que ela vendeu; o faturamento da empresa é do dono e dos gestores
    sales_ok = await db.sales.find({**q, 'status': 'ok', 'imported': {'$ne': True}, 'date': {'$gte': start}, **({'sellers.user_id': user['id']} if mine else {})}, {'_id': 0, 'contact_id': 1, 'total': 1}).to_list(20000)
    keep = {c['id'] for c in rows}
    ext = {c['contact_id'] for c in sales_ok}
    base_ids = {c['id'] for c in await db.contacts.find({'id': {'$in': list(ext)}}, {'_id': 0, 'id': 1, **BASE_FIELDS}).to_list(20000) if in_base(c, own)}
    won_rows = [{'value': s.get('total') or 0} for s in sales_ok if s['contact_id'] in base_ids]
    with_sale = {s['contact_id'] for s in sales_ok}
    won_rows += [c for c in rows if c.get('stage') in won and aware(c.get('won_at')) and aware(c['won_at']) >= start and c['id'] not in with_sale and not c.get('sales_count') and (not mine or c.get('assigned_to') == user['id'])]
    waiting = sum(1 for c in rows if is_waiting(c) and not c.get('needs_human'))  # quem pediu atendente aparece no aviso de cima
    human = sum(1 for c in rows if c.get('needs_human'))
    # o que a Nanda fez no mês: clientes atendidos, respostas fora do horário comercial, horários que ela marcou
    from agenda import tz_of
    tz = tz_of(user['company'])
    ia = {**q, 'dir': 'out', 'by': {'$regex': r'\(IA\)$'}, 'at': {'$gte': start}}
    outs = await db.messages.find(ia, {'_id': 0, 'at': 1, 'contact_id': 1}).to_list(50000)
    def late(d):
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        h = d.astimezone(tz); return h.hour < 8 or h.hour >= 20 or h.weekday() == 6
    appts = await db.appointments.count_documents({**q, 'by': {'$regex': r'\(IA\)$'}, 'created_at': {'$gte': start}})
    return {'new_contacts': new, 'won': len(won_rows), 'won_value': sum(r.get('value') or 0 for r in won_rows), 'waiting': waiting, 'needs_human': human,
            'nanda': {'people': len({m['contact_id'] for m in outs}), 'replies': len(outs), 'after_hours': sum(1 for m in outs if late(m['at'])), 'appts': appts}}

@router.get('/funnel/stages')
async def get_stages(user=Depends(current_user)):
    return await stages_for(user['company'])

class NotClientIn(BaseModel):
    ids: List[str] = Field(min_length=1, max_length=300)
    value: bool = True

@router.post('/contacts/not-client')
async def not_client(body: NotClientIn, request: Request, user=Depends(managers)):
    """Tira vários contatos da base de vendas de uma vez (time, teste ou mensagem automática): não contam como prospect nem cliente."""
    r = await db.contacts.update_many({'company_id': user['company_id'], 'id': {'$in': body.ids}}, {'$set': {'internal': body.value}})
    await audit(request, user, 'Tirou da base de vendas' if body.value else 'Voltou para a base de vendas', f'{r.modified_count} contato' + ('' if r.modified_count == 1 else 's'))
    return {'updated': r.modified_count}

@router.get('/funnel/auto-stage')
async def get_auto(user=Depends(current_user)):
    company = await db.companies.find_one({'id': user['company_id']}, {'_id': 0})
    return {'stage': auto_stage_of(company, await stages_for(company))}
