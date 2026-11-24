"""Planos, limites e assinatura (cobrança recorrente pelo Mercado Pago, conta da Fagotti Marketing Direto).

Planos: Essencial, Profissional e Equipe. Cada um tem número de usuários, atendimentos da Nanda por mês e módulos.

Entrada: a conta nasce em modo demonstração (conversa com a Nanda, explora o sistema, mas não conecta o WhatsApp).
Para atender de verdade, ativa os 14 dias grátis cadastrando o cartão no Mercado Pago (assinatura com free_trial):
hoje não cobra nada, avisa 3 dias antes e a primeira mensalidade entra no dia 15. Cancela com um clique.
Acabou a assinatura sem pagamento: a conta fica só para leitura até assinar.

Nanda: cobrada por atendimento (um cliente atendido por ela num dia, não importa quantas respostas). Acabou a cota
do mês, ela passa a sugerir respostas (copiloto) em vez de parar. Pacote extra de 500 atendimentos por R$ 59, com
um clique ou automático até o teto que o cliente escolher; entra na próxima mensalidade.

Cobrança: assinatura do Mercado Pago (preapproval) no cartão, mensal ou anual. O dono escolhe o plano, vai para a
página do Mercado Pago, paga e volta. O status vem pelo aviso (webhook) do Mercado Pago e é sempre conferido na API.
O token fica só no servidor (deploy/set-mercadopago.sh).
"""
import math, os, hmac, hashlib, logging
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
TRIAL_AI = 1000  # sugestões da Nanda no teste sem cartão
TRIAL_ATEND = int(os.environ.get('TRIAL_ATEND', '200'))  # atendimentos da Nanda sozinha nos 14 dias sem cartão (custo de IA sob controle)
TRIAL_DAYS_CARD = 14
DEMO_TESTS = 60  # conversas de teste com a Nanda no modo demonstração (por mês)
PACK = {'size': 500, 'price': 59}
FLOOR = 0.08  # nunca vender atendimento abaixo disso (custo de IA com margem de segurança ~R$ 0,06)
AUTO_PACKS_MAX = 10
GRACE_DAYS = 7  # cobrança recusada: quantos dias a conta segue funcionando depois do vencimento
FEATURES = {
    'automacoes': 'Automações e lembretes', 'promocoes': 'Promoções e cupons', 'auto': 'Nanda respondendo sozinha',
    'segmentos': 'Segmentos de clientes', 'campanhas': 'Campanhas', 'retorno': 'Retorno por anúncio', 'jornada_rel': 'Relatório de jornada',
    'equipes': 'Equipes, fila e rodízio', 'metas': 'Metas, prêmios e ranking', 'posvenda': 'Funil de pós-venda e renovação',
}
PLANS = {
    'essencial': {'name': 'Essencial', 'price': 147, 'users': 2, 'atend': 0, 'sug': 1000, 'features': ['automacoes', 'promocoes'],
                  'pitch': 'Para quem está começando: conversas, agenda, funil, vendas e a Nanda sugerindo respostas.'},
    'profissional': {'name': 'Profissional', 'price': 297, 'users': 5, 'atend': 600, 'sug': 3000, 'features': ['automacoes', 'promocoes', 'auto', 'segmentos', 'campanhas', 'retorno', 'jornada_rel'],
                     'pitch': 'A Nanda atendendo sozinha, campanhas, segmentos e retorno por anúncio.'},
    'equipe': {'name': 'Equipe', 'price': 597, 'users': 12, 'atend': 2000, 'sug': 8000, 'features': list(FEATURES),
               'pitch': 'Para operação com time: equipes, rodízio, metas, ranking e pós-venda.'},
}
for _p in PLANS.values(): _p['ai'] = _p['atend'] or _p['sug']  # cota principal: atendimentos (ou sugestões, no Essencial)
assert PACK['price'] / PACK['size'] >= FLOOR

LEGACY = {'oferta': 'profissional', 'completo': 'equipe', 'rede': 'equipe'}  # planos antigos dados pelo admin

def plan_of(company):
    """Situação de acesso: (estado, plano). Estado: trial | active | readonly.

    Assinatura do Mercado Pago vale até a data paga (paid_until). Cancelou ou parou de pagar: quando a data passa, a conta
    volta a ser só leitura. Plano liberado pelo admin (cortesia) é separado e pode ter data para acabar.
    """
    b = company.get('billing') or {}
    tu = aware(b.get('trial_until'))
    if tu and tu > now() and b.get('sub_id') and b.get('status') in ('active', 'cancelled', 'paused'): return 'trial', b.get('plan') or 'profissional'
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
    if p == 'demo': return 'demo', 'equipe'
    return 'readonly', None

def card_trial(company):
    """Teste de 14 dias com cartão cadastrado (assinatura com free_trial): data em que a primeira mensalidade entra."""
    b = company.get('billing') or {}; tu = aware(b.get('trial_until'))
    return tu if tu and tu > now() and b.get('sub_id') else None

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
    if st == 'readonly': return {'state': st, 'plan': None, 'users': 0, 'ai': 0, 'atend': 0, 'sug': 0, 'features': []}
    P = PLANS[p]; b = company.get('billing') or {}; extra = b.get('extra_users') or 0
    if st == 'demo': return {'state': st, 'plan': p, 'users': 1, 'ai': 0, 'atend': 0, 'sug': 0, 'tests': DEMO_TESTS, 'features': P['features']}
    legacy = st == 'trial' and not card_trial(company)  # teste antigo, sem cartão: tudo liberado com a cota de antes
    atend, sug = (min(P['atend'], TRIAL_ATEND), TRIAL_AI) if legacy else (P['atend'], P['sug'])
    return {'state': st, 'plan': p, 'users': P['users'] + (extra if st in ('active', 'trial') and not legacy else 0), 'ai': atend or sug, 'atend': atend, 'sug': sug,
            'features': P['features']}

def has(company, feature): return feature in limits(company)['features']

def require(user, feature):
    if not has(user['company'], feature):
        st = limits(user['company'])['state']
        if st == 'readonly': raise HTTPException(402, 'Sua conta está só para leitura. Assine um plano para voltar a usar.')
        need = next(PLANS[k]['name'] for k in ('essencial', 'profissional', 'equipe') if feature in PLANS[k]['features'])
        raise HTTPException(402, f'{FEATURES.get(feature, feature)} faz parte do plano {need}. Mude de plano em Empresa › Assinatura.')

def month(): return now().strftime('%Y-%m')

async def usage_of(company_id):
    u = await db.ai_usage.find_one({'company_id': company_id, 'month': month()}, {'_id': 0}) or {}
    return {'atend': u.get('atend') or 0, 'sug': u.get('suggestions') or 0, 'tests': u.get('tests') or 0, 'replies': u.get('replies') or 0}

async def packs_of(company_id, m=None):
    return await db.ai_packs.find({'company_id': company_id, 'month': m or month()}, {'_id': 0}).sort('at', 1).to_list(100)

async def quota(company):
    """Atendimentos do mês: o do plano mais os pacotes extras."""
    lim = limits(company)
    return lim['atend'] + sum(p['size'] for p in await packs_of(company['id'])) if lim['atend'] else 0

async def ai_used(company_id):
    """Uso da cota principal: atendimentos (planos com a Nanda sozinha) ou sugestões (Essencial)."""
    u = await usage_of(company_id)
    c = await db.companies.find_one({'id': company_id}, {'_id': 0}) or {}
    return u['atend'] if limits(c)['atend'] else u['sug']

async def served_today(company_id, contact_id):
    return bool(contact_id and await db.ai_days.find_one({'company_id': company_id, 'contact_id': contact_id, 'day': now().strftime('%Y-%m-%d')}, {'_id': 1}))

async def count_atend(company_id, contact_id):
    """Um cliente atendido pela Nanda num dia conta um atendimento, não importa quantas respostas."""
    if not contact_id: return False
    r = await db.ai_days.update_one({'company_id': company_id, 'contact_id': contact_id, 'day': now().strftime('%Y-%m-%d')},
                                    {'$setOnInsert': {'at': now()}}, upsert=True)
    if not r.upserted_id: return False
    await db.ai_usage.update_one({'company_id': company_id, 'month': month()}, {'$inc': {'atend': 1}}, upsert=True)
    return True

async def ai_allowed(company, kind='auto', contact_id=None):
    """auto: a Nanda pode atender sozinha? (cliente já atendido hoje segue até o fim do dia). sug: ainda tem sugestões?"""
    lim = limits(company); u = await usage_of(company['id'])
    if kind == 'sug': return u['sug'] < lim['sug']
    if kind == 'test': return lim['state'] != 'demo' or u['tests'] < lim.get('tests', 0)
    if not lim['atend']: return False
    if await served_today(company['id'], contact_id): return True
    if u['atend'] < await quota(company): return True
    return await auto_pack(company)

def can_buy_pack(company):
    b = company.get('billing') or {}
    if not b.get('sub_id') or b.get('status') != 'active': return 'Os pacotes extras ficam disponíveis com a assinatura ativa.'
    if b.get('cycle') == 'annual': return 'No plano anual, fale com o suporte para pacotes extras.'
    if not limits(company)['atend']: return 'Os pacotes são para a Nanda atendendo sozinha, que começa no plano Profissional.'
    return None

async def add_pack(company, how):
    p = {'id': uid(), 'company_id': company['id'], 'month': month(), 'size': PACK['size'], 'price': PACK['price'], 'how': how, 'status': 'a_cobrar', 'at': now()}
    await db.ai_packs.insert_one(p.copy())
    return p

async def auto_pack(company):
    """Cota acabou: se o cliente ligou o pacote automático e ainda não chegou no teto do mês, entra mais um."""
    mx = int(company.get('ai_auto_packs') or 0)
    if not mx or can_buy_pack(company): return False
    if sum(1 for p in await packs_of(company['id']) if p['how'] == 'auto') >= mx: return False
    await add_pack(company, 'auto')
    try:
        import notices
        await notices.deliver(company, 'ai_pack', {'valor': notices.money(PACK['price'])}, once='ai_pack:' + str(now().timestamp()))
    except Exception: log.exception('falha no aviso de pacote automático')
    return True

async def users_used(company_id): return await db.users.count_documents({'company_id': company_id, 'active': True})

def require_live(user, what='conectar o WhatsApp'):
    """Modo demonstração: explora tudo, mas para atender de verdade precisa ativar os 14 dias grátis."""
    if limits(user['company'])['state'] == 'demo':
        raise HTTPException(402, f'Para {what}, ative seus 14 dias grátis em Empresa › Assinatura. Hoje não é cobrado nada.')

async def check_seat(user):
    lim = limits(user['company'])
    if lim['state'] == 'readonly': raise HTTPException(402, 'Sua conta está só para leitura. Assine um plano para voltar a usar.')
    require_live(user, 'chamar o seu time')
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
    if user['role'] not in ('owner', 'manager'):  # atendente: só o que a tela dela precisa, sem valor nem dados da assinatura
        return {'state': lim['state'], 'plan': lim['plan'], 'limits': lim}
    ct = card_trial(c)
    end = ct or (aware(c['trial_ends_at']) if c.get('plan') == 'trial' and c.get('trial_ends_at') else None)
    trial_left = max(0, (end - now()).days + 1) if end else None
    u = await usage_of(c['id'])
    return {'state': lim['state'], 'plan': lim['plan'], 'limits': lim, 'trial_left': trial_left if lim['state'] == 'trial' else None,
            'card_trial': ct, 'trial_offer': c.get('plan') == 'demo' and not c.get('trial_used') and not b.get('since'), 'trial_days': TRIAL_DAYS_CARD,
            'usage': {'users': await users_used(c['id']), 'ai': await ai_used(c['id']), **u, 'quota': await quota(c)},
            'billing': {k: b.get(k) for k in ('plan', 'cycle', 'extra_users', 'status', 'amount', 'next_payment', 'paid_until', 'email', 'pending_url', 'trial_until')},
            'plans': PLANS, 'features': FEATURES, 'extra_user': EXTRA_USER, 'annual_months': ANNUAL_MONTHS, 'pack': PACK, 'ready': bool(os.environ.get('MP_ACCESS_TOKEN'))}

@router.post('/billing/subscribe')
async def subscribe(body: SubIn, request: Request, user=Depends(owners)):
    c = user['company']; b = c.get('billing') or {}
    if b.get('status') == 'active' and b.get('sub_id'):
        try: await mp('PUT', f'/preapproval/{b["sub_id"]}', {'status': 'cancelled'})  # troca de plano: encerra a anterior
        except HTTPException: pass
    amount = price_of(body.plan, body.cycle, body.extra_users)
    base = app_url(request)
    trial = c.get('plan') == 'demo' and not c.get('trial_used') and not b.get('since')  # 14 dias grátis uma vez, ao sair do modo demonstração
    days = TRIAL_DAYS_CARD
    te = aware(c.get('trial_ends_at'))
    if c.get('plan') == 'trial' and te and te > now() and not b.get('since'):  # assinou durante o teste sem cartão: a cobrança só começa quando o teste acaba
        trial, days = True, max(1, math.ceil((te - now()).total_seconds() / 86400))
    rec = {'frequency': 12 if body.cycle == 'annual' else 1, 'frequency_type': 'months', 'transaction_amount': amount, 'currency_id': 'BRL'}
    if trial: rec['free_trial'] = {'frequency': days, 'frequency_type': 'days'}
    sub = await mp('POST', '/preapproval', {
        'reason': f'Oi Nanda · plano {PLANS[body.plan]["name"]}' + (' (anual)' if body.cycle == 'annual' else '') + (f' · {days} dias grátis' if trial else ''),
        'external_reference': c['id'], 'payer_email': body.email, 'back_url': f'{base}/empresa/assinatura', 'status': 'pending', 'auto_recurring': rec})
    nb = {**b, 'pending': {'sub_id': sub['id'], 'plan': body.plan, 'cycle': body.cycle, 'extra_users': body.extra_users, 'amount': amount, 'email': body.email, 'at': now(),
                           'trial': trial},
          'pending_url': sub.get('init_point')}
    if b.get('status') != 'active': nb.update(status='pending')
    await db.companies.update_one({'id': c['id']}, {'$set': {'billing': nb}})
    await audit(request, user, 'Iniciou assinatura', f'{PLANS[body.plan]["name"]} · {body.cycle} · R$ {amount:.2f}')
    return {'url': sub.get('init_point'), 'amount': amount, 'trial': trial}

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
        extra = {}
        if cur.get('trial') and b.get('sub_id') != sub['id']:  # assinatura nova com 14 dias grátis: a primeira mensalidade entra no fim do teste
            tu = nxt_dt or now() + timedelta(days=TRIAL_DAYS_CARD)
            upd['trial_until'] = tu; extra = {'trial_used': True, 'trial_ends_at': tu}
        upd.pop('pending', None); upd.pop('pending_url', None)
        await db.companies.update_one({'id': company['id']}, {'$set': {'billing': upd, 'plan': cur['plan'], **extra}, '$unset': {'courtesy': ''}})
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
    ct = card_trial(user['company'])
    upd = {'billing.status': 'cancelled', 'billing.cancelled_at': now()}
    until = b.get('paid_until')
    if ct: until = ct; upd['billing.paid_until'] = ct  # cancelou no teste: usa até o fim dos 14 dias e nada é cobrado
    await db.companies.update_one({'id': user['company_id']}, {'$set': upd})
    await audit(request, user, 'Cancelou a assinatura' + (' no teste grátis' if ct else ''), b.get('plan'))
    import notices
    await notices.deliver(user['company'], 'cancelled', {'data': notices.day(until)}, once=f'cancelled:{b["sub_id"]}')
    return {'ok': True, 'paid_until': until, 'in_trial': bool(ct)}

# ---------- Nanda: atendimentos, pacotes extras ----------
@router.get('/billing/ai')
async def ai_status(user=Depends(current_user)):
    c = user['company']; lim = limits(c); u = await usage_of(c['id']); q = await quota(c)
    packs = await packs_of(c['id'])
    return {'state': lim['state'], 'plan': lim['plan'], 'atend': {'used': u['atend'], 'quota': q, 'plan': lim['atend']}, 'sug': {'used': u['sug'], 'limit': lim['sug']},
            'packs': [{k: p[k] for k in ('size', 'price', 'how', 'at', 'status')} for p in packs], 'pack': PACK, 'auto_packs': int(c.get('ai_auto_packs') or 0),
            'auto_max': AUTO_PACKS_MAX, 'can_buy': can_buy_pack(c) is None, 'why_not': can_buy_pack(c), 'next_payment': (c.get('billing') or {}).get('next_payment')}

@router.post('/billing/ai/pack')
async def buy_pack(request: Request, user=Depends(owners)):
    c = user['company']; why = can_buy_pack(c)
    if why: raise HTTPException(400, why)
    if sum(1 for p in await packs_of(c['id'])) >= 20: raise HTTPException(400, 'Chegou ao máximo de pacotes do mês. Fale com o suporte para um plano sob medida.')
    p = await add_pack(c, 'click')
    await audit(request, user, 'Comprou pacote extra da Nanda', f'+{PACK["size"]} atendimentos · R$ {PACK["price"]}')
    return await ai_status({**user, 'company': await db.companies.find_one({'id': c['id']}, {'_id': 0})})

class AutoIn(BaseModel):
    max: int = Field(ge=0, le=AUTO_PACKS_MAX)

@router.put('/billing/ai/auto')
async def set_auto(body: AutoIn, request: Request, user=Depends(owners)):
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'ai_auto_packs': body.max}})
    await audit(request, user, 'Mudou o pacote automático da Nanda', f'até {body.max} por mês' if body.max else 'desligado')
    return await ai_status({**user, 'company': await db.companies.find_one({'id': user['company_id']}, {'_id': 0})})

async def bill_packs():
    """Pacotes extras entram na próxima mensalidade: 2 dias antes da cobrança soma no valor da assinatura; paga, volta ao valor do plano."""
    n = now()
    for c in await db.companies.find({'billing.status': 'active', 'billing.cycle': {'$ne': 'annual'}, 'billing.next_payment': {'$gt': n, '$lte': n + timedelta(days=2)},
                                      'billing.extra_due': {'$exists': False}}, {'_id': 0}).to_list(500):
        packs = await db.ai_packs.find({'company_id': c['id'], 'status': 'a_cobrar'}, {'_id': 0}).to_list(100)
        if not packs: continue
        b = c['billing']; extra = sum(p['price'] for p in packs)
        try: await mp('PUT', f'/preapproval/{b["sub_id"]}', {'auto_recurring': {'transaction_amount': round(b['amount'] + extra, 2), 'currency_id': 'BRL'}})
        except HTTPException: continue
        await db.ai_packs.update_many({'id': {'$in': [p['id'] for p in packs]}}, {'$set': {'status': 'na_fatura', 'due': b['next_payment']}})
        await db.companies.update_one({'id': c['id']}, {'$set': {'billing.extra_due': extra, 'billing.extra_for': b['next_payment']}})

async def packs_paid(company):
    """Mensalidade com pacotes paga: marca os pacotes e devolve a assinatura ao valor do plano."""
    b = company.get('billing') or {}
    if not b.get('extra_due'): return
    try: await mp('PUT', f'/preapproval/{b["sub_id"]}', {'auto_recurring': {'transaction_amount': b['amount'], 'currency_id': 'BRL'}})
    except HTTPException: return
    await db.ai_packs.update_many({'company_id': company['id'], 'status': 'na_fatura'}, {'$set': {'status': 'pago', 'paid_at': now()}})
    await db.companies.update_one({'id': company['id']}, {'$unset': {'billing.extra_due': '', 'billing.extra_for': ''}})

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
        if b.get('extra_due') and aware(b.get('extra_for')) and due >= aware(b['extra_for']) - timedelta(days=3): await packs_paid(company)
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
    try: await bill_packs()
    except Exception: log.exception('falha ao somar pacotes na mensalidade')

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
