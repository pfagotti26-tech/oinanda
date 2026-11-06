"""Vendas: catálogo de produtos e serviços, registro de vendas e relatório configurável.

Uma venda pertence a um cliente e tem itens (do catálogo ou avulsos), forma de pagamento e um ou mais vendedores
com percentual. Um cliente pode ter várias vendas. O valor do card no funil passa a ser a soma das vendas dele.
No relatório, numa venda compartilhada cada vendedor recebe a parte do seu percentual: o total da empresa não duplica.
"""
import csv, io, re
from datetime import datetime, timedelta, time as dtime
from typing import Optional, List, Literal, Dict, Any
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, can_see, aware, note
import agenda

router = APIRouter()
PAYMENTS = {'pix': 'Pix', 'cartao_credito': 'Cartão de crédito', 'cartao_debito': 'Cartão de débito', 'boleto': 'Boleto',
            'dinheiro': 'Dinheiro', 'transferencia': 'Transferência', 'outro': 'Outro'}
SOURCES = {'whatsapp': 'Chamou no WhatsApp', 'manual': 'Cadastro manual', 'historico': 'Conversa antiga do celular', 'agenda do celular': 'Agenda do celular'}
GROUPS = ('seller', 'product', 'category', 'team', 'source', 'kind', 'promo', 'payment', 'day', 'week', 'month')
MGR = ('owner', 'manager')

# ---------- catálogo ----------
class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    category: str = Field(default='', max_length=40)
    price: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    kind: Literal['produto', 'servico'] = 'servico'
    active: bool = True
    duration_days: Optional[int] = Field(default=None, ge=1, le=3650)  # validade: plano, módulo, assinatura
    next_ids: List[str] = Field(default_factory=list, max_length=10)     # o que oferecer depois

async def sync_agenda(company):
    """Os serviços da agenda entram no catálogo (e o preço acompanha)."""
    s = await agenda.settings_for(company)
    for sv in s.get('services') or []:
        p = await db.products.find_one({'company_id': company['id'], 'service_id': sv['id']}, {'_id': 0})
        if not p:
            await db.products.insert_one({'id': uid(), 'company_id': company['id'], 'service_id': sv['id'], 'name': sv['name'], 'category': 'Agenda',
                                          'price': sv.get('price'), 'kind': 'servico', 'active': True, 'created_at': now()})
        elif p['name'] != sv['name'] or (sv.get('price') is not None and p.get('price') != sv.get('price')):
            await db.products.update_one({'id': p['id']}, {'$set': {'name': sv['name'], **({'price': sv['price']} if sv.get('price') is not None else {})}})

@router.get('/products')
async def products(user=Depends(current_user)):
    await sync_agenda(user['company'])
    return await db.products.find({'company_id': user['company_id']}, {'_id': 0}).sort([('category', 1), ('name', 1)]).to_list(1000)

@router.post('/products')
async def add_product(body: ProductIn, request: Request, user=Depends(managers)):
    if await db.products.count_documents({'company_id': user['company_id']}) >= 1000: raise HTTPException(400, 'Limite de 1000 itens no catálogo.')
    if await db.products.find_one({'company_id': user['company_id'], 'name': body.name.strip()}): raise HTTPException(409, 'Já existe um item com esse nome.')
    p = {'id': uid(), 'company_id': user['company_id'], **body.model_dump(), 'name': body.name.strip(), 'category': body.category.strip(), 'created_at': now()}
    await db.products.insert_one(p.copy())
    await audit(request, user, 'Cadastrou no catálogo', p['name'])
    p.pop('_id', None)
    return p

@router.put('/products/{pid}')
async def edit_product(pid: str, body: ProductIn, request: Request, user=Depends(managers)):
    p = await db.products.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(404, 'Item não encontrado.')
    upd = {**body.model_dump(), 'name': body.name.strip(), 'category': body.category.strip(), 'next_ids': [i for i in body.next_ids if i != pid]}
    await db.products.update_one({'id': pid}, {'$set': upd})
    if p.get('duration_days') != body.duration_days or p.get('name') != upd['name']:
        from wallet import recompute_product
        await recompute_product(user['company_id'], pid)
    return {**p, **upd}

@router.delete('/products/{pid}')
async def del_product(pid: str, user=Depends(managers)):
    p = await db.products.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(404, 'Item não encontrado.')
    if p.get('service_id'): raise HTTPException(400, 'Este serviço vem da Agenda. Para tirar, apague lá ou marque como inativo.')
    used = await db.sales.find_one({'company_id': user['company_id'], 'items.product_id': pid})
    if used:  # já vendido: só desativa, para o histórico continuar certo
        await db.products.update_one({'id': pid}, {'$set': {'active': False}})
        return {'ok': True, 'deactivated': True}
    await db.products.delete_one({'id': pid})
    return {'ok': True}

# ---------- vendas ----------
class ItemIn(BaseModel):
    product_id: Optional[str] = None
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(default='', max_length=40)
    qty: float = Field(default=1, gt=0, le=100000)
    price: float = Field(ge=0, le=100_000_000)
    discount: float = Field(default=0, ge=0, le=100_000_000)

class SellerIn(BaseModel):
    user_id: str
    pct: float = Field(gt=0, le=100)

class SaleIn(BaseModel):
    contact_id: str
    date: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    items: List[ItemIn] = Field(min_length=1, max_length=50)
    payment: Literal['pix', 'cartao_credito', 'cartao_debito', 'boleto', 'dinheiro', 'transferencia', 'outro', ''] = ''
    sellers: Optional[List[SellerIn]] = Field(default=None, max_length=10)
    notes: str = Field(default='', max_length=1000)
    move_won: bool = True
    promo_ids: Optional[List[str]] = Field(default=None, max_length=20)  # None = aplica sozinho as promoções automáticas
    coupon: Optional[str] = Field(default=None, max_length=30)

def line_total(i): return round(max(0.0, i['qty'] * i['price'] - (i.get('discount') or 0)), 2)

def local_noon(company, d):
    tz = agenda.tz_of(company)
    return datetime.combine(datetime.strptime(d, '%Y-%m-%d').date(), dtime(12), tz)

def local_day(company, dt):
    return aware(dt).astimezone(agenda.tz_of(company)).date()

async def clean_sale(user, body: SaleIn, contact, skip_sale=None):
    users = {u['id']: u for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    sellers = [s.model_dump() for s in body.sellers] if body.sellers is not None else [{'user_id': contact.get('assigned_to') or user['id'], 'pct': 100}]
    if any(s['user_id'] not in users for s in sellers): raise HTTPException(400, 'Vendedor não encontrado.')
    if len({s['user_id'] for s in sellers}) != len(sellers): raise HTTPException(400, 'O mesmo vendedor aparece duas vezes.')
    if sellers and abs(sum(s['pct'] for s in sellers) - 100) > 0.01: raise HTTPException(400, 'A divisão entre vendedores precisa somar 100%.')
    for s in sellers: s['name'] = users[s['user_id']]['name']
    cat = {p['id']: p for p in await db.products.find({'company_id': user['company_id']}, {'_id': 0}).to_list(1000)}
    items = []
    for i in body.items:
        it = i.model_dump()
        if it['product_id']:
            p = cat.get(it['product_id'])
            if not p: raise HTTPException(400, 'Item do catálogo não encontrado.')
            it['category'] = it['category'] or p.get('category') or ''
        it['name'] = it['name'].strip(); it['category'] = it['category'].strip()
        it['total'] = line_total(it)
        items.append(it)
    date = local_noon(user['company'], body.date) if body.date else now()
    if date > now() + timedelta(days=1): raise HTTPException(400, 'A data da venda não pode ser no futuro.')
    import promos as pr
    r = await pr.price(user['company'], contact, items, body.promo_ids, body.coupon, skip_sale)
    if body.coupon and not (r['coupon'] or {}).get('ok'): raise HTTPException(400, f'Cupom {body.coupon.strip().upper()}: {r["coupon"]["reason"]}.')
    subtotal = round(sum(i['total'] for i in items), 2)
    for it, w in zip(items, r['items']):
        it['promo_discount'] = w['promo_discount']; it['total'] = round(max(0.0, it['total'] - w['promo_discount']), 2)
    gross = sum(i['qty'] * i['price'] for i in items); manual = sum(i.get('discount') or 0 for i in items)
    lim = user['company'].get('max_discount_pct')
    approval = bool(lim is not None and user['role'] == 'agent' and gross and 100 * manual / gross > lim + 0.001)
    return {'contact_id': contact['id'], 'contact_name': contact.get('name'), 'date': date, 'items': items, 'total': round(sum(i['total'] for i in items), 2),
            'subtotal': subtotal, 'promos': r['applied'], 'discount': round(manual + r['discount'], 2), 'needs_approval': approval,
            'payment': body.payment, 'sellers': sellers, 'notes': body.notes.strip(), 'team_id': contact.get('team_id'),
            'source': (('ad:' + ((contact.get('ad') or {}).get('headline') or (contact.get('ad') or {}).get('source_id') or 'sem título')) if contact.get('ad') else contact.get('source') or 'whatsapp')}

async def refresh_contact(company, contact_id):
    """O valor do card no funil passa a ser a soma das vendas do cliente."""
    rows = await db.sales.find({'company_id': company['id'], 'contact_id': contact_id, 'status': 'ok'}, {'_id': 0, 'total': 1, 'date': 1}).to_list(1000)
    await db.contacts.update_one({'id': contact_id}, {'$set': {'value': round(sum(r['total'] for r in rows), 2) if rows else None, 'sales_count': len(rows),
                                                              'first_sale_at': min((r['date'] for r in rows), default=None), 'last_sale_at': max((r['date'] for r in rows), default=None)}})
    from wallet import refresh_holdings
    await refresh_holdings(company['id'], contact_id)

def can_edit(user, sale):
    return user['role'] in MGR or sale.get('created_by') == user['id'] or any(s['user_id'] == user['id'] for s in sale.get('sellers') or [])

def out(sale):
    s = {k: v for k, v in sale.items() if k != '_id'}
    s['payment_label'] = PAYMENTS.get(s.get('payment'), '')
    return s

@router.post('/sales')
async def create_sale(body: SaleIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, body.contact_id)
    who = user.get('impersonated_by') or user['name']
    data = await clean_sale(user, body, c)
    pending = data.pop('needs_approval')
    sale = {'id': uid(), 'company_id': user['company_id'], **data, 'status': 'pending' if pending else 'ok',
            'created_by': user['id'], 'created_by_name': who, 'created_at': now(), 'history': []}
    await db.sales.insert_one(sale.copy())
    if body.move_won:
        from funnel import stages_for, move_stage
        stages = await stages_for(user['company'])
        won = next((s for s in stages if s['kind'] == 'won'), None)
        if won and next((s['kind'] for s in stages if s['id'] == c.get('stage')), None) != 'won':
            await move_stage(user['company'], c, won['id'], who, stages)
    await refresh_contact(user['company'], c['id'])
    brl = f'R$ {sale["total"]:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    await note(user['company_id'], c['id'], f'{who} registrou uma venda de {brl}' + (' (desconto acima do limite: aguardando aprovação do gestor)' if pending else ''))
    await audit(request, user, 'Registrou venda', f'{c["name"]} · {brl}')
    return out(sale)

@router.put('/sales/{sid}')
async def edit_sale(sid: str, body: SaleIn, request: Request, user=Depends(current_user)):
    s = await db.sales.find_one({'id': sid, 'company_id': user['company_id']}, {'_id': 0})
    if not s or not can_edit(user, s): raise HTTPException(404, 'Venda não encontrada.')
    c = await db.contacts.find_one({'id': s['contact_id'], 'company_id': user['company_id']}, {'_id': 0}) or {'id': s['contact_id'], 'name': s.get('contact_name')}
    upd = await clean_sale(user, body, {**c, 'team_id': s.get('team_id')}, skip_sale=sid)
    pending = upd.pop('needs_approval')
    if s.get('status') == 'cancelled': raise HTTPException(400, 'Venda cancelada não pode ser editada.')
    upd['status'] = 'pending' if pending else ('ok' if user['role'] in MGR else s.get('status', 'ok'))
    upd['source'] = s.get('source'); upd['team_id'] = s.get('team_id'); upd['contact_id'] = s['contact_id']
    if not body.date: upd['date'] = s['date']  # sem data nova: mantém a da venda
    elif local_day(user['company'], upd['date']) == local_day(user['company'], s['date']): upd['date'] = s['date']
    changes = [k for k in ('date', 'items', 'total', 'payment', 'sellers', 'notes', 'promos') if upd[k] != s.get(k)]
    if 'items' in changes and all({k: v for k, v in a.items() if k not in ('promo_discount',)} == {k: v for k, v in b.items() if k not in ('promo_discount',)} for a, b in zip(upd['items'], s.get('items') or [])) and len(upd['items']) == len(s.get('items') or []) and 'total' not in changes: changes.remove('items')
    hist = {'at': now(), 'by': user.get('impersonated_by') or user['name'], 'what': ', '.join({'date': 'data', 'items': 'itens', 'total': 'valor', 'payment': 'pagamento', 'sellers': 'vendedores', 'notes': 'observação', 'promos': 'promoções'}[k] for k in changes) or 'sem mudanças'}
    if s.get('status') == 'pending' and upd['status'] == 'ok': hist['what'] += ' e aprovou'
    await db.sales.update_one({'id': sid}, {'$set': {**upd, 'updated_at': now()}, '$push': {'history': hist}})
    await refresh_contact(user['company'], s['contact_id'])
    await audit(request, user, 'Editou venda', f'{s.get("contact_name")} · {hist["what"]}')
    return out({**s, **upd, 'history': (s.get('history') or []) + [hist]})

@router.post('/sales/{sid}/approve')
async def approve_sale(sid: str, request: Request, user=Depends(managers)):
    s = await db.sales.find_one({'id': sid, 'company_id': user['company_id']}, {'_id': 0})
    if not s or s.get('status') != 'pending': raise HTTPException(404, 'Venda não encontrada ou já aprovada.')
    await db.sales.update_one({'id': sid}, {'$set': {'status': 'ok', 'approved_by': user['name'], 'approved_at': now()},
                                            '$push': {'history': {'at': now(), 'by': user.get('impersonated_by') or user['name'], 'what': 'aprovou o desconto'}}})
    await refresh_contact(user['company'], s['contact_id'])
    await note(user['company_id'], s['contact_id'], f'{user["name"]} aprovou a venda com desconto')
    await audit(request, user, 'Aprovou venda', s.get('contact_name'))
    return {'ok': True}

class CancelIn(BaseModel):
    reason: str = Field(default='', max_length=300)

@router.post('/sales/{sid}/cancel')
async def cancel_sale(sid: str, body: CancelIn, request: Request, user=Depends(current_user)):
    s = await db.sales.find_one({'id': sid, 'company_id': user['company_id']}, {'_id': 0})
    if not s or not can_edit(user, s): raise HTTPException(404, 'Venda não encontrada.')
    hist = {'at': now(), 'by': user.get('impersonated_by') or user['name'], 'what': 'cancelou' + (f': {body.reason}' if body.reason else '')}
    await db.sales.update_one({'id': sid}, {'$set': {'status': 'cancelled', 'cancelled_at': now()}, '$push': {'history': hist}})
    await refresh_contact(user['company'], s['contact_id'])
    await audit(request, user, 'Cancelou venda', s.get('contact_name'))
    return {'ok': True}

@router.get('/contacts/{contact_id}/sales')
async def contact_sales(contact_id: str, user=Depends(current_user)):
    await get_contact(user, contact_id)
    rows = await db.sales.find({'company_id': user['company_id'], 'contact_id': contact_id}, {'_id': 0}).sort('date', -1).to_list(200)
    return [out(r) for r in rows]

# ---------- vendas antigas (valor preenchido no funil) ----------
async def migrate_legacy(company):
    from funnel import stages_for
    won = [s['id'] for s in await stages_for(company) if s['kind'] == 'won']
    rows = await db.contacts.find({'company_id': company['id'], 'stage': {'$in': won}, 'value': {'$gt': 0}, 'sales_migrated': {'$ne': True}}, {'_id': 0}).to_list(5000)
    if not rows: return
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': company['id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    for c in rows:
        if not await db.sales.find_one({'company_id': company['id'], 'contact_id': c['id']}):
            sellers = [{'user_id': c['assigned_to'], 'pct': 100, 'name': names.get(c['assigned_to'], '')}] if c.get('assigned_to') in names else []
            await db.sales.insert_one({'id': uid(), 'company_id': company['id'], 'contact_id': c['id'], 'contact_name': c.get('name'),
                                       'date': aware(c.get('won_at') or c.get('stage_at') or c.get('created_at')) or now(),
                                       'items': [{'product_id': None, 'name': 'Venda registrada no funil', 'category': '', 'qty': 1, 'price': c['value'], 'discount': 0, 'total': c['value']}],
                                       'total': c['value'], 'payment': '', 'sellers': sellers, 'notes': '', 'team_id': c.get('team_id'),
                                       'source': ('ad:' + ((c.get('ad') or {}).get('headline') or 'sem título')) if c.get('ad') else c.get('source') or 'whatsapp',
                                       'status': 'ok', 'legacy': True, 'created_by': None, 'created_by_name': 'Funil', 'created_at': now(), 'history': []})
        await db.contacts.update_one({'id': c['id']}, {'$set': {'sales_migrated': True}})

# ---------- consulta e relatório ----------
class Filters(BaseModel):
    start: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    seller: Optional[str] = None
    team: Optional[str] = None
    product: Optional[str] = None
    category: Optional[str] = None
    payment: Optional[str] = None
    source: Optional[str] = None
    q: Optional[str] = Field(default=None, max_length=80)
    status: Literal['ok', 'cancelled', 'pending', 'open', 'all'] = 'ok'
    promo: Optional[str] = None
    kind: Literal['', 'nova', 'recompra'] = ''

def period(company, f: Filters):
    tz = agenda.tz_of(company)
    today = now().astimezone(tz).date()
    end = datetime.strptime(f.end, '%Y-%m-%d').date() if f.end else today
    start = datetime.strptime(f.start, '%Y-%m-%d').date() if f.start else end - timedelta(days=29)
    if start > end: start, end = end, start
    if (end - start).days > 1100: raise HTTPException(400, 'Escolha um período de até 3 anos.')
    return start, end, datetime.combine(start, dtime(0), tz), datetime.combine(end + timedelta(days=1), dtime(0), tz)

def src_label(s):
    if (s or '').startswith('ad:'): return 'Anúncio: ' + s[3:]
    return SOURCES.get(s, s or 'Outro')

async def origin_names(company):
    from origins import origins_of
    return {o['id']: o['name'] for o in await origins_of(company)} | {'_none': 'Não sabemos'}

async def fetch(user, f: Filters, t0, t1):
    q = {'company_id': user['company_id'], 'date': {'$gte': t0, '$lt': t1}}
    if f.status == 'open': q['status'] = {'$in': ['ok', 'pending']}
    elif f.status != 'all': q['status'] = f.status
    if f.promo == 'none': q['promos.0'] = {'$exists': False}
    elif f.promo: q['promos.promo_id'] = f.promo
    if user['role'] not in MGR: q['$or'] = [{'sellers.user_id': user['id']}, {'created_by': user['id']}]
    if f.team: q['team_id'] = f.team
    if f.payment: q['payment'] = f.payment
    if f.seller and f.seller != 'none': q['sellers.user_id'] = f.seller
    rows = await db.sales.find(q, {'_id': 0}).sort('date', -1).to_list(20000)
    # origem atual do cliente (correções feitas depois valem para trás) e se foi a primeira compra dele
    cids = list({r['contact_id'] for r in rows})
    cs = {c['id']: c for c in await db.contacts.find({'id': {'$in': cids}}, {'_id': 0, 'id': 1, 'origin': 1, 'ad': 1}).to_list(len(cids) + 1)}
    first = {}
    for x in await db.sales.find({'company_id': user['company_id'], 'contact_id': {'$in': cids}, 'status': 'ok'}, {'_id': 0, 'id': 1, 'contact_id': 1, 'date': 1}).sort('date', 1).to_list(50000):
        first.setdefault(x['contact_id'], x['id'])
    for r in rows:
        c = cs.get(r['contact_id']) or {}
        r['_origin'] = c.get('origin') or ('anuncio' if c.get('ad') or (r.get('source') or '').startswith('ad:') else '_none')
        r['_first'] = first.get(r['contact_id']) == r['id']
    if f.source: rows = [r for r in rows if r['_origin'] == f.source]
    if f.kind: rows = [r for r in rows if r['_first'] == (f.kind == 'nova')]
    if f.seller == 'none': rows = [r for r in rows if not r.get('sellers')]
    if f.q:
        t = f.q.lower()
        rows = [r for r in rows if t in (r.get('contact_name') or '').lower() or any(t in i['name'].lower() for i in r['items'])]
    return rows

def item_ok(i, f):
    if f.product and (i.get('product_id') or 'avulso:' + i['name']) != f.product: return False
    if f.category and (i.get('category') or '') != (f.category if f.category != '_none' else ''): return False
    return True

def facts(rows, f, my_id=None):
    """Uma linha por item de venda, já com a parte do vendedor quando há filtro por vendedor."""
    out = []
    for r in rows:
        share = 1.0
        if f.seller and f.seller != 'none': share = next((s['pct'] for s in r.get('sellers') or [] if s['user_id'] == f.seller), 0) / 100
        for i in r['items']:
            if not item_ok(i, f): continue
            out.append({'sale': r, 'item': i, 'value': round(i['total'] * share, 2), 'qty': i['qty'], 'share': share,
                        'disc': round(((i.get('discount') or 0) + (i.get('promo_discount') or 0)) * share, 2)})
    return out

def bucket_of(d, start, size):
    if size == 'day': return d
    if size == 'week': return d - timedelta(days=d.weekday())
    return d.replace(day=1)

def totals(fs):
    ids = {x['sale']['id'] for x in fs}
    v = round(sum(x['value'] for x in fs), 2)
    return {'value': v, 'count': len(ids), 'qty': round(sum(x['qty'] for x in fs), 2), 'ticket': round(v / len(ids), 2) if ids else None,
            'discount': round(sum(x.get('disc', 0) for x in fs), 2)}

@router.get('/sales')
async def list_sales(start: Optional[str] = None, end: Optional[str] = None, seller: Optional[str] = None, team: Optional[str] = None,
                     product: Optional[str] = None, category: Optional[str] = None, payment: Optional[str] = None, source: Optional[str] = None,
                     q: Optional[str] = None, status: str = 'open', kind: str = '', promo: Optional[str] = None, user=Depends(current_user)):
    f = Filters(start=start, end=end, seller=seller, team=team, product=product, category=category, payment=payment, source=source, q=q, status=status, kind=kind, promo=promo)
    await migrate_legacy(user['company'])
    onames = await origin_names(user['company'])
    s0, s1, t0, t1 = period(user['company'], f)
    rows = await fetch(user, f, t0, t1)
    if f.product or f.category: rows = [r for r in rows if any(item_ok(i, f) for i in r['items'])]
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'items': [{**out(r), 'source_label': onames.get(r['_origin'], ''), 'first': r['_first'], 'can_edit': can_edit(user, r)} for r in rows[:2000]]}

@router.get('/sales/report')
async def report(start: Optional[str] = None, end: Optional[str] = None, group: str = 'seller', seller: Optional[str] = None, team: Optional[str] = None,
                 product: Optional[str] = None, category: Optional[str] = None, payment: Optional[str] = None, source: Optional[str] = None,
                 bucket: Optional[str] = None, kind: str = '', promo: Optional[str] = None, user=Depends(current_user)):
    if group not in GROUPS: raise HTTPException(400, 'Agrupamento inválido.')
    if user['role'] not in MGR: seller = user['id']  # atendente vê só as próprias vendas
    f = Filters(start=start, end=end, seller=seller, team=team, product=product, category=category, payment=payment, source=source, kind=kind, promo=promo)
    company = user['company']
    onames = await origin_names(company)
    await migrate_legacy(company)
    s0, s1, t0, t1 = period(company, f)
    days = (s1 - s0).days + 1
    fs = facts(await fetch(user, f, t0, t1), f)
    pt0, pt1 = t0 - timedelta(days=days), t0
    prev = totals(facts(await fetch(user, f, pt0, pt1), f))
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    teams = {t['id']: t for t in await db.teams.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1, 'color': 1}).to_list(50)}
    size = bucket if bucket in ('day', 'week', 'month') else ('day' if days <= 45 else 'week' if days <= 200 else 'month')

    rows: Dict[str, Dict[str, Any]] = {}
    def add(key, label, x, value, qty, color=None):
        r = rows.setdefault(key, {'key': key, 'label': label, 'value': 0.0, 'qty': 0.0, 'sales': set(), 'color': color})
        r['value'] += value; r['qty'] += qty; r['sales'].add(x['sale']['id'])
    for x in fs:
        s, i = x['sale'], x['item']
        if group == 'seller':
            sel = s.get('sellers') or []
            if f.seller and f.seller != 'none': sel = [y for y in sel if y['user_id'] == f.seller]
            if not sel: add('none', 'Sem vendedor', x, x['value'], x['qty'])
            for y in sel:
                part = (y['pct'] / 100) / (x['share'] or 1) if f.seller and f.seller != 'none' else y['pct'] / 100
                add(y['user_id'], names.get(y['user_id'], y.get('name') or '?'), x, x['value'] * part, x['qty'] * part)
        elif group == 'product': add(i.get('product_id') or 'avulso:' + i['name'], i['name'], x, x['value'], x['qty'])
        elif group == 'category': add(i.get('category') or '_none', i.get('category') or 'Sem categoria', x, x['value'], x['qty'])
        elif group == 'team':
            t = teams.get(s.get('team_id'))
            add(t['id'] if t else '_none', t['name'] if t else 'Sem equipe', x, x['value'], x['qty'], t.get('color') if t else None)
        elif group == 'source': add(s['_origin'], onames.get(s['_origin'], s['_origin']), x, x['value'], x['qty'])
        elif group == 'promo':
            ps = s.get('promos') or []
            if not ps: add('none', 'Sem promoção', x, x['value'], x['qty'])
            else: add(ps[0]['promo_id'], ' + '.join(a['name'] for a in ps), x, x['value'], x['qty'])
        elif group == 'kind': add('nova' if s['_first'] else 'recompra', 'Primeira compra' if s['_first'] else 'Recompra', x, x['value'], x['qty'])
        elif group == 'payment': add(s.get('payment') or '_none', PAYMENTS.get(s.get('payment'), 'Não informado'), x, x['value'], x['qty'])
        else:
            b = bucket_of(local_day(company, s['date']), s0, group)
            add(b.isoformat(), b.isoformat(), x, x['value'], x['qty'])
    tot = totals(fs)
    out_rows = []
    for r in rows.values():
        n = len(r['sales'])
        out_rows.append({'key': r['key'], 'label': r['label'], 'value': round(r['value'], 2), 'qty': round(r['qty'], 2), 'count': n,
                         'ticket': round(r['value'] / n, 2) if n else None, 'share': round(100 * r['value'] / tot['value'], 1) if tot['value'] else 0, 'color': r['color']})
    out_rows.sort(key=(lambda r: r['key']) if group in ('day', 'week', 'month') else (lambda r: -r['value']))

    # série no tempo (preenchida com zeros)
    series, cur = {}, bucket_of(s0, s0, size)
    while cur <= s1:
        series[cur.isoformat()] = {'date': cur.isoformat(), 'value': 0.0, 'count': 0, 'ids': set()}
        cur = cur + timedelta(days=1) if size == 'day' else cur + timedelta(days=7) if size == 'week' else (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    for x in fs:
        k = bucket_of(local_day(company, x['sale']['date']), s0, size).isoformat()
        if k in series: series[k]['value'] += x['value']; series[k]['ids'].add(x['sale']['id'])
    ser = [{'date': v['date'], 'value': round(v['value'], 2), 'count': len(v['ids'])} for v in series.values()]

    # opções dos filtros
    cat = await db.products.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1, 'category': 1}).to_list(1000)
    seen = await db.sales.find({'company_id': user['company_id']}, {'_id': 0, 'items.name': 1, 'items.product_id': 1, 'items.category': 1, 'source': 1}).to_list(20000)
    avulsos = sorted({i['name'] for r in seen for i in r['items'] if not i.get('product_id')})
    cats = sorted({p.get('category') for p in cat if p.get('category')} | {i.get('category') for r in seen for i in r['items'] if i.get('category')})
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'days': days, 'bucket': size, 'group': group,
            'totals': tot, 'prev': prev, 'prev_start': (s0 - timedelta(days=days)).isoformat(), 'prev_end': (s0 - timedelta(days=1)).isoformat(),
            'rows': out_rows, 'series': ser,
            'options': {'sellers': [{'id': k, 'name': v} for k, v in names.items()], 'teams': [{'id': t['id'], 'name': t['name']} for t in teams.values()],
                        'products': [{'id': p['id'], 'name': p['name']} for p in sorted(cat, key=lambda p: p['name'])] + [{'id': 'avulso:' + n, 'name': n + ' (avulso)'} for n in avulsos],
                        'categories': cats, 'payments': [{'id': k, 'name': v} for k, v in PAYMENTS.items()], 'sources': [{'id': k, 'name': v} for k, v in onames.items()],
                        'promos': [{'id': p['id'], 'name': p['name']} for p in await db.promos.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)] + [{'id': 'none', 'name': 'Sem promoção'}]},
            'mine_only': user['role'] not in MGR,
            'pending': await db.sales.count_documents({'company_id': user['company_id'], 'status': 'pending', **({} if user['role'] in MGR else {'sellers.user_id': user['id']})})}

@router.get('/sales/export')
async def export(start: Optional[str] = None, end: Optional[str] = None, seller: Optional[str] = None, team: Optional[str] = None,
                 product: Optional[str] = None, category: Optional[str] = None, payment: Optional[str] = None, source: Optional[str] = None,
                 kind: str = '', user=Depends(current_user)):
    if user['role'] not in MGR: seller = user['id']
    f = Filters(start=start, end=end, seller=seller, team=team, product=product, category=category, payment=payment, source=source, kind=kind)
    onames = await origin_names(user['company'])
    s0, s1, t0, t1 = period(user['company'], f)
    teams = {t['id']: t['name'] for t in await db.teams.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(50)}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    br = lambda v: f'{v:.2f}'.replace('.', ',')
    w.writerow(['Data', 'Cliente', 'Item', 'Categoria', 'Quantidade', 'Preço', 'Desconto', 'Desconto de promoção', 'Total do item', 'Vendedores', 'Pagamento', 'Equipe', 'Origem', 'Compra', 'Promoção', 'Observação'])
    for x in facts(await fetch(user, f, t0, t1), f):
        s, i = x['sale'], x['item']
        w.writerow([local_day(user['company'], s['date']).strftime('%d/%m/%Y'), s.get('contact_name') or '', i['name'], i.get('category') or '',
                    br(i['qty']).rstrip('0').rstrip(','), br(i['price']), br(i.get('discount') or 0), br(i.get('promo_discount') or 0), br(x['value']),
                    ', '.join(f'{y.get("name")} {y["pct"]:g}%' for y in s.get('sellers') or []), PAYMENTS.get(s.get('payment'), ''),
                    teams.get(s.get('team_id'), ''), onames.get(s['_origin'], ''), 'primeira' if s['_first'] else 'recompra', ' + '.join(a['name'] + (f' ({a["code"]})' if a.get('code') else '') for a in s.get('promos') or []), s.get('notes') or ''])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="vendas_{s0.isoformat()}_{s1.isoformat()}.csv"'})

# ---------- visões salvas ----------
class ViewIn(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    params: Dict[str, str] = Field(default_factory=dict)
    shared: bool = True

KEYS = {'preset', 'start', 'end', 'group', 'seller', 'team', 'product', 'category', 'payment', 'source', 'kind', 'promo', 'metric', 'chart', 'bucket'}

@router.get('/sales/views')
async def views(user=Depends(current_user)):
    return await db.sales_views.find({'company_id': user['company_id'], '$or': [{'shared': True}, {'by_id': user['id']}]}, {'_id': 0}).sort('name', 1).to_list(100)

@router.post('/sales/views')
async def add_view(body: ViewIn, user=Depends(current_user)):
    if await db.sales_views.count_documents({'company_id': user['company_id']}) >= 100: raise HTTPException(400, 'Limite de 100 visões.')
    v = {'id': uid(), 'company_id': user['company_id'], 'name': body.name.strip(), 'params': {k: str(v)[:60] for k, v in body.params.items() if k in KEYS},
         'shared': body.shared and user['role'] in MGR, 'by_id': user['id'], 'by': user['name'], 'created_at': now()}
    await db.sales_views.insert_one(v.copy())
    v.pop('_id', None)
    return v

@router.delete('/sales/views/{vid}')
async def del_view(vid: str, user=Depends(current_user)):
    v = await db.sales_views.find_one({'id': vid, 'company_id': user['company_id']}, {'_id': 0})
    if not v or (v['by_id'] != user['id'] and user['role'] not in MGR): raise HTTPException(404, 'Visão não encontrada.')
    await db.sales_views.delete_one({'id': vid})
    return {'ok': True}
