"""Carteira do cliente e segmentos da base.

Carteira: o que o cliente tem (ativo e até quando), o que já teve (venceu e não renovou) e o que pode comprar
(próximas ofertas do catálogo). Item com validade (duration_days) gera vencimento; recompra antes de vencer soma
no fim do período atual. Fica guardada no contato (holdings) para filtrar rápido.

Segmentos: públicos montados com regras (todas precisam valer). Ficam salvos e se atualizam sozinhos:
quem passa a se encaixar entra, quem deixa de se encaixar sai.
"""
import re
import csv, io
from datetime import timedelta
from typing import Optional, List, Literal, Any
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers, owners
from whatsapp import get_contact, aware

router = APIRouter()
SOON_DAYS = 15

# ---------- carteira ----------
def compute_holdings(sales, products, cancels=None):
    """sales: vendas válidas do cliente (com items/date). products: id -> produto. cancels: id do produto -> quando a assinatura foi cancelada."""
    h = {}
    for s in sorted(sales, key=lambda x: aware(x['date'])):
        d = aware(s['date'])
        for i in s['items']:
            key = i.get('product_id') or 'avulso:' + i['name']
            p = products.get(i.get('product_id')) or {}
            x = h.setdefault(key, {'key': key, 'product_id': i.get('product_id'), 'name': p.get('name') or i['name'], 'category': p.get('category') or i.get('category') or '',
                                   'first_at': d, 'last_at': d, 'count': 0, 'spent': 0.0, 'expires_at': None})
            x['last_at'] = d; x['count'] += 1; x['_new'] = max(x.get('_new') or d, aware(s.get('created_at')) or d)  # quando foi registrada
            x['spent'] = round(x['spent'] + (i.get('total') or 0), 2)
            dur = p.get('duration_days')
            if i.get('expires_at'): x['expires_at'] = max(aware(i['expires_at']), x['expires_at']) if x['expires_at'] else aware(i['expires_at'])  # vencimento informado (planilha)
            elif p.get('recurring'): x['recurring'] = True; x['expires_at'] = None  # assinatura: segue ativa, sem vencimento
            elif dur:
                start = max(d, x['expires_at']) if x['expires_at'] else d  # renovou antes de vencer: soma no fim
                x['expires_at'] = start + timedelta(days=dur * max(1, int(round(i.get('qty') or 1))))
    for x in h.values():  # assinatura cancelada: termina na data do cancelamento (se comprou de novo depois, voltou a valer)
        ca = aware((cancels or {}).get(x.get('product_id')))
        # comprou de novo depois de cancelar (mesmo com a venda datada do mesmo dia, antes da hora do cancelamento): voltou a valer
        if x.get('recurring') and ca and ca >= x['last_at'] and ca >= x.get('_new', x['last_at']): x['cancelled_at'] = ca; x['expires_at'] = ca
        x.pop('_new', None)
    return list(h.values())

def cancels_of(contact):
    return {k: v.get('at') for k, v in ((contact or {}).get('sub_cancel') or {}).items()}

def monthly(p):
    """Quanto a assinatura rende por mês (o preço do catálogo dividido pelo período)."""
    d = p.get('duration_days') or 30
    return round((p.get('price') or 0) * 30 / d, 2)

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
    sales = await db.sales.find({'company_id': company_id, 'contact_id': contact_id, 'status': 'ok'}, {'_id': 0, 'items': 1, 'date': 1, 'created_at': 1}).to_list(1000)
    ct = await db.contacts.find_one({'id': contact_id}, {'_id': 0, 'sub_cancel': 1})
    hs = compute_holdings(sales, products, cancels_of(ct))
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

class SubIn(BaseModel):
    product_id: str = Field(max_length=40)
    cancel: bool = True
    at: Optional[str] = Field(default=None, max_length=10)  # AAAA-MM-DD; sem data = hoje
    reason: str = Field(default='', max_length=200)

@router.post('/contacts/{contact_id}/subscription')
async def subscription(contact_id: str, body: SubIn, request: Request, user=Depends(current_user)):
    """Cancelou (ou voltou) a assinatura: sem isso, quem cancela o cartão continuaria aparecendo como ativo para sempre."""
    from datetime import datetime, time as dtime
    from agenda import tz_of
    from whatsapp import note
    c = await get_contact(user, contact_id)
    p = await db.products.find_one({'company_id': user['company_id'], 'id': body.product_id}, {'_id': 0})
    if not p or not p.get('recurring'): raise HTTPException(400, 'Esse item não é uma assinatura.')
    if body.cancel:
        tz = tz_of(user['company'])
        try: d = datetime.strptime(body.at, '%Y-%m-%d').date() if body.at else now().astimezone(tz).date()
        except ValueError: raise HTTPException(400, 'Data inválida.')
        if d > (now() + timedelta(days=366)).date(): raise HTTPException(400, 'A data do cancelamento está longe demais.')
        at = now() if d == now().astimezone(tz).date() else datetime.combine(d, dtime(23, 59), tz)  # hoje: já sai; data futura: vale até o fim daquele dia
        await db.contacts.update_one({'id': c['id']}, {'$set': {f'sub_cancel.{p["id"]}': {'at': at, 'reason': body.reason.strip(), 'by': user['name'], 'marked_at': now()}}})
        txt = f'{user["name"]} marcou que a assinatura {p["name"]} foi cancelada' + (f' (vale até {d:%d/%m/%Y})' if d > now().astimezone(tz).date() else f' em {d:%d/%m/%Y}') + (f' · motivo: {body.reason.strip()}' if body.reason.strip() else '')
    else:
        await db.contacts.update_one({'id': c['id']}, {'$unset': {f'sub_cancel.{p["id"]}': ''}})
        txt = f'{user["name"]} marcou que a assinatura {p["name"]} voltou a valer'
    await note(user['company_id'], c['id'], txt)
    await audit(request, user, 'Cancelou assinatura' if body.cancel else 'Reativou assinatura', f'{c.get("name")} · {p["name"]}')
    await refresh_holdings(user['company_id'], c['id'])
    return await wallet(contact_id, user)

# ---------- segmentos ----------
FIELDS = {
    'product': 'Item', 'category': 'Categoria', 'expires_in': 'Vence nos próximos dias', 'expired_within': 'Venceu e não renovou (até X dias)',
    'last_purchase': 'Última compra', 'purchases': 'Número de compras', 'spent': 'Total gasto', 'kind': 'Situação', 'origin': 'Origem',
    'stage': 'Etapa do funil', 'tag': 'Etiqueta', 'team': 'Equipe', 'owner': 'Responsável', 'promo': 'Usou promoção', 'created': 'Chegou há', 'sub': 'Assinatura',
}

class Rule(BaseModel):
    field: Literal['product', 'category', 'expires_in', 'expired_within', 'last_purchase', 'purchases', 'spent', 'kind', 'origin', 'stage', 'tag', 'team', 'owner', 'promo', 'created', 'milestone', 'base', 'sub']
    op: str = Field(default='is', max_length=12)  # is | not | active | ever | never | gte | lte | more | less
    value: Any = None

class SegIn(BaseModel):
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    q: str = Field(default='', max_length=80)  # busca por nome ou telefone, só na lista
    sort: Literal['recent', 'spent', 'expiry', 'name'] = 'recent'
    limit: int = Field(default=300, ge=1, le=5000)  # a lista vem em páginas: "Carregar mais" pede mais 300

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
    if f == 'base': return is_agenda(c) == (v == 'agenda')
    if f == 'sub':  # assinatura que renova sozinha: ativa agora ou cancelada (e não voltou)
        subs = [x for x in hs if x.get('recurring')]
        if v == 'cancelled': return any(x.get('cancelled_at') and state_of(x) == 'vencido' for x in subs)
        return any(state_of(x) != 'vencido' for x in subs)
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

IN_AT = ('last_in_at', 'wa_last_in_at', 'ig_last_in_at', 'fb_last_in_at', 'tg_last_in_at', 'em_last_in_at')

def is_agenda(c):
    """Contato que só veio da agenda do celular (ou só recebeu mensagem): nunca escreveu, nunca comprou e o time não cadastrou.
    Não é prospect: fica fora da base e das campanhas, a não ser que o público peça ele de propósito."""
    return not any(c.get(k) for k in IN_AT) and not c.get('sales_count') and c.get('source') not in ('manual', 'importado')

def cold_import(c):
    """Veio de planilha e nunca falou com a empresa: fica em Clientes, mas fora do funil e dos contatos novos."""
    return c.get('source') == 'importado' and not any(c.get(k) for k in IN_AT)

def no_campaign(c):
    """Prospect importado que não autorizou mensagens: campanha pelo WhatsApp oficial derruba a qualidade do número."""
    return cold_import(c) and not c.get('consent') and not c.get('sales_count')

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
                                                    'sales_count': 1, 'value': 1, 'last_sale_at': 1, 'created_at': 1, 'holdings': 1, 'next_expiry': 1, 'opt_out': 1, 'last_in_at': 1, 'channel': 1, 'ig_username': 1,
                                                    'source': 1, 'wa_last_in_at': 1, 'ig_last_in_at': 1, 'fb_last_in_at': 1, 'tg_last_in_at': 1, 'em_last_in_at': 1, 'internal': 1, 'consent': 1}).to_list(50000)
    own = await own_numbers(company)
    rows = [c for c in rows if (c.get('wa_id') or '') not in own and not c.get('internal')]  # o próprio número da empresa e contatos de teste não são clientes
    if not any(r.field == 'base' for r in rules): rules = [*rules, Rule(field='base', value='conversaram')]  # agenda só quando pedida
    return [c for c in rows if all(match(c, r, ctx) for r in rules)]

async def own_numbers(company):
    """Números que são da própria empresa: o WhatsApp conectado e o telefone do cadastro."""
    own = {re.sub(r'\D', '', n.get('display_phone') or '') for n in await db.wa_numbers.find({'company_id': company['id']}, {'_id': 0, 'display_phone': 1}).to_list(20)} - {''}
    ph = re.sub(r'\D', '', company.get('phone') or '')
    if 10 <= len(ph) <= 11: own.add('55' + ph)
    return own

BASE_FIELDS = {'internal': 1, 'wa_id': 1, 'source': 1, 'sales_count': 1, 'consent': 1, **{k: 1 for k in IN_AT}}

def in_base(c, own=()):
    """Conta como prospect ou cliente? Fora: quem só está na agenda do celular, quem foi tirado da base e o número da própria empresa."""
    return not c.get('internal') and not is_agenda(c) and (c.get('wa_id') or '') not in own

def reachable(c): return not c.get('opt_out') and bool(c.get('wa_id')) and not no_campaign(c)  # campanha sai pelo WhatsApp

def row_out(c, names, inactive_days=None):
    from origins import status_of
    hs = c.get('holdings') or []
    return {'kind': 'agenda' if is_agenda(c) else status_of(c, inactive_days), 'channel': c.get('channel') or 'whatsapp', 'ig': c.get('ig_username'), 'origin': c.get('origin') or ('anuncio' if c.get('ad') else None),
            'last_in_at': max([aware(c.get(k)) for k in IN_AT if c.get(k)], default=None), 'agenda': is_agenda(c), 'id': c['id'], 'name': c['name'], 'wa_id': c.get('wa_id'), 'stage': c.get('stage'), 'owner': names.get(c.get('assigned_to')), 'spent': c.get('value') if c.get('sales_count') else 0,
            'purchases': c.get('sales_count') or 0, 'last_sale_at': c.get('last_sale_at'), 'next_expiry': c.get('next_expiry'), 'opt_out': bool(c.get('opt_out')),
            'has': [{'name': x['name'], 'state': state_of(x), 'expires_at': x.get('expires_at')} for x in hs][:6]}

@router.post('/segments/preview')
async def preview(body: SegIn, user=Depends(current_user)):
    from origins import status_of
    rows = await run_segment(user, body.rules)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    days = user['company'].get('inactive_days')
    kinds = {'prospect': 0, 'cliente': 0, 'inativo': 0}
    kinds['agenda'] = 0
    for c in rows: kinds['agenda' if is_agenda(c) else status_of(c, days)] += 1
    # quem já comprou e vence primeiro vem antes; depois quem gastou mais; depois quem falou por último
    far, old = now() + timedelta(days=99999), now() - timedelta(days=99999)
    last = lambda c: max([aware(c.get(k)) for k in IN_AT if c.get(k)] or [old])
    spent = lambda c: (c.get('value') or 0) if c.get('sales_count') else 0
    if body.sort == 'spent': rows.sort(key=lambda c: (-spent(c), -last(c).timestamp()))
    elif body.sort == 'expiry': rows.sort(key=lambda c: (aware(c.get('next_expiry')) or far, -spent(c)))
    elif body.sort == 'name': rows.sort(key=lambda c: (c.get('name') or '').lower())
    else: rows.sort(key=lambda c: (-last(c).timestamp(), -(aware(c.get('created_at')) or old).timestamp()))  # quem falou por último primeiro; quem nunca falou, no fim
    shown = rows
    q = body.q.strip().lower()
    if q:
        dq = re.sub(r'\D', '', q)
        shown = [c for c in rows if q in (c.get('name') or '').lower() or (len(dq) >= 4 and dq in (c.get('wa_id') or '')) or q.lstrip('@') in (c.get('ig_username') or '').lower()]
    return {'count': len(rows), 'spent': round(sum((c.get('value') or 0) for c in rows if c.get('sales_count')), 2), 'kinds': kinds,
            'reachable': sum(1 for c in rows if reachable(c)), 'found': len(shown), 'items': [row_out(c, names, days) for c in shown[:body.limit]]}

@router.get('/segments/overview')
async def overview(user=Depends(current_user)):
    """Números da base para o topo da página: cada um vira um público com um clique."""
    from origins import status_of
    rows = await run_segment(user, [])
    days = user['company'].get('inactive_days'); t = now()
    k = {'prospect': 0, 'cliente': 0, 'inativo': 0}
    for c in rows: k[status_of(c, days)] += 1
    due = sum(1 for c in rows if any(x.get('expires_at') and t <= aware(x['expires_at']) <= t + timedelta(days=30) for x in c.get('holdings') or []))
    lost = sum(1 for c in rows if any(x.get('expires_at') and t - timedelta(days=30) <= aware(x['expires_at']) < t for x in c.get('holdings') or []))
    agenda = len(await run_segment(user, [Rule(field='base', value='agenda')]))
    products = await products_map(user['company_id'])
    subs = [x for c in rows for x in c.get('holdings') or [] if x.get('recurring') and state_of(x) != 'vencido']
    gone = sum(1 for c in rows for x in c.get('holdings') or [] if x.get('cancelled_at') and t - timedelta(days=30) <= aware(x['cancelled_at']) <= t)
    from origins import origins_of
    names = {o['id']: o['name'] for o in await origins_of(user['company'])}
    oc = {}
    for c in rows:
        o = c.get('origin') or ('anuncio' if c.get('ad') else '_none'); oc[o] = oc.get(o, 0) + 1
    orgs = sorted([{'id': o, 'name': names.get(o, 'Não sabemos' if o == '_none' else o), 'count': n, 'paid': o == 'anuncio'} for o, n in oc.items()],
                  key=lambda x: (x['id'] == '_none', not x['paid'], -x['count']))
    return {'origins': orgs, 'total': len(rows), **k, 'due30': due, 'expired30': lost, 'subs': len(subs), 'mrr': round(sum(monthly(products.get(x.get('product_id')) or {}) for x in subs), 2), 'subs_lost30': gone, 'inactive_days': days, 'agenda': agenda, 'imported': sum(1 for c in rows if c.get('source') == 'importado')}

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
async def export(body: SegIn, request: Request, user=Depends(owners)):
    """Planilha Excel da base (ou do público filtrado). Só o dono baixa: é a lista de clientes da empresa inteira."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from origins import status_of, origins_of, STATUS
    rows = await run_segment(user, body.rules)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    onames = {o['id']: o['name'] for o in await origins_of(user['company'])}
    days = user['company'].get('inactive_days')
    full = {c['id']: c for c in await db.contacts.find({'id': {'$in': [r['id'] for r in rows]}}, {'_id': 0, 'id': 1, 'email': 1, 'tags': 1, 'created_at': 1, 'origin_detail': 1}).to_list(50000)}
    wb = Workbook(); ws = wb.active; ws.title = 'Clientes'
    head = ['Nome', 'Situação', 'Telefone (WhatsApp)', 'Instagram', 'E-mail', 'Origem', 'Detalhe da origem', 'Compras', 'Total gasto (R$)', 'Última compra',
            'Tem', 'Próximo vencimento', 'Etiquetas', 'Responsável', 'Chegou em', 'Não quer mensagens']
    ws.append(head)
    d = lambda v: aware(v).replace(tzinfo=None) if v else None
    for c in rows:
        o = row_out(c, names); x = full.get(c['id'], {})
        org = c.get('origin') or ('anuncio' if c.get('ad') else None)
        ws.append([o['name'], STATUS.get(status_of(c, days), ''), ('+' + o['wa_id']) if o['wa_id'] else '', ('@' + c['ig_username']) if c.get('ig_username') else '',
                   x.get('email') or '', onames.get(org, '') if org else 'Não sabemos', x.get('origin_detail') or '', o['purchases'], round(o['spent'] or 0, 2),
                   d(o['last_sale_at']), ', '.join(h['name'] for h in o['has']), d(o['next_expiry']), ', '.join(x.get('tags') or []), o['owner'] or '',
                   d(x.get('created_at')), 'sim' if o['opt_out'] else ''])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF'); cell.fill = PatternFill('solid', fgColor='E8603F'); cell.alignment = Alignment(vertical='center')
    widths = [28, 16, 20, 18, 26, 16, 22, 10, 16, 14, 34, 18, 22, 18, 14, 16]
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    for r in ws.iter_rows(min_row=2):
        r[8].number_format = '#,##0.00'
        for k in (9, 11, 14): r[k].number_format = 'DD/MM/YYYY'
    ws.freeze_panes = 'A2'; ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO(); wb.save(buf)
    await audit(request, user, 'Exportou a base de clientes em Excel', f'{len(rows)} contatos')
    fn = f'clientes-{now().strftime("%Y-%m-%d")}.xlsx'
    return Response(buf.getvalue(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': f'attachment; filename="{fn}"'})
