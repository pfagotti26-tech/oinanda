"""Admin da plataforma (só o Paulo): visão geral, financeiro, clientes e ficha do cliente.

Todas as telas aceitam período (start e end em AAAA-MM-DD, no horário de São Paulo) e filtros.
A conta do próprio admin aparece na lista, marcada como interna, e fica fora dos números.
"""
import os, io, csv
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from core import db, now, uid, audit, admin, client_ip, ADMIN_EMAIL
from whatsapp import aware
import billing
from billing import PLANS, plan_of, kind_of, limits, monthly_value, GRACE_DAYS

router = APIRouter()
SP = ZoneInfo('America/Sao_Paulo')
MONTHS = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']

def period(start, end):
    """(início, fim exclusivo, início anterior, fim anterior). Sem datas: do dia 1 deste mês até hoje."""
    today = now().astimezone(SP).date()
    def d(v, default):
        try: return datetime.strptime(v, '%Y-%m-%d').date() if v else default
        except ValueError: raise HTTPException(400, 'Data inválida. Use AAAA-MM-DD.')
    a, b = d(start, today.replace(day=1)), d(end, today)
    if b < a: a, b = b, a
    t0 = datetime(a.year, a.month, a.day, tzinfo=SP).astimezone(timezone.utc)
    t1 = (datetime(b.year, b.month, b.day, tzinfo=SP) + timedelta(days=1)).astimezone(timezone.utc)
    return t0, t1, t0 - (t1 - t0), t0

def inside(dt, t0, t1):
    dt = aware(dt)
    return bool(dt and t0 <= dt < t1)

async def base():
    """Carrega de uma vez o que as telas precisam, sem uma consulta por empresa."""
    companies = await db.companies.find({}, {'_id': 0}).sort('created_at', -1).to_list(5000)
    users = await db.users.find({}, {'_id': 0, 'company_id': 1, 'role': 1, 'name': 1, 'email': 1, 'active': 1, 'last_seen': 1, 'last_login_at': 1}).to_list(50000)
    month = now().strftime('%Y-%m')
    ai = {u['company_id']: u for u in await db.ai_usage.find({'month': month}, {'_id': 0}).to_list(5000)}
    wa = {w['company_id']: w for w in await db.wa_numbers.find({}, {'_id': 0, 'token_enc': 0}).to_list(5000)}
    refused = {}
    for p in await db.payments.find({'status': 'refused'}, {'_id': 0}).sort('due', -1).to_list(2000):
        refused.setdefault(p['company_id'], p)
    paid = {}
    for p in await db.payments.find({'status': 'paid'}, {'_id': 0}).sort('due', -1).to_list(5000):
        paid.setdefault(p['company_id'], p)
    by = {}
    for u in users: by.setdefault(u['company_id'], []).append(u)
    out = []
    for c in companies:
        us = by.get(c['id'], [])
        owner = next((u for u in us if u['role'] == 'owner'), None)
        last = max([aware(x) for u in us for x in (u.get('last_seen'), u.get('last_login_at')) if x] or [None], key=lambda x: x or datetime.min.replace(tzinfo=timezone.utc))
        lim = limits(c); kind = kind_of(c); b = c.get('billing') or {}
        a = ai.get(c['id']) or {}
        bad = refused.get(c['id']); ok = paid.get(c['id'])
        if bad and ok and aware(ok['due']) >= aware(bad['due']): bad = None
        if kind != 'paying': bad = None
        out.append({'c': c, 'kind': 'suspended' if c.get('status') == 'suspended' else kind, 'plan': lim['plan'], 'lim': lim, 'owner': owner,
                    'users': sum(1 for u in us if u.get('active', True)), 'last_use': last, 'ai_used': (a.get('atend') or 0) if lim.get('atend') else (a.get('suggestions') or 0),
                    'ai_in': a.get('input_tokens') or 0, 'ai_out': a.get('output_tokens') or 0, 'ai_cr': a.get('cache_read_tokens') or 0, 'ai_cw': a.get('cache_write_tokens') or 0,
                    'ai_real': ai_cost(a.get('input_tokens') or 0, a.get('output_tokens') or 0, a.get('cache_read_tokens') or 0, a.get('cache_write_tokens') or 0), 'wa': wa.get(c['id']), 'refused': bad,
                    'cancelled': kind == 'paying' and b.get('status') == 'cancelled', 'mrr': monthly_value(c),
                    'own': bool(owner and ADMIN_EMAIL and owner['email'] == ADMIN_EMAIL), 'test': bool(c.get('test')),
                    'internal': bool(c.get('test') or (owner and ADMIN_EMAIL and owner['email'] == ADMIN_EMAIL))})
    return out

def ai_cost(tokens_in, tokens_out, cache_read=0, cache_write=0):
    """Custo real de IA em reais. Preços em dólar por milhão de tokens (padrão: Claude Haiku 5.5; troque no .env com AI_PRICE_IN,
    AI_PRICE_OUT, AI_PRICE_CACHE_READ, AI_PRICE_CACHE_WRITE e AI_USD_BRL)."""
    e = os.environ.get
    try: pin, pout, pcr, pcw, fx = (float(e('AI_PRICE_IN', '0.10')), float(e('AI_PRICE_OUT', '0.50')), float(e('AI_PRICE_CACHE_READ', '0.01')),
                                   float(e('AI_PRICE_CACHE_WRITE', '0.125')), float(e('AI_USD_BRL', '5.4')))
    except ValueError: return None
    return round((tokens_in * pin + tokens_out * pout + cache_read * pcr + cache_write * pcw) / 1_000_000 * fx, 2)

def match(r, plan='', segment='', cycle=''):
    c = r['c']
    if plan and r['plan'] != plan: return False
    if segment and c.get('segment') != segment: return False
    if cycle and ((c.get('billing') or {}).get('cycle') or 'monthly') != cycle: return False
    return True

def row_out(r):
    c = r['c']; b = c.get('billing') or {}
    return {'id': c['id'], 'name': c.get('name'), 'segment': c.get('segment'), 'users': r['users'], 'created_at': c.get('created_at'), 'internal': r['internal'], 'own': r['own'], 'test': r['test'],
            'owner': {k: (r['owner'] or {}).get(k) for k in ('name', 'email')}, 'kind': r['kind'], 'plan': r['plan'], 'raw_plan': c.get('plan'),
            'trial_ends_at': c.get('trial_ends_at') if r['kind'] == 'trial' else None, 'next_payment': b.get('next_payment') if r['kind'] == 'paying' else None,
            'paid_until': b.get('paid_until'), 'amount': b.get('amount'), 'cycle': b.get('cycle'), 'cancelled': r['cancelled'],
            'refused': {'due': r['refused']['due'], 'amount': r['refused']['amount'], 'detail': r['refused'].get('detail')} if r['refused'] else None,
            'courtesy_until': (c.get('courtesy') or {}).get('until'), 'last_use': r['last_use'], 'ai_used': r['ai_used'], 'ai_limit': r['lim']['ai'], 'ai_cost': r['ai_real'],
            'ai_margin': round((b.get('amount') or 0) - (r['ai_real'] or 0), 2) if r['kind'] == 'paying' and (b.get('cycle') or 'monthly') == 'monthly' else None,
            'wa': bool(r['wa'] and r['wa'].get('status') == 'connected'), 'wa_alert': (r['wa'] or {}).get('alert') if r['wa'] and r['wa'].get('status') == 'connected' else None, 'status': c.get('status')}

# ---------- visão geral ----------
@router.get('/admin/overview')
async def overview(start: Optional[str] = None, end: Optional[str] = None, plan: str = '', segment: str = '', cycle: str = '', user=Depends(admin)):
    t0, t1, p0, p1 = period(start, end)
    rows = [r for r in await base() if not r['internal'] and match(r, plan, segment, cycle)]
    ids = {r['c']['id'] for r in rows}
    pays = [p for p in await db.payments.find({'status': 'paid'}, {'_id': 0}).to_list(20000) if p['company_id'] in ids]
    received = sum(p['amount'] for p in pays if inside(p.get('paid_at'), t0, t1))
    received_prev = sum(p['amount'] for p in pays if inside(p.get('paid_at'), p0, p1))
    since = lambda r: (r['c'].get('billing') or {}).get('since')
    paying = [r for r in rows if r['kind'] == 'paying']; trial = [r for r in rows if r['kind'] == 'trial']
    soon = [r for r in trial if aware(r['c']['trial_ends_at']) - now() <= timedelta(days=3)]
    ended = [r for r in rows if inside(r['c'].get('trial_ends_at'), t0, t1) and aware(r['c']['trial_ends_at']) <= now()]
    converted = [r for r in ended if since(r)]
    live = [r for r in rows if r['kind'] in ('paying', 'courtesy', 'trial')]
    week = now() - timedelta(days=7)
    # receita recebida nos últimos 6 meses (mês de São Paulo)
    today = now().astimezone(SP)
    months = []
    y, m = today.year, today.month
    for _ in range(6):
        months.append((y, m)); m -= 1
        if m == 0: y, m = y - 1, 12
    months.reverse()
    tot = {k: 0 for k in months}
    for p in pays:
        d = aware(p.get('paid_at'))
        if d:
            k = (d.astimezone(SP).year, d.astimezone(SP).month)
            if k in tot: tot[k] += p['amount']
    tin, tout = sum(r['ai_in'] for r in rows), sum(r['ai_out'] for r in rows)
    tcr, tcw = sum(r['ai_cr'] for r in rows), sum(r['ai_cw'] for r in rows)
    refused = [r for r in rows if r['refused']]
    # cadastros e funil de ativação (de quem se cadastrou no período)
    signed = [r for r in rows if inside(r['c'].get('created_at'), t0, t1)]
    sids = [r['c']['id'] for r in signed]
    taught = {s['company_id'] for s in await db.ai_settings.find({'company_id': {'$in': sids}}, {'_id': 0, 'company_id': 1, 'about': 1, 'offer': 1, 'knowledge': 1}).to_list(5000)
              if ((s.get('about') or '') + (s.get('offer') or '') + (s.get('knowledge') or '')).strip()}
    answered = {u['company_id'] for u in await db.ai_usage.find({'company_id': {'$in': sids}}, {'_id': 0, 'company_id': 1, 'atend': 1, 'replies': 1, 'suggestions': 1}).to_list(20000)
                if (u.get('atend') or 0) + (u.get('replies') or 0) + (u.get('suggestions') or 0) > 0}
    steps = [('signup', 'Cadastro', signed),
             ('whatsapp', 'Conectou o WhatsApp', [r for r in signed if r['wa']]),
             ('taught', 'Ensinou a Nanda', [r for r in signed if r['c']['id'] in taught]),
             ('answered', 'Nanda respondeu', [r for r in signed if r['c']['id'] in answered]),
             ('card', 'Cadastrou o cartão', [r for r in signed if (r['c'].get('billing') or {}).get('sub_id')]),
             ('paying', 'Virou pagante', [r for r in signed if since(r) and r['kind'] == 'paying'])]
    funnel = [{'key': k, 'label': l, 'count': len(v), 'pct': round(100 * len(v) / len(signed)) if signed else None} for k, l, v in steps]
    # cancelamentos: quem cancelou a assinatura no período e quanto de receita mensal saiu
    def cancel_at(r): return (r['c'].get('billing') or {}).get('cancelled_at')
    def monthly(r):
        b = r['c'].get('billing') or {}; a = b.get('amount') or 0
        return a / 12 if b.get('cycle') == 'annual' else a
    gone = [r for r in rows if inside(cancel_at(r), t0, t1)]
    gone_prev = [r for r in rows if inside(cancel_at(r), p0, p1)]
    base_n = len(paying) + sum(1 for r in gone if r['kind'] != 'paying')
    churn = {'count': len(gone), 'count_prev': len(gone_prev), 'mrr_lost': round(sum(monthly(r) for r in gone), 2),
             'pct': round(100 * len(gone) / base_n, 1) if base_n else None,
             'in_trial': sum(1 for r in gone if billing.card_trial(r['c']) or not since(r))}
    # uso da plataforma no período (só clientes, sem contas internas e de teste)
    idl = list(ids)
    cnt = lambda coll, q: coll.count_documents({'company_id': {'$in': idl}, **q})
    rng = {'$gte': t0, '$lt': t1}
    sales_ok = await db.sales.find({'company_id': {'$in': idl}, 'status': 'ok', 'created_at': rng}, {'_id': 0, 'total': 1}).to_list(50000)
    ai_cost_month = ai_cost(sum(r['ai_in'] for r in rows), sum(r['ai_out'] for r in rows), sum(r['ai_cr'] for r in rows), sum(r['ai_cw'] for r in rows))
    activity = {'conversations': await cnt(db.contacts, {'last_at': rng}), 'msgs_in': await cnt(db.messages, {'dir': 'in', 'at': rng}),
                'msgs_out': await cnt(db.messages, {'dir': 'out', 'at': rng}), 'handoffs': await cnt(db.contacts, {'needs_human_at': rng}),
                'appointments': await cnt(db.appointments, {'created_at': rng}), 'sales': len(sales_ok), 'sales_total': round(sum(x.get('total') or 0 for x in sales_ok), 2),
                'wa_connected': sum(1 for r in live if r['wa'] and r['wa'].get('status') == 'connected'),
                'ig_connected': await db.ig_accounts.count_documents({'company_id': {'$in': [r['c']['id'] for r in live]}})}
    top = sorted([r for r in live if r['ai_used']], key=lambda r: -r['ai_used'])[:5]
    top = [{'id': r['c']['id'], 'name': r['c'].get('name'), 'kind': r['kind'], 'plan': r['plan'], 'ai_used': r['ai_used'], 'ai_limit': r['lim']['ai'], 'ai_cost': r['ai_real'],
            'mrr': r['mrr'], 'wa': bool(r['wa'] and r['wa'].get('status') == 'connected')} for r in top]
    import mailer, notices as N
    bk = await db.settings.find_one({'id': 'backup'}, {'_id': 0}) or {}
    ops = {'wa_notices': await N.wa_ready(), 'payments': bool(os.environ.get('MP_ACCESS_TOKEN')), 'email': mailer.ready(),
           'errors_24h': await db.errors.count_documents({'at': {'$gte': now() - timedelta(days=1)}}), 'backup_at': bk.get('last_at')}
    wa_bad = [r for r in live if r['wa'] and r['wa'].get('status') == 'connected' and r['wa'].get('alert')]
    card_soon = [r for r in rows if (lambda tu: tu and now() < tu <= now() + timedelta(days=3))(aware((r['c'].get('billing') or {}).get('trial_until'))) and (r['c'].get('billing') or {}).get('status') == 'active']
    return {'period': {'start': t0, 'end': t1 - timedelta(seconds=1), 'prev_start': p0, 'prev_end': p1 - timedelta(seconds=1)},
            'mrr': round(sum(r['mrr'] for r in rows), 2), 'paying': len(paying), 'courtesy': sum(1 for r in rows if r['kind'] == 'courtesy'),
            'trial': len(trial), 'trial_soon': len(soon), 'readonly': sum(1 for r in rows if r['kind'] == 'readonly'), 'suspended': sum(1 for r in rows if r['kind'] == 'suspended'),
            'received': round(received, 2), 'received_prev': round(received_prev, 2),
            'signups': len(signed), 'signups_prev': sum(1 for r in rows if inside(r['c'].get('created_at'), p0, p1)), 'funnel': funnel, 'churn': churn,
            'new_paying': sum(1 for r in rows if inside(since(r), t0, t1)), 'new_paying_prev': sum(1 for r in rows if inside(since(r), p0, p1)),
            'conversion': {'ended': len(ended), 'converted': len(converted), 'pct': round(100 * len(converted) / len(ended)) if ended else None},
            'revenue_months': [{'key': f'{y}-{m:02d}', 'label': MONTHS[m - 1], 'total': round(tot[(y, m)], 2)} for y, m in months],
            'attention': {'refused': {'count': len(refused), 'amount': round(sum(r['refused']['amount'] for r in refused), 2)},
                          'trial_soon': {'count': len(soon), 'connected': sum(1 for r in soon if r['wa'])},
                          'no_whatsapp': {'count': sum(1 for r in live if not r['wa'] and aware(r['c'].get('created_at')) and aware(r['c']['created_at']) < now() - timedelta(days=2))},
                          'ai_near': {'count': sum(1 for r in live if r['lim']['ai'] and r['ai_used'] >= 0.8 * r['lim']['ai'])},
                          'wa_alert': {'count': len(wa_bad)}, 'card_trial_soon': {'count': len(card_soon)}},
            'plans': [{'plan': k, 'name': PLANS[k]['name'], 'count': sum(1 for r in paying if r['plan'] == k), 'mrr': round(sum(r['mrr'] for r in paying if r['plan'] == k), 2)} for k in PLANS],
            'activity': activity, 'top': top, 'ops': ops, 'margin': round(sum(r['mrr'] for r in rows) - (ai_cost_month or 0), 2) if ai_cost_month is not None else None,
            'usage': {'accounts': len(live), 'active_week': sum(1 for r in live if r['last_use'] and r['last_use'] >= week),
                      'ai_replies': sum(r['ai_used'] for r in rows), 'ai_cost': ai_cost(tin, tout, tcr, tcw)}}

@router.get('/admin/stats')
async def stats(user=Depends(admin)):
    """Contadores simples (mantido para compatibilidade)."""
    rows = await base(); week = now() - timedelta(days=7)
    return {'companies': len(rows), 'active': sum(1 for r in rows if r['c'].get('status') == 'active'), 'trial': sum(1 for r in rows if r['kind'] == 'trial'),
            'paying': sum(1 for r in rows if r['kind'] == 'paying'), 'suspended': sum(1 for r in rows if r['kind'] == 'suspended'),
            'users': await db.users.count_documents({}), 'new_week': sum(1 for r in rows if aware(r['c'].get('created_at')) and aware(r['c']['created_at']) >= week),
            'errors_day': await db.errors.count_documents({'at': {'$gte': now() - timedelta(days=1)}})}

# ---------- dados do Oi Nanda (a empresa que vende a plataforma) ----------
PLATFORM_DEFAULT = {'legal_name': 'Fagotti Marketing Direto Ltda', 'cnpj': '40.864.395/0001-95', 'address': '', 'email': 'contato@altaagencydigital.com',
                    'phone': '', 'site': 'https://oinanda.com.br', 'instagram': '@oinanda.app', 'meta_portfolio': 'Oi Nanda'}

class PlatformIn(BaseModel):
    legal_name: Optional[str] = Field(default=None, max_length=120)
    cnpj: Optional[str] = Field(default=None, max_length=20)
    address: Optional[str] = Field(default=None, max_length=200)
    email: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=30)
    site: Optional[str] = Field(default=None, max_length=120)
    instagram: Optional[str] = Field(default=None, max_length=60)
    meta_portfolio: Optional[str] = Field(default=None, max_length=80)
    mrr_goal: Optional[float] = Field(default=None, ge=0, le=10_000_000)

@router.get('/admin/platform')
async def platform_get(user=Depends(admin)):
    d = await db.settings.find_one({'id': 'platform'}, {'_id': 0, 'id': 0}) or {}
    return {**PLATFORM_DEFAULT, **d}

@router.put('/admin/platform')
async def platform_put(body: PlatformIn, request: Request, user=Depends(admin)):
    ch = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump(exclude_none=True).items()}
    if ch:
        await db.settings.update_one({'id': 'platform'}, {'$set': ch}, upsert=True)
        await audit(request, user, 'Admin alterou os dados do Oi Nanda', ', '.join(ch))
    return await platform_get(user)

# ---------- financeiro ----------
STATUS = {'paid': 'paga', 'pending': 'pendente', 'refused': 'recusada', 'cancelled': 'cancelada', 'scheduled': 'a vencer'}

@router.get('/admin/payments')
async def payments(start: Optional[str] = None, end: Optional[str] = None, date_by: Literal['due', 'paid'] = 'due', status: str = '', plan: str = '', cycle: str = '',
                   segment: str = '', q: str = '', vmin: Optional[float] = None, vmax: Optional[float] = None, sort: Literal['recent', 'value'] = 'recent', fmt: str = '',
                   user=Depends(admin)):
    t0, t1, _, _ = period(start, end)
    if not start and not end:  # financeiro: o mês inteiro, para mostrar o que ainda vai vencer
        e = t1.astimezone(SP); nxt = (e.replace(day=1) + timedelta(days=32)).replace(day=1)
        t1 = datetime(nxt.year, nxt.month, 1, tzinfo=SP).astimezone(timezone.utc)
    rows_c = {r['c']['id']: r for r in await base() if not r['internal']}
    field = 'paid_at' if date_by == 'paid' else 'due'
    rows = []
    for p in await db.payments.find({}, {'_id': 0}).sort('due', -1).to_list(20000):
        r = rows_c.get(p['company_id'])
        if not r or not inside(p.get(field), t0, t1): continue
        rows.append({**{k: p.get(k) for k in ('id', 'company_id', 'plan', 'cycle', 'amount', 'status', 'due', 'paid_at', 'attempt', 'detail', 'nf', 'nf_number', 'nf_at')},
                     'company': r['c'].get('name'), 'segment': r['c'].get('segment')})
    if date_by == 'due':  # próximas cobranças das assinaturas ativas, ainda sem fatura no Mercado Pago
        for r in rows_c.values():
            b = r['c'].get('billing') or {}
            if r['kind'] == 'paying' and b.get('status') == 'active' and inside(b.get('next_payment'), max(t0, now()), t1):
                rows.append({'id': 'next:' + r['c']['id'], 'company_id': r['c']['id'], 'plan': b.get('plan'), 'cycle': b.get('cycle'), 'amount': float(b.get('amount') or 0),
                             'status': 'scheduled', 'due': aware(b['next_payment']), 'paid_at': None, 'attempt': 0, 'detail': None, 'company': r['c'].get('name'), 'segment': r['c'].get('segment')})
    base_rows = rows
    totals = {'received': round(sum(x['amount'] for x in base_rows if x['status'] == 'paid'), 2), 'received_n': sum(1 for x in base_rows if x['status'] == 'paid'),
              'to_receive': round(sum(x['amount'] for x in base_rows if x['status'] in ('pending', 'scheduled')), 2), 'to_receive_n': sum(1 for x in base_rows if x['status'] in ('pending', 'scheduled')),
              'refused': round(sum(x['amount'] for x in base_rows if x['status'] == 'refused'), 2), 'refused_n': sum(1 for x in base_rows if x['status'] == 'refused'),
              'cancellations': sum(1 for r in rows_c.values() if inside((r['c'].get('billing') or {}).get('cancelled_at'), t0, t1))}
    counts = {**{k: sum(1 for x in base_rows if x['status'] == k) for k in STATUS}, 'nf': sum(1 for x in base_rows if x['status'] == 'paid' and not x.get('nf'))}
    ql = q.strip().lower()
    want = {'open': ('pending', 'scheduled'), 'nf': ('paid',)}.get(status, (status,) if status else None)
    rows = [x for x in rows if (not want or x['status'] in want) and (status != 'nf' or not x.get('nf')) and (not plan or x['plan'] == plan) and (not cycle or (x['cycle'] or 'monthly') == cycle)
            and (not segment or x['segment'] == segment) and (not ql or ql in (x['company'] or '').lower())
            and (vmin is None or x['amount'] >= vmin) and (vmax is None or x['amount'] <= vmax)]
    rows.sort(key=(lambda x: -x['amount']) if sort == 'value' else (lambda x: aware(x['due'])), reverse=sort != 'value')
    if fmt == 'csv':
        buf = io.StringIO(); w = csv.writer(buf, delimiter=';')
        w.writerow(['Vencimento', 'Pago em', 'Cliente', 'Segmento', 'Plano', 'Ciclo', 'Valor', 'Situação', 'Nota fiscal'])
        day = lambda d: aware(d).astimezone(SP).strftime('%d/%m/%Y') if d else ''
        for x in rows:
            w.writerow([day(x['due']), day(x['paid_at']), x['company'], x['segment'], (PLANS.get(x['plan']) or {}).get('name', x['plan']),
                        'anual' if x['cycle'] == 'annual' else 'mensal', f"{x['amount']:.2f}".replace('.', ','), STATUS.get(x['status'], x['status']),
                        (x.get('nf_number') or 'emitida') if x.get('nf') else ('pendente' if x['status'] == 'paid' else '')])
        return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="oinanda-cobrancas.csv"'})
    summary = await fin_summary(rows_c, t0, t1)
    upcoming = sorted([{'company_id': r['c']['id'], 'company': r['c'].get('name'), 'due': aware(r['c']['billing']['next_payment']), 'plan': r['c']['billing'].get('plan'),
                        'extra_users': r['c']['billing'].get('extra_users') or 0, 'amount': float(r['c']['billing'].get('amount') or 0)}
                       for r in rows_c.values() if r['kind'] == 'paying' and (r['c'].get('billing') or {}).get('status') == 'active'
                       and inside(r['c']['billing'].get('next_payment'), now(), now() + timedelta(days=7))], key=lambda x: x['due'])
    return {'period': {'start': t0, 'end': t1 - timedelta(seconds=1)}, 'rows': rows[:500], 'total': len(rows), 'totals': totals, 'counts': counts, 'upcoming': upcoming, 'summary': summary,
            'ready': bool(os.environ.get('MP_ACCESS_TOKEN'))}

def per_month(c):
    b = c.get('billing') or {}; a = float(b.get('amount') or 0)
    return a / 12 if b.get('cycle') == 'annual' else a

async def fin_summary(rows_c, t0, t1):
    """Visão de dono do caixa: receita recorrente, o que entra nos próximos 30 dias, inadimplência e o que mudou na receita."""
    n = now(); rows = list(rows_c.values())
    paying = [r for r in rows if r['kind'] == 'paying']
    mrr = round(sum(r['mrr'] for r in rows), 2); active = [r for r in paying if (r['c'].get('billing') or {}).get('status') == 'active']
    pays = [p for p in await db.payments.find({'status': 'paid'}, {'_id': 0}).to_list(20000) if p['company_id'] in rows_c]
    p0 = t0 - (t1 - t0)
    received_prev = round(sum(p['amount'] for p in pays if inside(p.get('paid_at'), p0, t0)), 2)
    # próximos 30 dias: renovações das assinaturas ativas e primeiras mensalidades de quem está no teste com cartão
    fut = []
    for r in rows:
        b = r['c'].get('billing') or {}
        if b.get('status') != 'active': continue
        tu = aware(b.get('trial_until')); nx = aware(b.get('next_payment'))
        if tu and n < tu <= n + timedelta(days=30): fut.append({'company_id': r['c']['id'], 'company': r['c'].get('name'), 'due': tu, 'amount': float(b.get('amount') or 0), 'plan': b.get('plan'), 'cycle': b.get('cycle') or 'monthly', 'first': True})
        elif r['kind'] == 'paying' and nx and n <= nx <= n + timedelta(days=30): fut.append({'company_id': r['c']['id'], 'company': r['c'].get('name'), 'due': nx, 'amount': float(b.get('amount') or 0), 'plan': b.get('plan'), 'cycle': b.get('cycle') or 'monthly', 'first': False})
    fut.sort(key=lambda x: x['due'])
    weeks = [{'label': f'{(n + timedelta(days=7 * i)).astimezone(SP):%d/%m} a {(n + timedelta(days=7 * i + 6)).astimezone(SP):%d/%m}', 'total': 0.0, 'n': 0} for i in range(5)]
    for x in fut:
        w = weeks[min(4, max(0, (x['due'] - n).days // 7))]; w['total'] = round(w['total'] + x['amount'], 2); w['n'] += 1
    # inadimplência: cobrança recusada ainda sem pagamento depois dela
    late = []
    for r in rows:
        if not r['refused']: continue
        p = r['refused']; lim = aware(p['due']) + timedelta(days=GRACE_DAYS) if p.get('due') else None
        late.append({'company_id': r['c']['id'], 'company': r['c'].get('name'), 'amount': p['amount'], 'due': p.get('due'), 'attempt': p.get('attempt') or 0,
                     'reason': reason(p.get('detail')) or 'recusada pelo banco', 'access_until': lim, 'blocked': bool(lim and lim < n)})
    late.sort(key=lambda x: aware(x['due']) or n)
    # o que mudou na receita recorrente no período
    since = lambda r: aware((r['c'].get('billing') or {}).get('since'))
    new = [r for r in paying if inside(since(r), t0, t1) and not billing.card_trial(r['c'])]
    lost = [r for r in rows if inside((r['c'].get('billing') or {}).get('cancelled_at'), t0, t1)]
    new_mrr = round(sum(per_month(r['c']) for r in new), 2); lost_mrr = round(sum(per_month(r['c']) for r in lost), 2)
    cyc = lambda k: [r for r in paying if ((r['c'].get('billing') or {}).get('cycle') or 'monthly') == k]
    # recebido nos últimos 12 meses
    today = n.astimezone(SP); ms = []; y, m = today.year, today.month
    for _ in range(12):
        ms.append((y, m)); m -= 1
        if m == 0: y, m = y - 1, 12
    ms.reverse(); tot = {k: 0.0 for k in ms}
    for p in pays:
        d = aware(p.get('paid_at'))
        if d:
            k = (d.astimezone(SP).year, d.astimezone(SP).month)
            if k in tot: tot[k] += p['amount']
    # notas fiscais: toda cobrança paga precisa de nota; mostra as que faltam
    nf_missing = [p for p in pays if not p.get('nf')]
    # assinaturas: quem paga (ou vai pagar) e quanto já pagou desde o começo
    paid_by = {}
    for p in pays: paid_by[p['company_id']] = paid_by.get(p['company_id'], 0) + p['amount']
    subs = []
    for r in rows:
        b = r['c'].get('billing') or {}
        if not b.get('sub_id') or r['kind'] not in ('paying', 'trial'): continue
        st = 'cancelled' if b.get('status') == 'cancelled' else 'trial' if billing.card_trial(r['c']) else 'active' if r['kind'] == 'paying' else None
        if not st: continue
        subs.append({'company_id': r['c']['id'], 'company': r['c'].get('name'), 'plan': b.get('plan'), 'cycle': b.get('cycle') or 'monthly', 'amount': float(b.get('amount') or 0),
                     'monthly': round(per_month(r['c']), 2), 'since': b.get('since'), 'next_payment': b.get('trial_until') if st == 'trial' else b.get('next_payment'),
                     'paid_until': b.get('paid_until'), 'paid_total': round(paid_by.get(r['c']['id'], 0), 2), 'status': st, 'late': bool(r['refused'])})
    subs.sort(key=lambda x: (-x['monthly'], x['company'] or ''))
    goal = ((await db.settings.find_one({'id': 'platform'}, {'_id': 0, 'mrr_goal': 1})) or {}).get('mrr_goal')
    tin = sum(r['ai_in'] for r in rows); tout = sum(r['ai_out'] for r in rows)
    aic = ai_cost(tin, tout, sum(r['ai_cr'] for r in rows), sum(r['ai_cw'] for r in rows))
    return {'mrr': mrr, 'arr': round(mrr * 12, 2), 'paying': len(paying), 'ticket': round(mrr / len(paying), 2) if paying else 0, 'received_prev': received_prev,
            'forecast': {'total': round(sum(x['amount'] for x in fut), 2), 'n': len(fut), 'first': sum(1 for x in fut if x['first']), 'items': fut[:30], 'weeks': weeks},
            'late': {'total': round(sum(x['amount'] for x in late), 2), 'n': len(late), 'items': late},
            'movement': {'new': new_mrr, 'new_n': len(new), 'lost': lost_mrr, 'lost_n': len(lost), 'net': round(new_mrr - lost_mrr, 2)},
            'cycles': {k: {'n': len(cyc(k)), 'mrr': round(sum(r['mrr'] for r in cyc(k)), 2)} for k in ('monthly', 'annual')},
            'plans': [{'plan': k, 'name': PLANS[k]['name'], 'n': sum(1 for r in paying if r['plan'] == k), 'mrr': round(sum(r['mrr'] for r in paying if r['plan'] == k), 2)} for k in PLANS],
            'months': [{'key': f'{y}-{m:02d}', 'label': MONTHS[m - 1], 'total': round(tot[(y, m)], 2)} for y, m in ms],
            'ai_cost': aic, 'margin': round(mrr - aic, 2) if aic is not None else None, 'margin_pct': round(100 * (mrr - aic) / mrr) if aic is not None and mrr else None,
            'mp_fee_pct': float(os.environ['MP_FEE_PCT']) if os.environ.get('MP_FEE_PCT') else None,
            'nf': {'missing': len(nf_missing), 'missing_total': round(sum(p['amount'] for p in nf_missing), 2)}, 'subs': subs, 'goal': goal,
            'recovered': round(sum(p['amount'] for p in pays if inside(p.get('paid_at'), t0, t1) and (p.get('attempt') or 0) > 0), 2)}

class NfIn(BaseModel):
    done: bool
    number: Optional[str] = Field(default=None, max_length=40)

@router.put('/admin/payments/{pid}/nf')
async def payment_nf(pid: str, body: NfIn, request: Request, user=Depends(admin)):
    """Marca que a nota fiscal desta cobrança foi emitida (ou desfaz)."""
    p = await db.payments.find_one({'id': pid}, {'_id': 0})
    if not p: raise HTTPException(404, 'Cobrança não encontrada.')
    if p['status'] != 'paid': raise HTTPException(400, 'Só cobrança paga leva nota fiscal.')
    await db.payments.update_one({'id': pid}, {'$set': {'nf': body.done, 'nf_number': (body.number or '').strip() or None, 'nf_at': now() if body.done else None}})
    await audit(request, {**user, 'company_id': p['company_id']}, 'Admin marcou nota fiscal' if body.done else 'Admin desmarcou nota fiscal', body.number or pid, p['company_id'])
    return {'ok': True}

@router.post('/admin/payments/sync')
async def payments_sync(request: Request, user=Depends(admin)):
    """Busca agora no Mercado Pago as cobranças de todas as assinaturas."""
    if not os.environ.get('MP_ACCESS_TOKEN'): raise HTTPException(503, 'O Mercado Pago ainda não foi configurado neste servidor.')
    await billing.full_sync()
    await audit(request, user, 'Admin atualizou as cobranças pelo Mercado Pago')
    return {'ok': True}

# ---------- avisos automáticos ----------
# ---------- avisos automáticos (textos, canais, enviados) ----------
def notice_out(key, on, t, custom, tpl):
    import notices as N
    n = N.DEFS[key]
    return {'key': key, 'name': n['name'], 'when': n['when'], 'email': True, 'whatsapp': 'w' in n['ch'], 'on': on, 'custom': custom,
            'wa_status': (tpl or {}).get('status') if 'w' in n['ch'] else None, **t}

@router.get('/admin/notices')
async def notices_get(user=Depends(admin)):
    import notices as N
    on = await N.toggles(); custom = (await db.settings.find_one({'id': 'notice_texts'}, {'_id': 0})) or {}
    items = []
    for k in N.DEFS:
        tpl = await db.notice_templates.find_one({'key': k}, {'_id': 0}, sort=[('version', -1)])
        items.append(notice_out(k, on[k], await N.texts(k), bool(custom.get(k)), tpl))
    return {'notices': on, 'items': items, 'vars': sorted(N.VARS), 'email_ready': bool(os.environ.get('RESEND_API_KEY')), 'wa_ready': await N.wa_ready()}

@router.put('/admin/notices')
async def notices_put(body: dict, request: Request, user=Depends(admin)):
    import notices as N
    ch = {k: bool(v) for k, v in body.items() if k in N.DEFS}
    if ch:
        await db.settings.update_one({'id': 'notices'}, {'$set': ch}, upsert=True)
        await audit(request, user, 'Admin ajustou os avisos automáticos', ', '.join(f"{N.DEFS[k]['name']} {'ligado' if v else 'desligado'}" for k, v in ch.items()))
    return {'notices': await N.toggles()}

@router.get('/admin/notices/log')
async def notices_log(limit: int = 100, company_id: str = '', user=Depends(admin)):
    q = {'company_id': company_id} if company_id else {}
    return await db.notice_log.find(q, {'_id': 0}).sort('at', -1).limit(min(limit, 500)).to_list(500)

class NoticeText(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=120)
    pre: Optional[str] = Field(default=None, max_length=160)
    title: Optional[str] = Field(default=None, max_length=80)
    body: Optional[str] = Field(default=None, max_length=1200)
    wa: Optional[str] = Field(default=None, max_length=900)

async def notice_view(key):
    import notices as N
    if key not in N.DEFS: raise HTTPException(404, 'Aviso não encontrado.')
    t = await N.texts(key); n = N.DEFS[key]
    tpls = await db.notice_templates.find({'key': key}, {'_id': 0}).sort('version', -1).to_list(5)
    live = next((x for x in tpls if x['status'] == 'APPROVED'), None)
    return {**notice_out(key, (await N.toggles())[key], t, bool(((await db.settings.find_one({'id': 'notice_texts'}, {'_id': 0})) or {}).get(key)), tpls[0] if tpls else None),
            'default': {f: n[f] for f in N.EDIT}, 'vars': sorted(N.VARS), 'button': n['button'][0], 'reply': n.get('reply'),
            'preview': {'subject': N.fill(t['subject'], N.VARS), 'pre': N.fill(t['pre'], N.VARS), 'html': N.email_html(key, t, N.VARS),
                        'wa_title': N.fill(t['title'], N.VARS), 'wa': N.wa_text(t, N.VARS)},
            'wa_live': (live or {}).get('text'), 'wa_error': (tpls[0].get('error') if tpls else None)}

@router.get('/admin/notices/{key}')
async def notice_get(key: str, user=Depends(admin)): return await notice_view(key)

@router.put('/admin/notices/{key}')
async def notice_put(key: str, body: NoticeText, request: Request, user=Depends(admin)):
    import notices as N, re
    if key not in N.DEFS: raise HTTPException(404, 'Aviso não encontrado.')
    ch = {k: v.strip() for k, v in body.model_dump(exclude_none=True).items() if v.strip()}
    for f, v in ch.items():
        bad = [x for x in re.findall(r'\{(\w+)\}', v) if x not in N.VARS]
        if bad: raise HTTPException(400, 'Campo desconhecido: {' + bad[0] + '}. Use só os campos da lista.')
    if ch:
        await db.settings.update_one({'id': 'notice_texts'}, {'$set': {f'{key}.{f}': v for f, v in ch.items()}}, upsert=True)
        if 'wa' in ch and 'w' in N.DEFS[key]['ch']: await N.wa_submit(key, ch['wa'])
        await audit(request, user, 'Admin mudou o texto de um aviso', N.DEFS[key]['name'])
    return await notice_view(key)

@router.delete('/admin/notices/{key}')
async def notice_reset(key: str, request: Request, user=Depends(admin)):
    import notices as N
    if key not in N.DEFS: raise HTTPException(404, 'Aviso não encontrado.')
    await db.settings.update_one({'id': 'notice_texts'}, {'$unset': {key: ''}})
    if 'w' in N.DEFS[key]['ch']: await N.wa_submit(key, N.DEFS[key]['wa'])
    await audit(request, user, 'Admin voltou um aviso ao texto padrão', N.DEFS[key]['name'])
    return await notice_view(key)

@router.post('/admin/notices/{key}/test')
async def notice_test(key: str, user=Depends(admin)):
    """Manda o aviso, com dados de exemplo, para o e-mail do próprio admin."""
    import notices as N, mailer
    if key not in N.DEFS: raise HTTPException(404, 'Aviso não encontrado.')
    t = await N.texts(key)
    return {'sent': await mailer.send(user['email'], '[teste] ' + N.fill(t['subject'], N.VARS), N.email_html(key, t, N.VARS), N.email_text(key, t, N.VARS)), 'to': user['email']}

@router.post('/admin/notices/whatsapp/submit')
async def notices_wa_submit(request: Request, user=Depends(admin)):
    """Manda para aprovação da Meta os textos de WhatsApp que ainda não foram enviados."""
    import notices as N
    if not await N.wa_ready(): raise HTTPException(400, 'O número do Oi Nanda ainda não está conectado para enviar avisos.')
    out = []
    for k, n in N.DEFS.items():
        if 'w' in n['ch']: out.append((await N.wa_submit(k, (await N.texts(k))['wa']))['status'])
    await audit(request, user, 'Admin enviou os avisos de WhatsApp para aprovação', f'{len(out)} modelos')
    return {'submitted': len(out), 'status': out}

# ---------- precisa da sua atenção: a lista, com quem e por quê ----------
@router.get('/admin/attention')
async def attention(kind: str = '', user=Depends(admin)):
    import notices as N
    rows = await base(); out = []; n = now()
    last = {}
    for l in await db.notice_log.find({'at': {'$gte': n - timedelta(days=30)}}, {'_id': 0}).sort('at', 1).to_list(5000): last[(l['company_id'], l['key'])] = l
    def item(r, k, title, detail, when, urgency, notice, action):
        l = last.get((r['c']['id'], notice))
        return {'company_id': r['c']['id'], 'company': r['c'].get('name'), 'owner': (r['owner'] or {}).get('name'), 'email': (r['owner'] or {}).get('email'), 'phone': r['c'].get('phone'),
                'kind': k, 'title': title, 'detail': detail, 'when': when, 'urgency': urgency, 'notice': notice, 'action': action,
                'noticed_at': l['at'] if l else None, 'noticed_by': ('WhatsApp e e-mail' if l['wa'] and l['email'] else 'WhatsApp' if l['wa'] else 'e-mail') if l else None}
    for r in rows:
        if r['internal'] or r['c'].get('status') == 'suspended': continue
        c = r['c']
        if r.get('refused'):
            p = r['refused']; limit = aware(p['due']) + timedelta(days=GRACE_DAYS) if p.get('due') else None
            out.append(item(r, 'refused', 'Cobrança recusada', f"{mailer_money(p['amount'])} não passou" + (f" · {reason(p.get('detail'))}" if reason(p.get('detail')) else ''), limit, 0, 'refused', 'Enviar aviso'))
        if r['kind'] == 'trial' and aware(c.get('trial_ends_at')) and aware(c['trial_ends_at']) - n <= timedelta(days=3):
            out.append(item(r, 'trial_soon', 'Teste acabando', 'WhatsApp conectado, vale uma conversa' if r['wa'] else 'Ainda sem WhatsApp conectado', aware(c['trial_ends_at']), 1, 'trial_3d', 'Enviar aviso'))
        elif r['kind'] in ('trial', 'paying', 'courtesy') and not r['wa'] and aware(c.get('created_at')) and aware(c['created_at']) < n - timedelta(days=2):
            out.append(item(r, 'no_whatsapp', 'Sem WhatsApp conectado', 'Conta criada, mas as conversas não chegam', aware(c['created_at']), 2, 'wa_missing', 'Enviar aviso'))
        w = r['wa'] or {}
        if w.get('status') == 'connected' and w.get('alert'):
            a = w['alert']
            out.append(item(r, 'wa_alert', 'WhatsApp com problema na Meta', a.get('title') or 'A Meta restringiu o número', aware(a.get('at')), 0, None, 'Ver cliente'))
        b = c.get('billing') or {}; tu = aware(b.get('trial_until'))
        if tu and n < tu <= n + timedelta(days=3) and b.get('status') == 'active':
            out.append(item(r, 'card_trial_soon', 'Primeira mensalidade chegando', ('WhatsApp conectado' if r['wa'] else 'Ainda sem WhatsApp conectado') + ' · cobrança automática no cartão', tu, 1, 'trial_card_3d', 'Enviar aviso'))
        ca = aware(b.get('cancelled_at'))
        if ca and ca >= n - timedelta(days=14):
            out.append(item(r, 'cancelled', 'Cancelou a assinatura', 'Vale perguntar o motivo', ca, 2, None, 'Ver cliente'))
        if r['kind'] != 'readonly' and r['lim']['ai'] and r['ai_used'] >= 0.8 * r['lim']['ai']:
            full = r['ai_used'] >= r['lim']['ai']
            out.append(item(r, 'ai_near', 'Nanda no limite' if full else 'Nanda perto do limite', f"{r['ai_used']} de {r['lim']['ai']} respostas no mês", None, 1 if full else 3, 'ai_100' if full else 'ai_80', 'Enviar aviso'))
    far = n + timedelta(days=3650)
    out.sort(key=lambda x: (x['urgency'], x['when'] or far))
    counts = {k: sum(1 for x in out if x['kind'] == k) for k in ('refused', 'wa_alert', 'trial_soon', 'card_trial_soon', 'no_whatsapp', 'ai_near', 'cancelled')}
    return {'items': [x for x in out if not kind or x['kind'] == kind], 'counts': counts, 'total': len(out), 'wa_ready': await N.wa_ready()}

REASONS = {'cc_rejected_insufficient_amount': 'sem limite', 'cc_rejected_bad_filled_date': 'validade incorreta', 'cc_rejected_bad_filled_security_code': 'código de segurança incorreto',
           'cc_rejected_bad_filled_card_number': 'número do cartão incorreto', 'cc_rejected_card_disabled': 'cartão bloqueado', 'cc_rejected_call_for_authorize': 'o banco pede autorização',
           'cc_rejected_high_risk': 'recusada pelo banco', 'cc_rejected_other_reason': 'recusada pelo banco', 'cc_rejected_blacklist': 'recusada pelo banco',
           'cc_rejected_duplicated_payment': 'pagamento duplicado', 'cc_rejected_max_attempts': 'muitas tentativas', 'cc_rejected_card_error': 'erro no cartão', 'expired': 'cartão vencido'}
def reason(detail): return REASONS.get(detail or '', '')
def mailer_money(v):
    import mailer
    return mailer.money(v)

class NotifyIn(BaseModel):
    key: str
    channel: Literal['both', 'email', 'wa'] = 'both'

@router.post('/admin/companies/{company_id}/notify')
async def notify(company_id: str, body: NotifyIn, request: Request, user=Depends(admin)):
    """Manda agora um dos avisos prontos para o responsável da conta."""
    import notices as N
    if body.key not in N.DEFS: raise HTTPException(404, 'Aviso não encontrado.')
    r = await one(company_id)
    if not r['owner']: raise HTTPException(400, 'Esta conta não tem um responsável ativo.')
    c = r['c']; v = {'data': N.day(c.get('trial_ends_at')) if r['kind'] == 'trial' else N.day((c.get('billing') or {}).get('next_payment'))}
    if r.get('refused'):
        p = r['refused']; v.update(valor=mailer_money(p['amount']), prazo=N.day(aware(p['due']) + timedelta(days=GRACE_DAYS)) if p.get('due') else '')
    if r['lim']['ai']:
        f = lambda x: f'{x:,}'.replace(',', '.'); v.update(usadas=f(r['ai_used']), limite=f(r['lim']['ai']), bar_now=r['ai_used'], bar_total=r['lim']['ai'])
    await N.deliver(c, body.key, v, manual=True, only=None if body.channel == 'both' else body.channel)
    l = await db.notice_log.find_one({'company_id': company_id, 'key': body.key}, {'_id': 0}, sort=[('at', -1)])
    await audit(request, {**user, 'company_id': company_id}, 'Admin enviou um aviso', N.DEFS[body.key]['name'], company_id)
    await db.admin_notes.insert_one({'id': uid(), 'company_id': company_id, 'text': f"Aviso enviado: {N.DEFS[body.key]['name']}" + ('' if l and (l['email'] or l['wa']) else ' (não saiu: envio não configurado)'), 'by': user['name'], 'at': now()})
    return {'email': bool(l and l['email']), 'wa': bool(l and l['wa'])}

# ---------- clientes ----------
@router.get('/admin/companies')
async def companies(q: str = '', tab: str = '', plan: str = '', segment: str = '', wa: str = '', ai: str = '', by: str = '', start: Optional[str] = None,
                    end: Optional[str] = None, sort: Literal['recent', 'use', 'trial_end', 'name'] = 'recent', limit: int = 50, offset: int = 0, user=Depends(admin)):
    rows = await base()
    ql = q.strip().lower()
    def keep(r):
        c = r['c']; o = r['owner'] or {}
        if ql and ql not in ' '.join([c.get('name') or '', o.get('name') or '', o.get('email') or '']).lower(): return False
        if not match(r, plan, segment): return False
        if wa and (wa == 'yes') != bool(r['wa'] and r['wa'].get('status') == 'connected'): return False
        if ai == 'high' and not (r['lim']['ai'] and r['ai_used'] >= 0.8 * r['lim']['ai']): return False
        if ai == 'none' and r['ai_used']: return False
        if by and (start or end):
            t0, t1, _, _ = period(start, end)
            v = {'created': c.get('created_at'), 'last_use': r['last_use'], 'next_payment': (c.get('billing') or {}).get('next_payment'), 'trial_end': c.get('trial_ends_at')}.get(by)
            if not inside(v, t0, t1): return False
        return True
    rows = [r for r in rows if keep(r)]
    real = [r for r in rows if not r['internal']]
    counts = {'all': len(real), **{k: sum(1 for r in real if r['kind'] == k) for k in ('paying', 'courtesy', 'trial', 'readonly', 'suspended')}}
    if tab: rows = [r for r in rows if r['kind'] == tab]
    far = datetime.max.replace(tzinfo=timezone.utc); old = datetime.min.replace(tzinfo=timezone.utc)
    if sort == 'use': rows.sort(key=lambda r: -(r['ai_used'] / r['lim']['ai'] if r['lim']['ai'] else 0))
    elif sort == 'trial_end': rows.sort(key=lambda r: aware(r['c'].get('trial_ends_at')) if r['kind'] == 'trial' else far)
    elif sort == 'name': rows.sort(key=lambda r: (r['c'].get('name') or '').lower())
    else: rows.sort(key=lambda r: aware(r['c'].get('created_at')) or old, reverse=True)
    limit = max(1, min(limit, 200)); offset = max(0, offset)
    return {'rows': [row_out(r) for r in rows[offset:offset + limit]], 'total': len(rows), 'counts': counts}

@router.get('/admin/companies.csv')
async def companies_csv(user=Depends(admin)):
    buf = io.StringIO(); w = csv.writer(buf, delimiter=';')
    w.writerow(['Empresa', 'Segmento', 'Responsável', 'E-mail', 'Telefone', 'Plano', 'Situação', 'Usuários', 'Cadastro', 'Último uso', 'Respostas da Nanda no mês', 'WhatsApp'])
    day = lambda d: aware(d).astimezone(SP).strftime('%d/%m/%Y') if d else ''
    KIND = {'paying': 'pagante', 'courtesy': 'liberada pelo admin', 'trial': 'em teste', 'readonly': 'só leitura', 'suspended': 'suspensa'}
    for r in await base():
        if r['internal']: continue
        c = r['c']; o = r['owner'] or {}
        w.writerow([c.get('name'), c.get('segment'), o.get('name'), o.get('email'), c.get('phone'), (PLANS.get(r['plan']) or {}).get('name', 'Teste grátis' if r['kind'] == 'trial' else ''),
                    KIND.get(r['kind'], r['kind']), r['users'], day(c.get('created_at')), day(r['last_use']), r['ai_used'], 'conectado' if r['wa'] else 'não conectado'])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="oinanda-clientes.csv"'})

# ---------- ficha do cliente ----------
async def one(company_id):
    r = next((r for r in await base() if r['c']['id'] == company_id), None)
    if not r: raise HTTPException(404, 'Empresa não encontrada.')
    return r

@router.get('/admin/companies/{company_id}')
async def company_file(company_id: str, start: Optional[str] = None, end: Optional[str] = None, user=Depends(admin)):
    t0, t1, _, _ = period(start, end)
    r = await one(company_id); c = r['c']; b = c.get('billing') or {}
    ig = await db.ig_accounts.find_one({'company_id': company_id}, {'_id': 0, 'username': 1, 'name': 1})
    months = set(); d = t0
    while d < t1: months.add(d.astimezone(SP).strftime('%Y-%m')); d += timedelta(days=1)
    usage = await db.ai_usage.find({'company_id': company_id, 'month': {'$in': sorted(months)}}, {'_id': 0}).to_list(60)
    tin, tout = sum(u.get('input_tokens') or 0 for u in usage), sum(u.get('output_tokens') or 0 for u in usage)
    tcr, tcw = sum(u.get('cache_read_tokens') or 0 for u in usage), sum(u.get('cache_write_tokens') or 0 for u in usage)
    return {**row_out(r), 'phone': c.get('phone'), 'legal_name': c.get('legal_name'), 'cnpj': c.get('cnpj'), 'address': c.get('address'), 'contract_url': c.get('contract_url'),
            'timezone': c.get('timezone'), 'users_limit': r['lim']['users'], 'terms': c.get('terms'),
            'billing': {k: b.get(k) for k in ('plan', 'cycle', 'extra_users', 'status', 'amount', 'next_payment', 'paid_until', 'email', 'since', 'cancelled_at')},
            'courtesy': c.get('courtesy'), 'trial': {'start': c.get('created_at'), 'end': c.get('trial_ends_at')},
            'usage': {'conversations': await db.contacts.count_documents({'company_id': company_id, 'last_at': {'$gte': t0, '$lt': t1}}),
                      'ai_replies': sum((u.get('replies') or 0) + (u.get('suggestions') or 0) for u in usage), 'ai_cost': ai_cost(tin, tout, tcr, tcw),
                      'atend': sum(u.get('atend') or 0 for u in usage), 'suggestions': sum(u.get('suggestions') or 0 for u in usage),
                      'packs': await db.ai_packs.count_documents({'company_id': company_id, 'month': {'$in': sorted(months)}})},
            'channels': {'whatsapp': {'connected': bool(r['wa'] and r['wa'].get('status') == 'connected'), 'phone': (r['wa'] or {}).get('display_phone')},
                         'instagram': {'connected': bool(ig), 'username': (ig or {}).get('username')}},
            'payments': await db.payments.find({'company_id': company_id}, {'_id': 0}).sort('due', -1).to_list(36),
            'notes': await db.admin_notes.find({'company_id': company_id}, {'_id': 0}).sort('at', -1).to_list(100)}

class CompanyPatch(BaseModel):
    status: Optional[Literal['active', 'suspended']] = None
    plan: Optional[Literal['trial', 'oferta', 'completo', 'rede', 'essencial', 'profissional', 'equipe']] = None
    courtesy_days: Optional[int] = Field(default=None, ge=1, le=730)
    extend_trial_days: Optional[int] = Field(default=None, ge=1, le=60)
    note: Optional[str] = Field(default=None, max_length=500)
    phone: Optional[str] = Field(default=None, max_length=30)
    legal_name: Optional[str] = Field(default=None, max_length=120)
    cnpj: Optional[str] = Field(default=None, max_length=20)
    address: Optional[str] = Field(default=None, max_length=200)
    contract_url: Optional[str] = Field(default=None, max_length=500)
    test: Optional[bool] = None

@router.patch('/admin/companies/{company_id}')
async def company_update(company_id: str, body: CompanyPatch, request: Request, user=Depends(admin)):
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if not company: raise HTTPException(404, 'Empresa não encontrada.')
    data = body.model_dump(exclude_none=True)
    changes = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items() if k in ('status', 'note', 'phone', 'legal_name', 'cnpj', 'address', 'contract_url')}
    unset = {}
    if body.test is not None: changes['test'] = body.test
    if changes.get('contract_url') and not changes['contract_url'].lower().startswith(('https://', 'http://')): raise HTTPException(400, 'O link do contrato precisa começar com https://')
    if body.plan:
        changes['plan'] = body.plan
        if body.plan == 'trial': unset['courtesy'] = ''
        else:  # liberação pelo admin (cortesia): fica registrada, com prazo se você der um
            plan = billing.LEGACY.get(body.plan, body.plan)
            changes['courtesy'] = {'plan': plan, 'until': now() + timedelta(days=body.courtesy_days) if body.courtesy_days else None, 'by': user['name'], 'at': now()}
    if body.extend_trial_days:
        end = aware(company.get('trial_ends_at')) or now()
        changes['trial_ends_at'] = max(end, now()) + timedelta(days=body.extend_trial_days)
        if kind_of(company) in ('readonly', 'trial') and not body.plan: changes['plan'] = 'trial'; unset['courtesy'] = ''
    if changes or unset:
        op = {}
        if changes: op['$set'] = changes
        if unset: op['$unset'] = unset
        await db.companies.update_one({'id': company_id}, op)
        if changes.get('status') == 'suspended':
            ids = [u['id'] for u in await db.users.find({'company_id': company_id}, {'id': 1}).to_list(1000)]
            await db.sessions.delete_many({'user_id': {'$in': ids}})
        await audit(request, {**user, 'company_id': company_id}, 'Admin alterou a conta', ', '.join(list(changes) + [f'-{k}' for k in unset]), company_id)
    return await db.companies.find_one({'id': company_id}, {'_id': 0})

class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

@router.post('/admin/companies/{company_id}/notes')
async def note_add(company_id: str, body: NoteIn, request: Request, user=Depends(admin)):
    if not await db.companies.find_one({'id': company_id}, {'_id': 1}): raise HTTPException(404, 'Empresa não encontrada.')
    n = {'id': uid(), 'company_id': company_id, 'text': body.text.strip(), 'by': user['name'], 'at': now()}
    await db.admin_notes.insert_one(n.copy())
    return n

@router.delete('/admin/companies/{company_id}/notes/{note_id}')
async def note_del(company_id: str, note_id: str, user=Depends(admin)):
    await db.admin_notes.delete_one({'id': note_id, 'company_id': company_id})
    return {'ok': True}

class MessageIn(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    text: str = Field(min_length=3, max_length=3000)

@router.post('/admin/companies/{company_id}/message')
async def message(company_id: str, body: MessageIn, request: Request, user=Depends(admin)):
    """Aviso do admin para o dono da conta, por e-mail."""
    import mailer, html
    r = await one(company_id)
    if not r['owner']: raise HTTPException(400, 'Esta conta não tem um dono ativo.')
    paras = [html.escape(p) for p in body.text.strip().split('\n') if p.strip()]
    sent = await mailer.send(r['owner']['email'], body.subject.strip(), mailer.layout(body.subject.strip(), paras), mailer.plain(body.subject.strip(), paras))
    await audit(request, {**user, 'company_id': company_id}, 'Admin enviou um aviso por e-mail', body.subject.strip(), company_id)
    await db.admin_notes.insert_one({'id': uid(), 'company_id': company_id, 'text': f'Aviso enviado por e-mail: {body.subject.strip()}' + ('' if sent else ' (o e-mail não saiu: envio não configurado)'), 'by': user['name'], 'at': now()})
    return {'sent': sent}
