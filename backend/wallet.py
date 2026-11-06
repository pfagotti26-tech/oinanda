"""Carteira do cliente e segmentos da base.

Carteira: o que o cliente tem (ativo e até quando), o que já teve (venceu e não renovou) e o que pode comprar
(próximas ofertas do catálogo). Item com validade (duration_days) gera vencimento; recompra antes de vencer soma
no fim do período atual. Fica guardada no contato (holdings) para filtrar rápido.

Segmentos: públicos montados com regras (todas precisam valer). Ficam salvos e se atualizam sozinhos:
quem passa a se encaixar entra, quem deixa de se encaixar sai.
"""
import csv, io
from datetime import timedelta
from typing import Optional, List, Literal, Any
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, aware

router = APIRouter()
SOON_DAYS = 15

# ---------- carteira ----------
def compute_holdings(sales, products):
    """sales: vendas válidas do cliente (com items/date). products: id -> produto."""
    h = {}
    for s in sorted(sales, key=lambda x: aware(x['date'])):
        d = aware(s['date'])
        for i in s['items']:
            key = i.get('product_id') or 'avulso:' + i['name']
            p = products.get(i.get('product_id')) or {}
            x = h.setdefault(key, {'key': key, 'product_id': i.get('product_id'), 'name': p.get('name') or i['name'], 'category': p.get('category') or i.get('category') or '',
                                   'first_at': d, 'last_at': d, 'count': 0, 'spent': 0.0, 'expires_at': None})
            x['last_at'] = d; x['count'] += 1; x['spent'] = round(x['spent'] + (i.get('total') or 0), 2)
            dur = p.get('duration_days')
            if dur:
                start = max(d, x['expires_at']) if x['expires_at'] else d  # renovou antes de vencer: soma no fim
                x['expires_at'] = start + timedelta(days=dur * max(1, int(round(i.get('qty') or 1))))
    return list(h.values())

def state_of(x, at=None):
    t = at or now()
    e = aware(x.get('expires_at'))
    if not e: return 'ativo'
    if e < t: return 'vencido'
    if e - t <= timedelta(days=SOON_DAYS): return 'vence_logo'
    return 'ativo'

async def products_map(company_id):
    return {p['id']: p for p in await db.products.find({'company_id': company_id}, {'_id': 0}).to_list(2000)}

async def refresh_holdings(company_id, contact_id, products=None):
    products = products or await products_map(company_id)
    sales = await db.sales.find({'company_id': company_id, 'contact_id': contact_id, 'status': 'ok'}, {'_id': 0, 'items': 1, 'date': 1}).to_list(1000)
    hs = compute_holdings(sales, products)
    nxt = min((aware(x['expires_at']) for x in hs if x.get('expires_at') and aware(x['expires_at']) >= now()), default=None)
    await db.contacts.update_one({'id': contact_id}, {'$set': {'holdings': hs, 'next_expiry': nxt}})
    return hs

async def recompute_product(company_id, product_id):
    """Mudou a validade de um item: refaz a carteira de quem comprou."""
    products = await products_map(company_id)
    ids = await db.sales.distinct('contact_id', {'company_id': company_id, 'items.product_id': product_id})
    for cid in ids: await refresh_holdings(company_id, cid, products)
    return len(ids)

def offers_for(hs, products):
    """Próximas ofertas: o que os itens que ele tem sugerem e ele ainda não tem ativo. Renovação para o que vence/venceu."""
    active = {x['product_id'] for x in hs if x.get('product_id') and state_of(x) != 'vencido'}
    out, seen = [], set()
    for x in hs:
        st = state_of(x)
        if x.get('product_id') and st in ('vence_logo', 'vencido') and x['product_id'] not in seen:
            seen.add(x['product_id']); p = products.get(x['product_id']) or {}
            out.append({'product_id': x['product_id'], 'name': x['name'], 'price': p.get('price'), 'why': 'renovar' if st == 'vence_logo' else 'voltar'})
    for x in hs:
        for nid in (products.get(x.get('product_id')) or {}).get('next_ids') or []:
            p = products.get(nid)
            if p and p.get('active', True) and nid not in active and nid not in seen:
                seen.add(nid); out.append({'product_id': nid, 'name': p['name'], 'price': p.get('price'), 'why': 'proximo', 'from': x['name']})
    return out

@router.get('/contacts/{contact_id}/wallet')
async def wallet(contact_id: str, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    products = await products_map(user['company_id'])
    hs = c.get('holdings')
    if hs is None and c.get('sales_count'): hs = await refresh_holdings(user['company_id'], c['id'], products)
    hs = hs or []
    items = [{**x, 'state': state_of(x)} for x in hs]
    items.sort(key=lambda x: ({'vence_logo': 0, 'ativo': 1, 'vencido': 2}[x['state']], aware(x['expires_at']) or now()))
    return {'items': items, 'offers': offers_for(hs, products)}

# ---------- segmentos ----------
FIELDS = {
    'product': 'Item', 'category': 'Categoria', 'expires_in': 'Vence nos próximos dias', 'expired_within': 'Venceu e não renovou (até X dias)',
    'last_purchase': 'Última compra', 'purchases': 'Número de compras', 'spent': 'Total gasto', 'kind': 'Situação', 'origin': 'Origem',
    'stage': 'Etapa do funil', 'tag': 'Etiqueta', 'team': 'Equipe', 'owner': 'Responsável', 'promo': 'Usou promoção', 'created': 'Chegou há',
}

class Rule(BaseModel):
    field: Literal['product', 'category', 'expires_in', 'expired_within', 'last_purchase', 'purchases', 'spent', 'kind', 'origin', 'stage', 'tag', 'team', 'owner', 'promo', 'created', 'milestone']
    op: str = Field(default='is', max_length=12)  # is | not | active | ever | never | gte | lte | more | less
    value: Any = None

class SegIn(BaseModel):
    rules: List[Rule] = Field(default_factory=list, max_length=12)

def _days(v):
    try: return max(0, min(3650, int(v)))
    except (TypeError, ValueError): raise HTTPException(400, 'Informe um número de dias.')

def _num(v):
    try: return float(v)
    except (TypeError, ValueError): raise HTTPException(400, 'Informe um número.')

def match(c, r: Rule, ctx):
    t = now(); hs = c.get('holdings') or []
    f, op, v = r.field, r.op, r.value
    if f in ('product', 'category'):
        sel = [x for x in hs if (x.get('product_id') or x['key']) == v] if f == 'product' else [x for x in hs if (x.get('category') or '') == v]
        if op == 'active': return any(state_of(x) != 'vencido' for x in sel)
        if op == 'never': return not sel
        if op == 'expired': return bool(sel) and all(state_of(x) == 'vencido' for x in sel)
        return bool(sel)  # ever
    if f == 'expires_in':
        n = _days(v.get('days') if isinstance(v, dict) else v); pid = v.get('product') if isinstance(v, dict) else None
        return any(x.get('expires_at') and t <= aware(x['expires_at']) <= t + timedelta(days=n) and (not pid or x.get('product_id') == pid) for x in hs)
    if f == 'expired_within':
        n = _days(v.get('days') if isinstance(v, dict) else v); pid = v.get('product') if isinstance(v, dict) else None
        return any(x.get('expires_at') and t - timedelta(days=n) <= aware(x['expires_at']) < t and (not pid or x.get('product_id') == pid) for x in hs)
    if f == 'last_purchase':
        last = aware(c.get('last_sale_at'))
        if not last: return op == 'more'  # nunca comprou conta como "há mais de"
        days = (t - last).days
        return days > _days(v) if op == 'more' else days <= _days(v)
    if f == 'created':
        cr = aware(c.get('created_at'))
        if not cr: return False
        days = (t - cr).days
        return days > _days(v) if op == 'more' else days <= _days(v)
    if f == 'purchases':
        n = c.get('sales_count') or 0
        return n >= _num(v) if op == 'gte' else n <= _num(v)
    if f == 'spent':
        n = c.get('value') or 0 if c.get('sales_count') else 0
        return n >= _num(v) if op == 'gte' else n <= _num(v)
    if f == 'kind':
        from origins import status_of
        return (status_of(c, ctx['inactive_days']) == v) == (op != 'not')
    if f == 'origin':
        o = c.get('origin') or ('anuncio' if c.get('ad') else '_none')
        vals = v if isinstance(v, list) else [v]
        return (o in vals) == (op != 'not')
    if f == 'stage': return ((c.get('stage') or ctx['first_stage']) == v) == (op != 'not')
    if f == 'tag': return (v in (c.get('tags') or [])) == (op != 'not')
    if f == 'team': return (c.get('team_id') == (v or None)) == (op != 'not')
    if f == 'owner': return (c.get('assigned_to') == (v or None)) == (op != 'not')
    if f == 'promo': return (c['id'] in ctx['promo_users'].get(v, set())) == (op != 'not')
    if f == 'milestone':
        mid = v.get('id') if isinstance(v, dict) else v
        last = ctx['milestones'].get(mid, {}).get(c['id'])
        if op == 'never': return last is None
        if op in ('within', 'more'):
            if last is None: return False
            days = (t - last).days; n = _days(v.get('days') if isinstance(v, dict) else 0)
            return days <= n if op == 'within' else days > n
        return last is not None
    return True

async def run_segment(user, rules):
    from funnel import stages_for
    from inbox import visible_q
    company = user['company']
    stages = await stages_for(company)
    ctx = {'inactive_days': company.get('inactive_days'), 'first_stage': stages[0]['id'] if stages else None, 'promo_users': {}}
    for pid in {r.value for r in rules if r.field == 'promo'}:
        ctx['promo_users'][pid] = set(await db.sales.distinct('contact_id', {'company_id': company['id'], 'status': 'ok', 'promos.promo_id': pid}))
    ctx['milestones'] = {}
    if any(r.field == 'milestone' for r in rules):
        from journey import hits_for
        for h in await hits_for(company):
            d = ctx['milestones'].setdefault(h['milestone_id'], {})
            if h['contact_id'] not in d or h['at'] > d[h['contact_id']]: d[h['contact_id']] = h['at']
    if any(r.field in ('product', 'category', 'expires_in', 'expired_within') for r in rules):  # carteira ainda não calculada
        products = None
        for c in await db.contacts.find({'company_id': company['id'], 'sales_count': {'$gt': 0}, 'holdings': {'$exists': False}}, {'_id': 0, 'id': 1}).to_list(20000):
            products = products or await products_map(company['id'])
            await refresh_holdings(company['id'], c['id'], products)
    rows = await db.contacts.find(visible_q(user), {'_id': 0, 'id': 1, 'name': 1, 'wa_id': 1, 'stage': 1, 'tags': 1, 'team_id': 1, 'assigned_to': 1, 'origin': 1, 'ad': 1,
                                                    'sales_count': 1, 'value': 1, 'last_sale_at': 1, 'created_at': 1, 'holdings': 1, 'next_expiry': 1, 'opt_out': 1, 'last_in_at': 1}).to_list(50000)
    return [c for c in rows if all(match(c, r, ctx) for r in rules)]

def row_out(c, names):
    hs = c.get('holdings') or []
    return {'id': c['id'], 'name': c['name'], 'wa_id': c.get('wa_id'), 'stage': c.get('stage'), 'owner': names.get(c.get('assigned_to')), 'spent': c.get('value') if c.get('sales_count') else 0,
            'purchases': c.get('sales_count') or 0, 'last_sale_at': c.get('last_sale_at'), 'next_expiry': c.get('next_expiry'), 'opt_out': bool(c.get('opt_out')),
            'has': [{'name': x['name'], 'state': state_of(x), 'expires_at': x.get('expires_at')} for x in hs][:6]}

@router.post('/segments/preview')
async def preview(body: SegIn, user=Depends(current_user)):
    rows = await run_segment(user, body.rules)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    rows.sort(key=lambda c: (aware(c.get('next_expiry')) or now() + timedelta(days=99999), -(c.get('value') or 0)))
    return {'count': len(rows), 'spent': round(sum((c.get('value') or 0) for c in rows if c.get('sales_count')), 2),
            'reachable': sum(1 for c in rows if not c.get('opt_out')), 'items': [row_out(c, names) for c in rows[:300]]}

@router.get('/segments/fields')
async def fields(user=Depends(current_user)):
    return FIELDS

class SegSave(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    rules: List[Rule] = Field(default_factory=list, max_length=12)

@router.get('/segments')
async def list_segments(user=Depends(current_user)):
    rows = await db.segments.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(200)
    for s in rows:
        try: s['count'] = len(await run_segment(user, [Rule(**r) for r in s['rules']]))
        except HTTPException: s['count'] = None
    return rows

@router.post('/segments')
async def add_segment(body: SegSave, request: Request, user=Depends(current_user)):
    from billing import require; require(user, 'segmentos')
    if await db.segments.count_documents({'company_id': user['company_id']}) >= 200: raise HTTPException(400, 'Limite de 200 segmentos.')
    s = {'id': uid(), 'company_id': user['company_id'], 'name': body.name.strip(), 'rules': [r.model_dump() for r in body.rules], 'by': user['name'], 'created_at': now()}
    await db.segments.insert_one(s.copy())
    await audit(request, user, 'Criou segmento', s['name'])
    s.pop('_id', None)
    return s

@router.put('/segments/{sid}')
async def edit_segment(sid: str, body: SegSave, user=Depends(current_user)):
    s = await db.segments.find_one({'id': sid, 'company_id': user['company_id']}, {'_id': 0})
    if not s: raise HTTPException(404, 'Segmento não encontrado.')
    upd = {'name': body.name.strip(), 'rules': [r.model_dump() for r in body.rules], 'updated_at': now()}
    await db.segments.update_one({'id': sid}, {'$set': upd})
    return {**s, **upd}

@router.delete('/segments/{sid}')
async def del_segment(sid: str, user=Depends(current_user)):
    r = await db.segments.delete_one({'id': sid, 'company_id': user['company_id']})
    if not r.deleted_count: raise HTTPException(404, 'Segmento não encontrado.')
    return {'ok': True}

class BulkTag(SegIn):
    tag: str = Field(min_length=1, max_length=24)
    remove: bool = False

@router.post('/segments/tag')
async def bulk_tag(body: BulkTag, request: Request, user=Depends(current_user)):
    rows = await run_segment(user, body.rules)
    ids = [c['id'] for c in rows]
    tag = body.tag.strip()
    if body.remove: await db.contacts.update_many({'id': {'$in': ids}}, {'$pull': {'tags': tag}})
    else: await db.contacts.update_many({'id': {'$in': ids}}, {'$addToSet': {'tags': tag}})
    await audit(request, user, ('Tirou' if body.remove else 'Colocou') + f' a etiqueta "{tag}" em massa', f'{len(ids)} contatos')
    return {'ok': True, 'count': len(ids)}

@router.post('/segments/export')
async def export(body: SegIn, user=Depends(managers)):
    rows = await run_segment(user, body.rules)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    buf = io.StringIO(); w = csv.writer(buf, delimiter=';')
    w.writerow(['Nome', 'Telefone', 'Compras', 'Total gasto', 'Última compra', 'Próximo vencimento', 'Tem', 'Responsável', 'Não quer mensagens'])
    br = lambda v: f'{v:.2f}'.replace('.', ',')
    for c in rows:
        o = row_out(c, names)
        w.writerow([o['name'], '+' + (o['wa_id'] or ''), o['purchases'], br(o['spent'] or 0), aware(o['last_sale_at']).strftime('%d/%m/%Y') if o['last_sale_at'] else '',
                    aware(o['next_expiry']).strftime('%d/%m/%Y') if o['next_expiry'] else '', ', '.join(h['name'] for h in o['has']), o['owner'] or '', 'sim' if o['opt_out'] else ''])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="segmento.csv"'})
