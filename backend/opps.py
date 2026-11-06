"""Funil de pós-venda: oportunidades de renovação, próximo produto (upsell) e reativação.

Separado do funil de vendas, para não misturar cliente novo com quem já é cliente. Cada oportunidade é um card
ligado a um cliente (e, se for o caso, a um item). Nascem em massa a partir de um público (segmento) e são
distribuídas para o time. Quando sai uma venda daquele item para o cliente, o card vai sozinho para "Renovou / comprou".
A coluna "A contatar" de cada pessoa é a lista de trabalho do dia.
"""
from datetime import timedelta
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import aware, note
from wallet import Rule, run_segment, state_of

router = APIRouter()
KINDS = {'renovacao': 'Renovação', 'upsell': 'Próximo produto', 'reativacao': 'Reativação'}
DEFAULT_STAGES = [('todo', 'A contatar', 'open'), ('talking', 'Em conversa', 'open'), ('won', 'Renovou / comprou', 'won'), ('lost', 'Não quis', 'lost')]

def stages_of(company):
    return company.get('post_stages') or [{'id': i, 'name': n, 'kind': k} for i, n, k in DEFAULT_STAGES]

def visible(user):
    q = {'company_id': user['company_id']}
    if user['role'] == 'agent': q['assigned_to'] = {'$in': [None, user['id']]}
    return q

@router.get('/opps')
async def list_opps(mine: bool = False, kind: str = '', user=Depends(current_user)):
    q = visible(user)
    if mine: q['assigned_to'] = user['id']
    if kind: q['kind'] = kind
    q['$or'] = [{'closed_at': None}, {'closed_at': {'$gte': now() - timedelta(days=30)}}]  # fechadas somem depois de 30 dias
    rows = await db.opps.find(q, {'_id': 0}).sort('due_at', 1).to_list(3000)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    for r in rows: r['owner'] = names.get(r.get('assigned_to'))
    stg = stages_of(user['company'])
    tot = {s['id']: {'count': 0, 'value': 0.0} for s in stg}
    for r in rows:
        if r['stage_id'] in tot: tot[r['stage_id']]['count'] += 1; tot[r['stage_id']]['value'] += r.get('value') or 0
    return {'stages': stg, 'items': rows, 'totals': tot, 'kinds': KINDS}

class BulkIn(BaseModel):
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    kind: Literal['renovacao', 'upsell', 'reativacao'] = 'renovacao'
    product_id: Optional[str] = None
    assign: Literal['owner', 'team', 'user', 'none'] = 'owner'
    team_id: Optional[str] = None
    user_id: Optional[str] = None
    note: str = Field(default='', max_length=300)

@router.post('/opps/bulk')
async def bulk(body: BulkIn, request: Request, user=Depends(managers)):
    from billing import require; require(user, 'posvenda')
    rows = await run_segment(user, body.rules)
    if not rows: raise HTTPException(400, 'Ninguém neste público.')
    prod = await db.products.find_one({'id': body.product_id, 'company_id': user['company_id']}, {'_id': 0}) if body.product_id else None
    if body.product_id and not prod: raise HTTPException(404, 'Item não encontrado.')
    team = await db.teams.find_one({'id': body.team_id, 'company_id': user['company_id']}, {'_id': 0}) if body.assign == 'team' else None
    if body.assign == 'team' and not team: raise HTTPException(404, 'Equipe não encontrada.')
    if body.assign == 'user' and not await db.users.find_one({'id': body.user_id, 'company_id': user['company_id'], 'active': True}): raise HTTPException(404, 'Pessoa não encontrada.')
    from teams import pick_member
    products = {p['id']: p for p in await db.products.find({'company_id': user['company_id']}, {'_id': 0}).to_list(2000)}
    made = skipped = 0
    for c in rows:
        hs = c.get('holdings') or []
        h = next((x for x in hs if x.get('product_id') == body.product_id), None) if body.product_id else \
            min((x for x in hs if x.get('expires_at')), key=lambda x: aware(x['expires_at']), default=None)
        pid = body.product_id or (h or {}).get('product_id')
        if await db.opps.find_one({'company_id': user['company_id'], 'contact_id': c['id'], 'kind': body.kind, 'product_id': pid, 'closed_at': None}):
            skipped += 1; continue
        p = products.get(pid) or {}
        owner = c.get('assigned_to') if body.assign == 'owner' else (await pick_member(team) if body.assign == 'team' else body.user_id if body.assign == 'user' else None)
        await db.opps.insert_one({'id': uid(), 'company_id': user['company_id'], 'contact_id': c['id'], 'contact_name': c['name'], 'kind': body.kind,
                                  'product_id': pid, 'product_name': p.get('name') or (h or {}).get('name'), 'value': p.get('price'),
                                  'due_at': (h or {}).get('expires_at') if body.kind == 'renovacao' else None, 'stage_id': 'todo', 'assigned_to': owner,
                                  'team_id': team['id'] if team else None, 'note': body.note.strip(), 'created_at': now(), 'by': user['name'], 'closed_at': None})
        made += 1
    await audit(request, user, 'Criou oportunidades de pós-venda', f'{KINDS[body.kind]} · {made}')
    return {'ok': True, 'created': made, 'skipped': skipped}

class OppPatch(BaseModel):
    stage_id: Optional[str] = None
    assigned_to: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=500)
    unassign: bool = False

@router.patch('/opps/{oid}')
async def patch(oid: str, body: OppPatch, user=Depends(current_user)):
    o = await db.opps.find_one({**visible(user), 'id': oid}, {'_id': 0})
    if not o: raise HTTPException(404, 'Oportunidade não encontrada.')
    upd = {}
    stg = {s['id']: s for s in stages_of(user['company'])}
    if body.stage_id:
        if body.stage_id not in stg: raise HTTPException(400, 'Etapa não existe.')
        upd.update(stage_id=body.stage_id, closed_at=now() if stg[body.stage_id]['kind'] != 'open' else None)
        await note(user['company_id'], o['contact_id'], f'{user.get("impersonated_by") or user["name"]} moveu {KINDS[o["kind"]].lower()}{" de " + o["product_name"] if o.get("product_name") else ""} para "{stg[body.stage_id]["name"]}"')
    if body.assigned_to or body.unassign:
        if user['role'] == 'agent' and body.assigned_to not in (None, user['id']): raise HTTPException(403, 'Só dono e gestor passam para outra pessoa.')
        upd['assigned_to'] = None if body.unassign else body.assigned_to
    if body.note is not None: upd['note'] = body.note.strip()
    if upd: await db.opps.update_one({'id': oid}, {'$set': upd})
    return {**o, **upd}

async def on_sale(company_id, contact_id, sale):
    """Venda registrada: oportunidade aberta daquele item (ou sem item) vai para 'Renovou / comprou'."""
    pids = {i.get('product_id') for i in sale['items']}
    for o in await db.opps.find({'company_id': company_id, 'contact_id': contact_id, 'closed_at': None}, {'_id': 0}).to_list(50):
        if o.get('product_id') in pids or not o.get('product_id'):
            await db.opps.update_one({'id': o['id']}, {'$set': {'stage_id': 'won', 'closed_at': now(), 'won_value': sale['total'], 'sale_id': sale['id']}})

# ---------- renovação e upsell ----------
def terms_of(sales, products):
    """Cada compra de item com validade vira um período. Renovou = comprou de novo até 30 dias depois do fim."""
    out, cur = [], {}
    for s in sorted(sales, key=lambda x: aware(x['date'])):
        d = aware(s['date'])
        for i in s['items']:
            p = products.get(i.get('product_id')) or {}
            if not p.get('duration_days'): continue
            k = (s['contact_id'], i['product_id'])
            prev = cur.get(k)
            if prev and d <= prev['end'] + timedelta(days=30): prev['renewed'] = True
            start = max(d, prev['end']) if prev and prev['end'] > d else d
            t = {'contact_id': s['contact_id'], 'product_id': i['product_id'], 'name': p['name'], 'end': start + timedelta(days=p['duration_days'] * max(1, int(round(i.get('qty') or 1)))),
                 'value': i.get('total') or 0, 'renewed': False}
            out.append(t); cur[k] = t
    return out

@router.get('/reports/retention')
async def retention(start: Optional[str] = None, end: Optional[str] = None, user=Depends(managers)):
    from sales import period, Filters
    company = user['company']
    s0, s1, t0, t1 = period(company, Filters(start=start, end=end))
    products = {p['id']: p for p in await db.products.find({'company_id': user['company_id']}, {'_id': 0}).to_list(2000)}
    sales = await db.sales.find({'company_id': user['company_id'], 'status': 'ok'}, {'_id': 0, 'id': 1, 'contact_id': 1, 'date': 1, 'items': 1, 'total': 1}).to_list(100000)
    terms = terms_of(sales, products)
    t = now()
    due = [x for x in terms if t0 <= x['end'] < min(t1, t)]
    by = {}
    for x in due:
        b = by.setdefault(x['product_id'], {'name': x['name'], 'due': 0, 'renewed': 0, 'lost_value': 0.0})
        b['due'] += 1
        if x['renewed']: b['renewed'] += 1
        else: b['lost_value'] += x['value']
    for b in by.values(): b['rate'] = round(100 * b['renewed'] / b['due'], 1) if b['due'] else None; b['lost_value'] = round(b['lost_value'], 2)
    upcoming = {}
    last = {}
    for x in terms: last[(x['contact_id'], x['product_id'])] = x
    for x in last.values():
        if x['end'] >= t:
            for n in (30, 60, 90):
                if x['end'] <= t + timedelta(days=n): upcoming[n] = upcoming.get(n, {'count': 0, 'value': 0.0}); upcoming[n]['count'] += 1; upcoming[n]['value'] += x['value']
    for n in (30, 60, 90):
        u = upcoming.setdefault(n, {'count': 0, 'value': 0.0}); u['value'] = round(u['value'], 2)
    # upsell: de quem comprou A, quantos compraram depois um dos próximos de A
    firsts = {}
    for s in sorted(sales, key=lambda x: aware(x['date'])):
        for i in s['items']:
            if i.get('product_id'): firsts.setdefault((s['contact_id'], i['product_id']), aware(s['date']))
    ups = []
    for p in products.values():
        if not p.get('next_ids'): continue
        buyers = [cid for (cid, pid) in firsts if pid == p['id']]
        conv = [cid for cid in buyers if any((cid, n) in firsts and firsts[(cid, n)] >= firsts[(cid, p['id'])] for n in p['next_ids'])]
        if buyers: ups.append({'name': p['name'], 'next': ', '.join(products[n]['name'] for n in p['next_ids'] if n in products), 'buyers': len(buyers), 'converted': len(conv),
                               'rate': round(100 * len(conv) / len(buyers), 1)})
    ndue = len(due); nren = sum(1 for x in due if x['renewed'])
    open_opps = await db.opps.count_documents({'company_id': user['company_id'], 'closed_at': None})
    won_opps = await db.opps.find({'company_id': user['company_id'], 'stage_id': 'won', 'closed_at': {'$gte': t0, '$lt': t1}}, {'_id': 0, 'won_value': 1}).to_list(5000)
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'due': ndue, 'renewed': nren, 'rate': round(100 * nren / ndue, 1) if ndue else None,
            'lost_value': round(sum(x['value'] for x in due if not x['renewed']), 2), 'by_product': sorted(by.values(), key=lambda b: -b['due']),
            'upcoming': {str(k): v for k, v in sorted(upcoming.items())}, 'upsell': sorted(ups, key=lambda u: -u['buyers']),
            'opps_open': open_opps, 'opps_won': len(won_opps), 'opps_won_value': round(sum(x.get('won_value') or 0 for x in won_opps), 2)}
