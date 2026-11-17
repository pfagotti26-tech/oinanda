"""Planos, limites e assinatura (cobrança recorrente pelo Mercado Pago, conta da Fagotti Marketing Direto).

Planos: Essencial, Profissional e Equipe. Cada um tem número de usuários, respostas da Nanda por mês e módulos.
Teste grátis de 14 dias com tudo liberado. Acabou o teste (ou a assinatura) sem pagamento: a conta fica só para
leitura (vê tudo, mas não envia, a Nanda e as automações param) até assinar.

Cobrança: assinatura do Mercado Pago (preapproval) no cartão, mensal ou anual. O dono escolhe o plano, vai para a
página do Mercado Pago, paga e volta. O status vem pelo aviso (webhook) do Mercado Pago e é sempre conferido na API.
O token fica só no servidor (deploy/set-mercadopago.sh).
"""
import os, hmac, hashlib, logging
from datetime import timedelta, datetime
from typing import Optional, Literal
import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field, EmailStr
from core import db, now, uid, audit, current_user, owners, admin
from whatsapp import aware

router = APIRouter()
log = logging.getLogger('oinanda.billing')
MP = 'https://api.mercadopago.com'
EXTRA_USER = 39
ANNUAL_MONTHS = 10  # anual: paga 10, leva 12
TRIAL_AI = 1000
GRACE_DAYS = 7  # cobrança recusada: quantos dias a conta segue funcionando depois do vencimento
FEATURES = {
    'automacoes': 'Automações e lembretes', 'promocoes': 'Promoções e cupons', 'auto': 'Nanda respondendo sozinha',
    'segmentos': 'Segmentos de clientes', 'campanhas': 'Campanhas', 'retorno': 'Retorno por anúncio', 'jornada_rel': 'Relatório de jornada',
    'equipes': 'Equipes, fila e rodízio', 'metas': 'Metas, prêmios e ranking', 'posvenda': 'Funil de pós-venda e renovação',
}
PLANS = {
    'essencial': {'name': 'Essencial', 'price': 147, 'users': 2, 'ai': 300, 'features': ['automacoes', 'promocoes'],
                  'pitch': 'Para quem está começando: conversas, agenda, funil, vendas e a Nanda sugerindo respostas.'},
    'profissional': {'name': 'Profissional', 'price': 297, 'users': 5, 'ai': 2000, 'features': ['automacoes', 'promocoes', 'auto', 'segmentos', 'campanhas', 'retorno', 'jornada_rel'],
                     'pitch': 'A Nanda atendendo sozinha, campanhas, segmentos e retorno por anúncio.'},
    'equipe': {'name': 'Equipe', 'price': 597, 'users': 12, 'ai': 6000, 'features': list(FEATURES),
               'pitch': 'Para operação com time: equipes, rodízio, metas, ranking e pós-venda.'},
}
LEGACY = {'oferta': 'profissional', 'completo': 'equipe', 'rede': 'equipe'}  # planos antigos dados pelo admin

def plan_of(company):
    """Situação de acesso: (estado, plano). Estado: trial | active | readonly.

    Assinatura do Mercado Pago vale até a data paga (paid_until). Cancelou ou parou de pagar: quando a data passa, a conta
    volta a ser só leitura. Plano liberado pelo admin (cortesia) é separado e pode ter data para acabar.
    """
    b = company.get('billing') or {}
    paid_until = aware(b.get('paid_until'))
    if paid_until and paid_until > now(): return 'active', b.get('plan') or 'profissional'
    if b.get('status') == 'active' and not paid_until: return 'active', b.get('plan') or 'profissional'  # registro antigo, sem data
    ct = company.get('courtesy') or {}
    if ct.get('plan') in PLANS and (not ct.get('until') or aware(ct['until']) > now()): return 'active', ct['plan']
    p = company.get('plan')
    if not b.get('sub_id') and not ct:  # plano dado pelo admin antes de existir a cortesia
        if p in PLANS: return 'active', p
        if p in LEGACY: return 'active', LEGACY[p]
    if p == 'trial' and aware(company.get('trial_ends_at')) and aware(company['trial_ends_at']) > now(): return 'trial', 'equipe'
    return 'readonly', None

def kind_of(company):
    """Para o admin: paying (assinatura paga) | courtesy (liberado pelo admin) | trial | readonly."""
    st, _ = plan_of(company)
    if st != 'active': return st
    b = company.get('billing') or {}
    paid_until = aware(b.get('paid_until'))
    return 'paying' if b.get('sub_id') and (b.get('status') == 'active' or (paid_until and paid_until > now())) else 'courtesy'

def monthly_value(company):
    """Quanto a assinatura rende por mês (anual dividido por 12). Só conta assinatura ativa, não cancelada."""
    b = company.get('billing') or {}
    if kind_of(company) != 'paying' or b.get('status') != 'active': return 0
    amount = float(b.get('amount') or 0)
    return round(amount / 12, 2) if b.get('cycle') == 'annual' else amount

def limits(company):
    st, p = plan_of(company)
    if st == 'readonly': return {'state': st, 'plan': None, 'users': 0, 'ai': 0, 'features': []}
    P = PLANS[p]; extra = (company.get('billing') or {}).get('extra_users') or 0
    return {'state': st, 'plan': p, 'users': P['users'] + (extra if st == 'active' else 0), 'ai': TRIAL_AI if st == 'trial' else P['ai'], 'features': P['features']}

def has(company, feature): return feature in limits(company)['features']

def require(user, feature):
    if not has(user['company'], feature):
        st = limits(user['company'])['state']
        if st == 'readonly': raise HTTPException(402, 'Sua conta está só para leitura. Assine um plano para voltar a usar.')
        need = next(PLANS[k]['name'] for k in ('essencial', 'profissional', 'equipe') if feature in PLANS[k]['features'])
        raise HTTPException(402, f'{FEATURES.get(feature, feature)} faz parte do plano {need}. Mude de plano em Empresa › Assinatura.')

async def ai_used(company_id):
    u = await db.ai_usage.find_one({'company_id': company_id, 'month': now().strftime('%Y-%m')}, {'_id': 0}) or {}
    return (u.get('replies') or 0) + (u.get('suggestions') or 0)

async def ai_allowed(company):
    return await ai_used(company['id']) < limits(company)['ai']

async def users_used(company_id): return await db.users.count_documents({'company_id': company_id, 'active': True})

async def check_seat(user):
    lim = limits(user['company'])
    if lim['state'] == 'readonly': raise HTTPException(402, 'Sua conta está só para leitura. Assine um plano para voltar a usar.')
    if await users_used(user['company_id']) >= lim['users']:
        raise HTTPException(402, f'Seu plano permite {lim["users"]} {"pessoa" if lim["users"] == 1 else "pessoas"}. Adicione usuários ou mude de plano em Empresa › Assinatura.')

# só leitura: o que ainda funciona sem assinatura
READONLY_OK = ('/api/auth/', '/api/billing', '/api/me/', '/api/push/', '/api/wa/webhook', '/api/admin', '/api/l/')
def blocked(company, method, path):
    if method in ('GET', 'HEAD', 'OPTIONS'): return False
    if any(path.startswith(p) for p in READONLY_OK): return False
    return limits(company)['state'] == 'readonly'

# ---------- Mercado Pago (isolado para os testes) ----------
async def mp(method, path, body=None):
    tok = os.environ.get('MP_ACCESS_TOKEN')
    if not tok: raise HTTPException(503, 'A cobrança ainda não foi configurada. Fale com o suporte do Oi Nanda.')
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, MP + path, json=body, headers={'Authorization': 'Bearer ' + tok, 'X-Idempotency-Key': uid()})
    try: d = r.json()
    except ValueError: d = {}
    if r.status_code >= 400:
        log.warning('mercado pago %s %s: %s', method, path, str(d)[:300])
        raise HTTPException(400, 'O Mercado Pago recusou: ' + (d.get('message') or f'erro {r.status_code}'))
    return d

def price_of(plan, cycle, extra):
    m = PLANS[plan]['price'] + EXTRA_USER * extra
    return round(m * (ANNUAL_MONTHS if cycle == 'annual' else 1), 2)

def app_url(request):
    o = (os.environ.get('APP_ORIGINS') or '').split(',')[0].strip()
    return o or str(request.base_url).rstrip('/')

class SubIn(BaseModel):
    plan: Literal['essencial', 'profissional', 'equipe']
    cycle: Literal['monthly', 'annual'] = 'monthly'
    extra_users: int = Field(default=0, ge=0, le=50)
    email: EmailStr

@router.get('/billing')
async def status(user=Depends(current_user)):
    c = user['company']; lim = limits(c); b = c.get('billing') or {}
    trial_left = max(0, (aware(c['trial_ends_at']) - now()).days + 1) if c.get('plan') == 'trial' and c.get('trial_ends_at') else None
    return {'state': lim['state'], 'plan': lim['plan'], 'limits': lim, 'trial_left': trial_left if lim['state'] == 'trial' else None,
            'usage': {'users': await users_used(c['id']), 'ai': await ai_used(c['id'])},
            'billing': {k: b.get(k) for k in ('plan', 'cycle', 'extra_users', 'status', 'amount', 'next_payment', 'paid_until', 'email', 'pending_url')},
            'plans': PLANS, 'features': FEATURES, 'extra_user': EXTRA_USER, 'annual_months': ANNUAL_MONTHS, 'ready': bool(os.environ.get('MP_ACCESS_TOKEN'))}

@router.post('/billing/subscribe')
async def subscribe(body: SubIn, request: Request, user=Depends(owners)):
    c = user['company']; b = c.get('billing') or {}
    if b.get('status') == 'active' and b.get('sub_id'):
        try: await mp('PUT', f'/preapproval/{b["sub_id"]}', {'status': 'cancelled'})  # troca de plano: encerra a anterior
        except HTTPException: pass
    amount = price_of(body.plan, body.cycle, body.extra_users)
    base = app_url(request)
    sub = await mp('POST', '/preapproval', {
        'reason': f'Oi Nanda · plano {PLANS[body.plan]["name"]}' + (' (anual)' if body.cycle == 'annual' else ''),
        'external_reference': c['id'], 'payer_email': body.email, 'back_url': f'{base}/empresa/assinatura', 'status': 'pending',
        'auto_recurring': {'frequency': 12 if body.cycle == 'annual' else 1, 'frequency_type': 'months', 'transaction_amount': amount, 'currency_id': 'BRL'}})
    nb = {**b, 'pending': {'sub_id': sub['id'], 'plan': body.plan, 'cycle': body.cycle, 'extra_users': body.extra_users, 'amount': amount, 'email': body.email, 'at': now()},
          'pending_url': sub.get('init_point')}
    if b.get('status') != 'active': nb.update(status='pending')
    await db.companies.update_one({'id': c['id']}, {'$set': {'billing': nb}})
    await audit(request, user, 'Iniciou assinatura', f'{PLANS[body.plan]["name"]} · {body.cycle} · R$ {amount:.2f}')
    return {'url': sub.get('init_point'), 'amount': amount}

async def apply(company, sub):
    """Atualiza a empresa a partir da assinatura no Mercado Pago (fonte da verdade)."""
    b = company.get('billing') or {}
    pend = b.get('pending') or {}
    cur = pend if pend.get('sub_id') == sub.get('id') else ({'sub_id': b.get('sub_id'), **{k: b.get(k) for k in ('plan', 'cycle', 'extra_users', 'amount', 'email')}}
                                                            if b.get('sub_id') == sub.get('id') else None)
    if not cur: return False
    st = sub.get('status')
    nxt = sub.get('next_payment_date')
    nxt_dt = aware(datetime.fromisoformat(nxt.replace('Z', '+00:00'))) if nxt else None
    upd = dict(b)
    if st == 'authorized':
        paid_until = (nxt_dt + timedelta(days=3)) if nxt_dt else now() + timedelta(days=33)
        bad = await db.payments.find({'sub_id': sub['id'], 'status': 'refused', 'due': {'$lt': now()}}, {'_id': 0}).sort('due', -1).to_list(1)
        if bad:  # tem cobrança recusada em aberto: o acesso vai só até 7 dias depois do vencimento dela
            ok = await db.payments.find({'sub_id': sub['id'], 'status': 'paid'}, {'_id': 0}).sort('due', -1).to_list(1)
            if not ok or aware(ok[0]['due']) < aware(bad[0]['due']): paid_until = min(paid_until, aware(bad[0]['due']) + timedelta(days=GRACE_DAYS))
        upd.update(sub_id=sub['id'], plan=cur['plan'], cycle=cur['cycle'], extra_users=cur.get('extra_users') or 0, amount=cur['amount'], email=cur.get('email'),
                   status='active', next_payment=nxt_dt, paid_until=paid_until)
        if not b.get('since') or b.get('sub_id') != sub['id']: upd['since'] = b.get('since') or now()
        upd.pop('pending', None); upd.pop('pending_url', None)
        await db.companies.update_one({'id': company['id']}, {'$set': {'billing': upd, 'plan': cur['plan']}, '$unset': {'courtesy': ''}})
        return True
    if b.get('sub_id') == sub.get('id') and st in ('paused', 'cancelled'):
        upd['status'] = st  # continua usando até paid_until
        if st == 'cancelled' and not upd.get('cancelled_at'): upd['cancelled_at'] = now()
        await db.companies.update_one({'id': company['id']}, {'$set': {'billing': upd}})
        return True
    return False

@router.post('/billing/sync')
async def sync(user=Depends(current_user)):
    c = user['company']; b = c.get('billing') or {}
    for sid in {x for x in ((b.get('pending') or {}).get('sub_id'), b.get('sub_id')) if x}:
        try: await apply(c, await mp('GET', f'/preapproval/{sid}'))
        except HTTPException: pass
        c = await db.companies.find_one({'id': c['id']}, {'_id': 0})
    return await status_for(c, user)

async def status_for(c, user):
    user = {**user, 'company': c}
    return await status(user)

@router.post('/billing/cancel')
async def cancel(request: Request, user=Depends(owners)):
    b = user['company'].get('billing') or {}
    if not b.get('sub_id'): raise HTTPException(400, 'Nenhuma assinatura ativa.')
    await mp('PUT', f'/preapproval/{b["sub_id"]}', {'status': 'cancelled'})
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'billing.status': 'cancelled', 'billing.cancelled_at': now()}})
    await audit(request, user, 'Cancelou a assinatura', b.get('plan'))
    import notices
    await notices.deliver(user['company'], 'cancelled', {'data': notices.day(b.get('paid_until'))}, once=f'cancelled:{b["sub_id"]}')
    return {'ok': True, 'paid_until': b.get('paid_until')}

# ---------- cobranças (cada fatura da assinatura no Mercado Pago) ----------
def invoice_status(inv):
    p = (inv.get('payment') or {}).get('status')
    if p == 'approved': return 'paid'
    if inv.get('status') in ('canceled', 'cancelled'): return 'cancelled'
    if p in ('rejected', 'cancelled'): return 'refused'
    return 'pending'

async def company_of_sub(sub_id, ref=None):
    c = await db.companies.find_one({'$or': [{'billing.sub_id': sub_id}, {'billing.pending.sub_id': sub_id}]}, {'_id': 0}) if sub_id else None
    return c or (await db.companies.find_one({'id': str(ref)}, {'_id': 0}) if ref else None)

async def record_payment(inv, company=None):
    """Guarda (ou atualiza) uma cobrança. Paga: estende o acesso. Mudou de situação: avisa o dono por e-mail."""
    if not inv or not inv.get('id'): return None
    sub_id = inv.get('preapproval_id')
    company = company or await company_of_sub(sub_id, inv.get('external_reference'))
    if not company: return None
    b = company.get('billing') or {}
    cur = (b.get('pending') or {}) if (b.get('pending') or {}).get('sub_id') == sub_id else b
    st = invoice_status(inv)
    due = aware(inv.get('debit_date')) or now()
    pid = str(inv['id'])
    old = await db.payments.find_one({'id': pid}, {'_id': 0})
    doc = {'id': pid, 'company_id': company['id'], 'sub_id': sub_id, 'plan': cur.get('plan') or company.get('plan'), 'cycle': cur.get('cycle') or 'monthly',
           'amount': float(inv.get('transaction_amount') or cur.get('amount') or 0), 'status': st, 'due': due, 'attempt': inv.get('retry_attempt') or 0,
           'detail': (inv.get('payment') or {}).get('status_detail'), 'updated_at': now()}
    if st == 'paid': doc['paid_at'] = (old or {}).get('paid_at') or aware(inv.get('last_modified')) or now()
    await db.payments.update_one({'id': pid}, {'$set': doc, '$setOnInsert': {'created_at': now()}}, upsert=True)
    if st == 'paid':
        until = due + timedelta(days=(366 if doc['cycle'] == 'annual' else 31) + 3)
        if not aware(b.get('paid_until')) or aware(b['paid_until']) < until:
            await db.companies.update_one({'id': company['id']}, {'$set': {'billing.paid_until': until}})
    if (old or {}).get('status') != st and st in ('paid', 'refused'):
        try:
            import mailer
            await mailer.payment_notice(company, doc)
        except Exception: log.exception('falha no aviso de cobrança')
    return doc

async def sync_invoices(company):
    b = company.get('billing') or {}
    for sid in {x for x in (b.get('sub_id'), (b.get('pending') or {}).get('sub_id')) if x}:
        d = await mp('GET', f'/authorized_payments/search?preapproval_id={sid}')
        for inv in (d.get('results') or [])[:50]: await record_payment(inv, company)

def signature_ok(request, data_id):
    """Assinatura do aviso do Mercado Pago (x-signature). Sem segredo configurado, aceita e confere tudo na API."""
    secret = os.environ.get('MP_WEBHOOK_SECRET')
    if not secret: return True
    sig = request.headers.get('x-signature', ''); rid = request.headers.get('x-request-id', '')
    parts = dict(p.split('=', 1) for p in sig.split(',') if '=' in p)
    msg = f'id:{data_id};request-id:{rid};ts:{parts.get("ts", "")};'
    return hmac.compare_digest(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest(), parts.get('v1', ''))

@router.post('/billing/webhook')
async def webhook(request: Request):
    try: body = await request.json()
    except Exception: body = {}
    kind = str(body.get('type') or body.get('topic') or request.query_params.get('type') or request.query_params.get('topic') or '')
    did = str((body.get('data') or {}).get('id') or request.query_params.get('data.id') or request.query_params.get('id') or '')
    if not did or not signature_ok(request, did): return {'ok': True}
    try:
        if 'authorized_payment' in kind:
            pay = await mp('GET', f'/authorized_payments/{did}')
            did = pay.get('preapproval_id') or ''
            await record_payment(pay)
        if did and ('preapproval' in kind or 'authorized_payment' in kind):
            sub = await mp('GET', f'/preapproval/{did}')
            company = await db.companies.find_one({'id': sub.get('external_reference')}, {'_id': 0})
            if company: await apply(company, sub)
    except HTTPException: pass
    except Exception: log.exception('falha no aviso do Mercado Pago')
    return {'ok': True}

_last_full = None
async def tick():
    """Confere assinaturas pendentes (caso o aviso do Mercado Pago não chegue) e, a cada 6 horas, todas as ativas:
    atualiza a data paga, registra as cobranças e manda o aviso de cobrança chegando."""
    global _last_full
    if not os.environ.get('MP_ACCESS_TOKEN'): return
    for c in await db.companies.find({'billing.pending.sub_id': {'$exists': True}, 'billing.pending.at': {'$gte': now() - timedelta(days=3)}}, {'_id': 0}).to_list(200):
        try: await apply(c, await mp('GET', f'/preapproval/{c["billing"]["pending"]["sub_id"]}'))
        except Exception: pass
    if _last_full and now() - _last_full < timedelta(hours=6): return
    _last_full = now()
    await full_sync()

async def full_sync():
    for c in await db.companies.find({'billing.sub_id': {'$exists': True}, 'billing.status': {'$in': ['active', 'paused']}}, {'_id': 0}).to_list(2000):
        try:
            await sync_invoices(c)
            c = await db.companies.find_one({'id': c['id']}, {'_id': 0})
            await apply(c, await mp('GET', f'/preapproval/{c["billing"]["sub_id"]}'))
        except Exception: log.warning('não deu para conferir a assinatura de %s', c.get('name'))
    try:
        import mailer
        import notices
        await notices.wa_refresh(); await notices.check_numbers()
    except Exception: log.exception('falha no aviso de cobrança chegando')
