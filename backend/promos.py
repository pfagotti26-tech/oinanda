"""Promoções e cupons.

Promoção: regra com período (e, se quiser, dias da semana e horário), o que cobre (tudo, categorias ou produtos),
o tipo de desconto (percentual, valor fixo, preço especial, leve X pague Y) e condições (primeira compra, valor mínimo,
origem do cliente, só cliente inativo, limite de usos). Pode valer sozinha ou só com cupom.
Cupom: código geral (BLACK20) ou individual (um por cliente), com validade e limite de usos.

O Oi Nanda não cobra: a promoção entra no registro da venda (o desconto vai certo para o relatório) e no que a Nanda
sabe (ela só oferece o que está valendo e confere o cupom que o cliente digita).
Desconto manual acima do limite da empresa, dado por atendente, deixa a venda esperando aprovação do gestor.
"""
import re, secrets
from datetime import datetime, timedelta, time as dtime
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, aware
import agenda

router = APIRouter()
KINDS = {'percent': 'percentual', 'fixed': 'valor fixo', 'price': 'preço especial', 'bxgy': 'leve X pague Y'}
WEEK = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']

def brl(v): return f'R$ {v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.').replace(',00', '')

class PromoIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(default='', max_length=400)
    active: bool = True
    starts: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    ends: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    weekdays: List[int] = Field(default_factory=list, max_length=7)  # 0 = segunda
    hour_from: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    hour_to: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    scope: Literal['all', 'categories', 'products'] = 'all'
    categories: List[str] = Field(default_factory=list, max_length=30)
    product_ids: List[str] = Field(default_factory=list, max_length=100)
    kind: Literal['percent', 'fixed', 'price', 'bxgy'] = 'percent'
    value: float = Field(default=0, ge=0, le=10_000_000)
    buy: int = Field(default=0, ge=0, le=100)
    pay: int = Field(default=0, ge=0, le=100)
    first_purchase: bool = False
    only_inactive: bool = False
    min_total: Optional[float] = Field(default=None, ge=0)
    origins: List[str] = Field(default_factory=list, max_length=30)
    max_uses: Optional[int] = Field(default=None, ge=1)
    max_per_contact: Optional[int] = Field(default=None, ge=1)
    requires_coupon: bool = False
    stackable: bool = False
    nanda_offers: bool = True

def check_promo(b: PromoIn):
    if b.kind == 'percent' and not 0 < b.value <= 100: raise HTTPException(400, 'O percentual precisa ser entre 1 e 100.')
    if b.kind in ('fixed', 'price') and b.value <= 0 and b.kind == 'fixed': raise HTTPException(400, 'Informe o valor do desconto.')
    if b.kind == 'bxgy' and not (b.buy >= 2 and 1 <= b.pay < b.buy): raise HTTPException(400, 'Em "leve X pague Y", X precisa ser maior que Y (ex.: leve 3, pague 2).')
    if b.kind == 'price' and b.scope == 'all': raise HTTPException(400, 'Preço especial vale para produtos ou categorias escolhidos.')
    if b.scope == 'categories' and not b.categories: raise HTTPException(400, 'Escolha ao menos uma categoria.')
    if b.scope == 'products' and not b.product_ids: raise HTTPException(400, 'Escolha ao menos um produto ou serviço.')
    if b.starts and b.ends and b.ends < b.starts: raise HTTPException(400, 'O fim precisa ser depois do início.')
    if bool(b.hour_from) != bool(b.hour_to) or (b.hour_from and b.hour_to <= b.hour_from): raise HTTPException(400, 'Confira o horário (das ... às ...).')
    if any(d not in range(7) for d in b.weekdays): raise HTTPException(400, 'Dia da semana inválido.')

def rule_text(p, names=None):
    """Descrição curta, usada na tela e no que a Nanda sabe."""
    k = p['kind']
    what = 'em tudo' if p['scope'] == 'all' else ('em ' + ', '.join(p.get('categories') or [])) if p['scope'] == 'categories' else \
        ('em ' + ', '.join((names or {}).get(i, '?') for i in p.get('product_ids') or []))
    v = f'{p["value"]:g}% de desconto' if k == 'percent' else f'{brl(p["value"])} de desconto' if k == 'fixed' else \
        f'por {brl(p["value"])} cada' if k == 'price' else f'leve {p["buy"]}, pague {p["pay"]}'
    t = f'{v} {what}'
    cond = []
    if p.get('first_purchase'): cond.append('só na primeira compra')
    if p.get('only_inactive'): cond.append('para quem não compra há um tempo')
    if p.get('min_total'): cond.append(f'em compras a partir de {brl(p["min_total"])}')
    if p.get('weekdays'): cond.append('às ' + ', '.join(WEEK[d] for d in sorted(p['weekdays'])))
    if p.get('hour_from'): cond.append(f'das {p["hour_from"]} às {p["hour_to"]}')
    if p.get('ends'): cond.append('até ' + datetime.strptime(p['ends'], '%Y-%m-%d').strftime('%d/%m'))
    if p.get('requires_coupon'): cond.append('com cupom')
    return t + (' (' + ', '.join(cond) + ')' if cond else '')

def state_of(p, company, at=None):
    """ativa | agendada | encerrada | pausada | fora_do_horario"""
    if not p.get('active'): return 'pausada'
    t = (at or now()).astimezone(agenda.tz_of(company))
    d = t.date().isoformat()
    if p.get('starts') and d < p['starts']: return 'agendada'
    if p.get('ends') and d > p['ends']: return 'encerrada'
    if p.get('max_uses') and (p.get('uses') or 0) >= p['max_uses']: return 'esgotada'
    if p.get('weekdays') and t.weekday() not in p['weekdays']: return 'fora_do_horario'
    if p.get('hour_from') and not (p['hour_from'] <= t.strftime('%H:%M') < p['hour_to']): return 'fora_do_horario'
    return 'ativa'

async def uses_of(promo_id, contact_id=None, skip_sale=None):
    q = {'status': {'$in': ['ok', 'pending']}, 'promos.promo_id': promo_id}
    if contact_id: q['contact_id'] = contact_id
    if skip_sale: q['id'] = {'$ne': skip_sale}
    return await db.sales.count_documents(q)

# ---------- cálculo ----------
def eligible(p, it):
    if p['scope'] == 'all': return True
    if p['scope'] == 'categories': return (it.get('category') or '') in (p.get('categories') or [])
    return it.get('product_id') in (p.get('product_ids') or [])

def promo_discount(p, items):
    """Quanto a promoção desconta e quanto cai em cada item (índice -> valor)."""
    base = {k: max(0.0, it['qty'] * it['price'] - (it.get('discount') or 0)) for k, it in enumerate(items) if eligible(p, it)}
    if not base: return 0.0, {}
    per = {}
    if p['kind'] == 'percent':
        per = {k: v * p['value'] / 100 for k, v in base.items()}
    elif p['kind'] == 'fixed':
        tot = sum(base.values())
        d = min(p['value'], tot)
        per = {k: d * v / tot for k, v in base.items()} if tot else {}
    elif p['kind'] == 'price':
        per = {k: min(base[k], max(0.0, (items[k]['price'] - p['value']) * items[k]['qty'])) for k in base}
    elif p['kind'] == 'bxgy':
        for k in base:
            free = int(items[k]['qty'] // p['buy']) * (p['buy'] - p['pay'])
            per[k] = min(base[k], free * items[k]['price'])
    per = {k: round(v, 2) for k, v in per.items() if v > 0}
    return round(sum(per.values()), 2), per

async def why_not(p, company, contact, items, coupon_ok, skip_sale=None):
    st = state_of(p, company)
    if st != 'ativa': return {'pausada': 'está pausada', 'agendada': 'ainda não começou', 'encerrada': 'já terminou', 'esgotada': 'atingiu o limite de usos', 'fora_do_horario': 'não vale neste dia ou horário'}[st]
    if p.get('requires_coupon') and not coupon_ok: return 'precisa de cupom'
    if not any(eligible(p, it) for it in items): return 'nenhum item da venda entra nela'
    sales = contact.get('sales_count') or 0
    if skip_sale: sales = await db.sales.count_documents({'contact_id': contact['id'], 'status': 'ok', 'id': {'$ne': skip_sale}})
    if p.get('first_purchase') and sales: return 'só vale na primeira compra'
    if p.get('only_inactive'):
        from origins import status_of
        if status_of(contact, company.get('inactive_days') or 90) != 'inativo': return 'só vale para cliente inativo'
    if p.get('min_total') and sum(max(0, it['qty'] * it['price'] - (it.get('discount') or 0)) for it in items) < p['min_total']: return f'compra mínima de {brl(p["min_total"])}'
    if p.get('origins'):
        o = contact.get('origin') or ('anuncio' if contact.get('ad') else None)
        if o not in p['origins']: return 'não vale para a origem deste cliente'
    if p.get('max_per_contact') and await uses_of(p['id'], contact['id'], skip_sale) >= p['max_per_contact']: return 'o cliente já usou o máximo de vezes'
    return None

async def find_coupon(company_id, code, contact_id=None, skip_sale=None):
    """Devolve (cupom, motivo se não vale)."""
    code = (code or '').strip().upper()
    if not code: return None, None
    c = await db.coupons.find_one({'company_id': company_id, 'code': code}, {'_id': 0})
    if not c: return None, 'cupom não existe'
    if not c.get('active', True): return c, 'cupom desativado'
    if c.get('expires') and now().date().isoformat() > c['expires']: return c, 'cupom vencido'
    if c.get('contact_id') and contact_id and c['contact_id'] != contact_id: return c, 'cupom é de outro cliente'
    q = {'status': {'$in': ['ok', 'pending']}, 'promos.coupon_id': c['id']}
    if skip_sale: q['id'] = {'$ne': skip_sale}
    if c.get('max_uses') and await db.sales.count_documents(q) >= c['max_uses']: return c, 'cupom já foi usado o máximo de vezes'
    if c.get('one_per_contact') and contact_id and await db.sales.count_documents({**q, 'contact_id': contact_id}): return c, 'este cliente já usou o cupom'
    return c, None

async def price(company, contact, items, promo_ids=None, coupon=None, skip_sale=None, auto=True):
    """Aplica promoções escolhidas (e as automáticas, se auto) e o cupom. Devolve itens com o desconto de promoção,
    as promoções aplicadas e as disponíveis com o motivo de quem não vale."""
    promos = await db.promos.find({'company_id': company['id']}, {'_id': 0}).to_list(500)
    for p in promos: p['uses'] = await uses_of(p['id'], skip_sale=skip_sale)
    names = {x['id']: x['name'] for x in await db.products.find({'company_id': company['id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(1000)}
    cp, cp_err = await find_coupon(company['id'], coupon, contact['id'], skip_sale) if coupon else (None, None)
    chosen = set(promo_ids or [])
    if cp and not cp_err: chosen.add(cp['promo_id'])
    avail, ok = [], []
    for p in promos:
        if state_of(p, company) in ('pausada', 'encerrada'): continue
        coupon_ok = bool(cp and not cp_err and cp['promo_id'] == p['id'])
        reason = await why_not(p, company, contact, items, coupon_ok, skip_sale)
        d, _ = promo_discount(p, items) if not reason else (0, {})
        if not reason and d <= 0: reason = 'não dá desconto nesta venda'
        avail.append({'id': p['id'], 'name': p['name'], 'rule': rule_text(p, names), 'requires_coupon': p.get('requires_coupon'), 'stackable': p.get('stackable'),
                      'discount': d, 'reason': reason, 'auto': not p.get('requires_coupon')})
        if not reason and (p['id'] in chosen or (auto and not p.get('requires_coupon') and promo_ids is None)): ok.append((p, coupon_ok))
    # não cumulativas: fica só a que dá mais desconto (cupom tem prioridade) junto com as cumulativas
    solo = [x for x in ok if not x[0].get('stackable')]
    if len(solo) > 1:
        best = max(solo, key=lambda x: (x[1], promo_discount(x[0], items)[0]))
        ok = [x for x in ok if x[0].get('stackable') or x is best]
    work = [dict(it, promo_discount=0.0) for it in items]
    applied = []
    for p, coupon_ok in ok:  # cada promoção calcula sobre o que sobrou depois das anteriores
        cur = [dict(it, discount=(it.get('discount') or 0) + it['promo_discount']) for it in work]
        d, per = promo_discount(p, cur)
        if d <= 0: continue
        for k, v in per.items(): work[k]['promo_discount'] = round(work[k]['promo_discount'] + v, 2)
        applied.append({'promo_id': p['id'], 'name': p['name'], 'discount': d, 'coupon_id': cp['id'] if coupon_ok else None, 'code': cp['code'] if coupon_ok else None})
    return {'items': work, 'applied': applied, 'available': avail, 'discount': round(sum(a['discount'] for a in applied), 2),
            'coupon': {'code': (coupon or '').strip().upper(), 'ok': bool(cp and not cp_err and any(a.get('coupon_id') for a in applied)), 'reason': cp_err or (None if not coupon or any(a.get('coupon_id') for a in applied) else 'a promoção do cupom não vale para esta venda')} if coupon else None}

class EvalIn(BaseModel):
    contact_id: str
    items: List[dict] = Field(default_factory=list, max_length=50)
    promo_ids: Optional[List[str]] = None
    coupon: Optional[str] = Field(default=None, max_length=30)
    sale_id: Optional[str] = None

@router.post('/promos/evaluate')
async def evaluate(body: EvalIn, user=Depends(current_user)):
    c = await get_contact(user, body.contact_id)
    items = [{'product_id': i.get('product_id'), 'category': str(i.get('category') or ''), 'qty': float(i.get('qty') or 0), 'price': float(i.get('price') or 0),
              'discount': float(i.get('discount') or 0)} for i in body.items if float(i.get('qty') or 0) > 0]
    cat = {p['id']: p for p in await db.products.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'category': 1}).to_list(1000)}
    for i in items:
        if i['product_id'] in cat and not i['category']: i['category'] = cat[i['product_id']].get('category') or ''
    r = await price(user['company'], c, items, body.promo_ids, body.coupon, body.sale_id)
    gross = sum(i['qty'] * i['price'] for i in items); manual = sum(i['discount'] for i in items)
    lim = user['company'].get('max_discount_pct')
    r['manual_pct'] = round(100 * manual / gross, 1) if gross else 0
    r['needs_approval'] = bool(lim is not None and user['role'] == 'agent' and r['manual_pct'] > lim)
    r['limit'] = lim
    return r

# ---------- cadastro ----------
@router.get('/promos')
async def list_promos(user=Depends(current_user)):
    company = user['company']
    names = {p['id']: p['name'] for p in await db.products.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(1000)}
    rows = await db.promos.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(500)
    stats = {}
    for s in await db.sales.find({'company_id': user['company_id'], 'status': 'ok', 'promos.0': {'$exists': True}}, {'_id': 0, 'promos': 1, 'total': 1, 'contact_id': 1}).to_list(50000):
        for a in s['promos']:
            x = stats.setdefault(a['promo_id'], {'uses': 0, 'discount': 0.0, 'revenue': 0.0, 'clients': set()})
            x['uses'] += 1; x['discount'] += a['discount']; x['revenue'] += s['total']; x['clients'].add(s['contact_id'])
    out = []
    for p in rows:
        st = stats.get(p['id'], {})
        p['uses'] = st.get('uses', 0)
        out.append({**p, 'state': state_of(p, company), 'rule': rule_text(p, names), 'discount_given': round(st.get('discount', 0), 2),
                    'revenue': round(st.get('revenue', 0), 2), 'clients': len(st.get('clients', ())),
                    'coupons': await db.coupons.count_documents({'promo_id': p['id']})})
    return {'items': out, 'max_discount_pct': company.get('max_discount_pct')}

@router.post('/promos')
async def add_promo(body: PromoIn, request: Request, user=Depends(managers)):
    check_promo(body)
    if await db.promos.count_documents({'company_id': user['company_id']}) >= 200: raise HTTPException(400, 'Limite de 200 promoções.')
    p = {'id': uid(), 'company_id': user['company_id'], **body.model_dump(), 'name': body.name.strip(), 'created_at': now(), 'by': user['name']}
    await db.promos.insert_one(p.copy())
    await audit(request, user, 'Criou promoção', p['name'])
    p.pop('_id', None)
    return p

@router.put('/promos/{pid}')
async def edit_promo(pid: str, body: PromoIn, request: Request, user=Depends(managers)):
    p = await db.promos.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(404, 'Promoção não encontrada.')
    check_promo(body)
    upd = {**body.model_dump(), 'name': body.name.strip(), 'updated_at': now()}
    await db.promos.update_one({'id': pid}, {'$set': upd})
    await audit(request, user, 'Editou promoção', upd['name'])
    return {**p, **upd}

@router.delete('/promos/{pid}')
async def del_promo(pid: str, request: Request, user=Depends(managers)):
    p = await db.promos.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(404, 'Promoção não encontrada.')
    if await db.sales.find_one({'promos.promo_id': pid}):  # já usada: pausa, para o histórico continuar certo
        await db.promos.update_one({'id': pid}, {'$set': {'active': False}})
        return {'ok': True, 'paused': True}
    await db.promos.delete_one({'id': pid}); await db.coupons.delete_many({'promo_id': pid})
    await audit(request, user, 'Apagou promoção', p['name'])
    return {'ok': True}

class LimitIn(BaseModel):
    max_discount_pct: Optional[float] = Field(default=None, ge=0, le=100)

@router.put('/promos-settings')
async def set_limit(body: LimitIn, user=Depends(managers)):
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'max_discount_pct': body.max_discount_pct}})
    return {'max_discount_pct': body.max_discount_pct}

# ---------- cupons ----------
class CouponIn(BaseModel):
    code: Optional[str] = Field(default=None, max_length=20)
    count: int = Field(default=1, ge=1, le=500)  # gerar vários códigos individuais de uma vez
    prefix: str = Field(default='', max_length=10)
    max_uses: Optional[int] = Field(default=None, ge=1)
    one_per_contact: bool = True
    expires: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    contact_id: Optional[str] = None
    public: bool = False  # a Nanda pode passar o código para quem perguntar

CODE_OK = re.compile(r'^[A-Z0-9]{3,20}$')

async def make_coupon(company_id, promo_id, code, body, contact_id=None, by=None):
    c = {'id': uid(), 'company_id': company_id, 'promo_id': promo_id, 'code': code, 'max_uses': body.max_uses, 'one_per_contact': body.one_per_contact,
         'expires': body.expires, 'contact_id': contact_id, 'public': body.public, 'active': True, 'created_at': now(), 'by': by}
    await db.coupons.insert_one(c.copy())
    c.pop('_id', None)
    return c

def random_code(prefix):
    return (re.sub(r'[^A-Z0-9]', '', prefix.upper()) + ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6)))[:20]

@router.get('/promos/{pid}/coupons')
async def list_coupons(pid: str, user=Depends(current_user)):
    rows = await db.coupons.find({'company_id': user['company_id'], 'promo_id': pid}, {'_id': 0}).sort('created_at', -1).to_list(2000)
    used = {}
    for s in await db.sales.find({'company_id': user['company_id'], 'status': {'$in': ['ok', 'pending']}, 'promos.promo_id': pid}, {'_id': 0, 'promos': 1}).to_list(50000):
        for a in s['promos']:
            if a.get('coupon_id'): used[a['coupon_id']] = used.get(a['coupon_id'], 0) + 1
    names = {}
    ids = [r['contact_id'] for r in rows if r.get('contact_id')]
    if ids: names = {c['id']: c['name'] for c in await db.contacts.find({'id': {'$in': ids}}, {'_id': 0, 'id': 1, 'name': 1}).to_list(len(ids))}
    return [{**r, 'used': used.get(r['id'], 0), 'contact_name': names.get(r.get('contact_id'))} for r in rows]

@router.post('/promos/{pid}/coupons')
async def add_coupons(pid: str, body: CouponIn, request: Request, user=Depends(managers)):
    p = await db.promos.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(404, 'Promoção não encontrada.')
    if body.contact_id: await get_contact(user, body.contact_id)
    out = []
    if body.code:
        code = body.code.strip().upper()
        if not CODE_OK.match(code): raise HTTPException(400, 'Use de 3 a 20 letras ou números, sem espaço (ex.: BLACK20).')
        if await db.coupons.find_one({'company_id': user['company_id'], 'code': code}): raise HTTPException(409, 'Já existe um cupom com esse código.')
        out.append(await make_coupon(user['company_id'], pid, code, body, body.contact_id, user['name']))
    else:
        for _ in range(body.count):
            for _try in range(5):
                code = random_code(body.prefix)
                if not await db.coupons.find_one({'company_id': user['company_id'], 'code': code}): break
            out.append(await make_coupon(user['company_id'], pid, code, body.model_copy(update={'max_uses': body.max_uses or 1}), body.contact_id, user['name']))
    if not p.get('requires_coupon'): await db.promos.update_one({'id': pid}, {'$set': {'requires_coupon': True}})
    await audit(request, user, 'Criou cupom', f'{p["name"]} · {len(out)}')
    return out

@router.delete('/coupons/{cid}')
async def del_coupon(cid: str, user=Depends(managers)):
    c = await db.coupons.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Cupom não encontrado.')
    if await db.sales.find_one({'promos.coupon_id': cid}): await db.coupons.update_one({'id': cid}, {'$set': {'active': False}}); return {'ok': True, 'deactivated': True}
    await db.coupons.delete_one({'id': cid})
    return {'ok': True}

# ---------- o que a Nanda sabe ----------
WORD = re.compile(r'\b[A-Za-z0-9]{3,20}\b')

async def nanda_block(company, contact, last_text=''):
    promos = [p for p in await db.promos.find({'company_id': company['id'], 'active': True, 'nanda_offers': {'$ne': False}}, {'_id': 0}).to_list(200)]
    names = {p['id']: p['name'] for p in await db.products.find({'company_id': company['id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(1000)}
    live = []
    for p in promos:
        p['uses'] = await uses_of(p['id'])
        if state_of(p, company) != 'ativa': continue
        line = f'- {p["name"]}: {rule_text(p, names)}.' + (f' {p["description"].strip()}' if p.get('description', '').strip() else '')
        if p.get('requires_coupon'):
            pub = await db.coupons.find_one({'promo_id': p['id'], 'public': True, 'active': True, 'contact_id': None}, {'_id': 0, 'code': 1})
            line += f' Cupom que pode passar: {pub["code"]}.' if pub else ' Só com cupom que o cliente já recebeu (não invente código).'
        live.append(line)
    out = ''
    if live:
        out += '\nPROMOÇÕES VALENDO AGORA (só ofereça estas, do jeito que estão escritas; o desconto é aplicado pelo time na hora da venda):\n' + '\n'.join(live) + '\n'
    # o cliente citou um código? conferimos de verdade
    words = {w.upper() for w in WORD.findall(last_text or '') if any(ch.isdigit() for ch in w) or w.isupper()}
    if words:
        for c in await db.coupons.find({'company_id': company['id'], 'code': {'$in': list(words)}}, {'_id': 0}).to_list(5):
            _, err = await find_coupon(company['id'], c['code'], contact['id'])
            p = await db.promos.find_one({'id': c['promo_id']}, {'_id': 0})
            if p: p['uses'] = await uses_of(p['id'])
            if not err and p and state_of(p, company) != 'ativa': err = 'a promoção não está valendo agora'
            out += f'\nO cliente citou o cupom {c["code"]}: ' + (f'VÁLIDO, dá {rule_text(p, names)}. Confirme e diga que o desconto entra no fechamento.' if not err else f'NÃO VALE ({err}). Explique com gentileza.') + '\n'
    return out
