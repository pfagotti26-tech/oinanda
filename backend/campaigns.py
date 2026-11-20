"""Campanhas no WhatsApp para um público (segmento).

Fora da janela de 24 h, a Meta só aceita modelo aprovado, então a campanha usa um modelo. Pode gerar um cupom
individual para cada pessoa ({cupom}). Envia aos poucos (para não prejudicar o número), só das 8h às 21h,
e nunca para quem pediu para não receber. Resultado: entregues, responderam, compraram e quanto faturou.

Freios do número (para o cliente não ter o WhatsApp bloqueado por causa de uma campanha):
- respeita o limite diário de pessoas novas da Meta e continua no dia seguinte, sozinho;
- a mesma pessoa recebe no máximo uma promoção a cada 7 dias;
- pausa sozinho se muita gente pedir para sair, se muitas mensagens falharem ou se a nota do número cair,
  e avisa o dono com o que fazer.
"""
import logging
from datetime import timedelta, timezone
from typing import Optional, List
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from wallet import Rule, run_segment

router = APIRouter()
log = logging.getLogger('oinanda.camp')
PER_TICK = 30  # mensagens por minuto
PROMO_GAP = timedelta(days=7)        # uma promoção por pessoa a cada 7 dias
BRAKE_MIN = 30                       # só avalia o freio depois de 30 enviadas
BRAKE_OPTOUT = 0.03                  # 3% pedindo para sair
BRAKE_ERRORS = 0.20                  # 20% falhando
REASONS = {'saida': 'Mais pessoas que o normal pediram para sair da lista.', 'erros': 'Muitas mensagens não foram entregues pela Meta.',
           'qualidade': 'A nota de qualidade do seu número caiu na Meta.', 'modelo': 'A Meta pausou ou recusou o modelo da mensagem.'}

class CampIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    segment_id: Optional[str] = None
    template_id: str
    coupon_promo_id: Optional[str] = None
    coupon_days: int = Field(default=15, ge=1, le=180)

async def audience(user, rules):
    rows = await run_segment(user, rules)
    from wallet import reachable
    return [c for c in rows if reachable(c)]

class PreviewIn(BaseModel):
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    template_id: str

def days_plan(n, left, limit):
    """Quantas pessoas recebem hoje, amanhã... respeitando o limite diário de pessoas novas."""
    if limit is None: return [n]
    out, first = [], min(n, left)
    if first: out.append(first)
    rest = n - first
    while rest > 0 and len(out) < 30:
        out.append(min(rest, limit)); rest -= out[-1]
    return out or [0]

async def recent_promo(company_id, contact_ids):
    rows = await db.campaign_sends.find({'company_id': company_id, 'contact_id': {'$in': contact_ids}, 'status': 'sent', 'category': 'MARKETING',
                                         'at': {'$gte': now() - PROMO_GAP}}, {'_id': 0, 'contact_id': 1}).to_list(10000)
    return {r['contact_id'] for r in rows}

@router.post('/campaigns/preview')
async def preview(body: PreviewIn, user=Depends(managers)):
    import metacost
    t = await db.templates.find_one({'id': body.template_id, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Modelo não encontrado.')
    rows = await run_segment(user, body.rules)
    from wallet import reachable
    ok = [c for c in rows if reachable(c)]
    cat = t.get('category') or 'MARKETING'
    recent = await recent_promo(user['company_id'], [c['id'] for c in ok]) if cat == 'MARKETING' else set()
    n = len(ok) - len(recent)
    r = await metacost.rates(); per = metacost.rate_of(r, cat)
    h = await metacost.health(user['company_id']) or {'limit': 250, 'left': 250, 'used': 0, 'quality': 'UNKNOWN', 'state': 'ok', 'limit_known': False}
    cold = sum(1 for c in ok if c['id'] not in recent and not (c.get('last_in_at') and (now() - (c['last_in_at'] if c['last_in_at'].tzinfo else c['last_in_at'].replace(tzinfo=timezone.utc))) <= timedelta(days=90)))
    exp = await metacost.expected(user['company_id'], cat)
    return {'total': len(rows), 'reachable': n, 'opt_out': len(rows) - len(ok), 'recent_promo': len(recent), 'cold': cold, 'approved': t.get('status') == 'APPROVED',
            'category': cat, 'per': per, 'cost': round(n * per, 2), 'rates_checked': r['checked_label'], 'rates_source': r['source'],
            'health': h, 'plan': days_plan(n, h['left'] if h['left'] is not None else n, h['limit']),
            'expected': exp and {**exp, 'people': round(n * exp['per100'] / 100), 'revenue': round(n * exp['per100'] / 100 * exp['ticket'], 2)},
            'minutes': max(1, -(-n // PER_TICK)), 'needs_coupon': 'cupom' in (t.get('params') or [])}

@router.post('/campaigns')
async def create(body: CampIn, request: Request, user=Depends(managers)):
    from billing import require; require(user, 'campanhas')
    t = await db.templates.find_one({'id': body.template_id, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Modelo não encontrado.')
    if t.get('status') != 'APPROVED': raise HTTPException(400, 'Esse modelo ainda não foi aprovado pela Meta.')
    if 'cupom' in (t.get('params') or []) and not body.coupon_promo_id: raise HTTPException(400, 'O modelo usa {cupom}: escolha a promoção dos cupons.')
    if body.coupon_promo_id and not await db.promos.find_one({'id': body.coupon_promo_id, 'company_id': user['company_id']}): raise HTTPException(404, 'Promoção não encontrada.')
    if not await db.wa_numbers.find_one({'company_id': user['company_id']}): raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    rows = await audience(user, body.rules)
    if not rows: raise HTTPException(400, 'Ninguém neste público pode receber mensagem.')
    if len(rows) > 5000: raise HTTPException(400, 'Público grande demais para uma campanha (máximo 5.000). Divida em partes.')
    import metacost
    h = await metacost.health(user['company_id'], fresh=True) or {}
    r = await metacost.rates()
    c = {'id': uid(), 'company_id': user['company_id'], 'name': body.name.strip(), 'rules': [r.model_dump() for r in body.rules], 'segment_id': body.segment_id,
         'template_id': t['id'], 'template': t['label'], 'coupon_promo_id': body.coupon_promo_id, 'coupon_days': body.coupon_days,
         'category': t.get('category') or 'MARKETING', 'per': metacost.rate_of(r, t.get('category')), 'quality_start': h.get('quality'),
         'status': 'sending', 'total': len(rows), 'by': user['name'], 'created_at': now()}
    await db.campaigns.insert_one(c.copy())
    await db.campaign_sends.insert_many([{'id': uid(), 'campaign_id': c['id'], 'company_id': user['company_id'], 'contact_id': x['id'], 'category': c['category'], 'status': 'queued'} for x in rows])
    await audit(request, user, 'Criou campanha', f'{c["name"]} · {len(rows)} pessoas')
    c.pop('_id', None)
    return c

async def send_one(company, camp, t, s):
    from automations import context, send_template, personal_coupon, in_quiet
    contact = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0})
    if not contact or contact.get('opt_out'): return 'skipped', 'pediu para não receber', None
    if (camp.get('category') or 'MARKETING') == 'MARKETING' and await db.campaign_sends.find_one(
            {'company_id': company['id'], 'contact_id': contact['id'], 'status': 'sent', 'category': 'MARKETING', 'campaign_id': {'$ne': camp['id']}, 'at': {'$gte': now() - PROMO_GAP}}):
        return 'skipped', 'recebeu outra promoção nos últimos 7 dias', None
    ctx = await context(company, contact)
    code = None
    if camp.get('coupon_promo_id'):
        code = await personal_coupon(company['id'], camp['coupon_promo_id'], contact['id'], f'Campanha · {camp["name"]}', camp.get('coupon_days') or 15)
        ctx['cupom'] = code
    msg = await send_template(company['id'], contact, t, ctx, f'Campanha · {camp["name"]}')
    return 'sent', code, msg.get('wa_msg_id')

async def counts(camp):
    sends = await db.campaign_sends.find({'campaign_id': camp['id']}, {'_id': 0}).to_list(10000)
    st = {}
    for s in sends: st[s['status']] = st.get(s['status'], 0) + 1
    sent = [s for s in sends if s['status'] == 'sent']
    ids = [s['contact_id'] for s in sent]
    cs = {x['id']: x for x in await db.contacts.find({'id': {'$in': ids}}, {'_id': 0, 'id': 1, 'last_in_at': 1, 'opt_out_at': 1}).to_list(len(ids) + 1)}
    def after(c, k, s):
        v = (c or {}).get(k)
        if not v or not s.get('at'): return False
        a = s['at'] if s['at'].tzinfo else s['at'].replace(tzinfo=timezone.utc)
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)) > a
    replied = sum(1 for s in sent if after(cs.get(s['contact_id']), 'last_in_at', s))
    opted = sum(1 for s in sent if after(cs.get(s['contact_id']), 'opt_out_at', s))
    mids = [s['wa_msg_id'] for s in sent if s.get('wa_msg_id')]
    delivered = await db.messages.count_documents({'wa_msg_id': {'$in': mids}, 'status': {'$in': ['delivered', 'read']}}) if mids else 0
    failed = st.get('error', 0) + (await db.messages.count_documents({'wa_msg_id': {'$in': mids}, 'status': 'failed'}) if mids else 0)
    return sends, st, sent, replied, opted, delivered, failed

async def brake(company, camp, why):
    """Pausa a campanha para proteger o número e avisa o dono do jeito que ajuda: o que houve e o que fazer."""
    _, st, sent, replied, opted, delivered, failed = await counts(camp)
    b = {'reason': why, 'text': REASONS[why], 'at': now(), 'sent': len(sent), 'replied': replied, 'opted': opted, 'failed': failed}
    await db.campaigns.update_one({'id': camp['id']}, {'$set': {'status': 'paused', 'paused_reason': 'pausada para proteger o seu número', 'brake': b}})
    log.info('freio da campanha %s: %s', camp['id'], why)
    try:
        from push import later, send_user
        from core import db as _db
        for u in await _db.users.find({'company_id': company['id'], 'role': {'$in': ['owner', 'manager']}, 'active': {'$ne': False}}, {'_id': 0, 'id': 1}).to_list(20):
            later(send_user(u['id'], 'Pausamos sua campanha para proteger o seu número', f'{camp["name"]}: {REASONS[why]} Veja o que fazer.', '/clientes/campanhas', 'camp-' + camp['id']))
    except Exception: log.exception('falha ao avisar o freio')

async def check_brakes(company, camp):
    _, st, sent, replied, opted, delivered, failed = await counts(camp)
    n = len(sent)
    if n < BRAKE_MIN: return None
    if opted / n >= BRAKE_OPTOUT: return 'saida'
    if failed / max(1, n + st.get('error', 0)) >= BRAKE_ERRORS: return 'erros'
    import metacost
    h = await metacost.health(company['id']) or {}
    if (camp.get('quality_start') or 'GREEN') == 'GREEN' and h.get('quality') in ('YELLOW', 'RED'): return 'qualidade'
    return None

async def tick(limit=PER_TICK):
    from automations import in_quiet
    import metacost
    sent = 0
    for camp in await db.campaigns.find({'status': 'sending'}, {'_id': 0}).to_list(200):
        company = await db.companies.find_one({'id': camp['company_id'], 'status': 'active'}, {'_id': 0})
        from billing import has
        if not company or in_quiet(company) or not has(company, 'campanhas'): continue
        ra = camp.get('resume_at')
        if ra and (ra if ra.tzinfo else ra.replace(tzinfo=timezone.utc)) > now(): continue
        t = await db.templates.find_one({'id': camp['template_id']}, {'_id': 0})
        if not t or t.get('status') != 'APPROVED':
            await brake(company, camp, 'modelo'); continue
        why = await check_brakes(company, camp)
        if why: await brake(company, camp, why); continue
        h = await metacost.health(company['id']) or {'left': limit}
        room = limit if h.get('left') is None else min(limit, h['left'])
        if room <= 0:  # limite diário da Meta: continua amanhã, sozinho
            if not camp.get('waiting_limit'): await db.campaigns.update_one({'id': camp['id']}, {'$set': {'waiting_limit': now()}})
            continue
        if camp.get('waiting_limit'): await db.campaigns.update_one({'id': camp['id']}, {'$unset': {'waiting_limit': ''}})
        batch = await db.campaign_sends.find({'campaign_id': camp['id'], 'status': 'queued'}, {'_id': 0}).limit(room).to_list(room)
        for s in batch:
            mid = None
            try: st, extra, mid = await send_one(company, camp, t, s)
            except HTTPException as e: st, extra = 'error', str(e.detail)[:200]
            except Exception as e: log.exception('falha no envio'); st, extra = 'error', type(e).__name__
            await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'status': st, 'at': now(), **({'wa_msg_id': mid} if mid else {}), **({'coupon': extra} if st == 'sent' else {'detail': extra})}})
            sent += 1
        if not await db.campaign_sends.find_one({'campaign_id': camp['id'], 'status': 'queued'}):
            await db.campaigns.update_one({'id': camp['id']}, {'$set': {'status': 'done', 'done_at': now()}})
        if sent >= limit: break
    return sent

@router.get('/campaigns')
async def list_campaigns(user=Depends(managers)):
    import metacost
    out = []
    company = user['company']
    for c in await db.campaigns.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(100):
        sends, st, sent, replied, opted, delivered, failed = await counts(c)
        bought = 0; revenue = 0.0
        for s in sent:
            sales = await db.sales.find({'company_id': user['company_id'], 'contact_id': s['contact_id'], 'status': 'ok', 'date': {'$gte': s['at'], '$lte': s['at'] + timedelta(days=30)}}, {'_id': 0, 'total': 1}).to_list(50)
            if sales: bought += 1; revenue += sum(x['total'] for x in sales)
        res = {'sent': len(sent), 'bought': bought, 'revenue': round(revenue, 2)}
        if c.get('result') != res: await db.campaigns.update_one({'id': c['id']}, {'$set': {'result': res}})
        est = round((delivered or len(sent)) * (c.get('per') or metacost.rate_of(await metacost.rates(), c.get('category'))), 2)
        mc = c.get('meta_cost')
        tried = c.get('meta_cost_tried_at')
        if tried and tried.tzinfo is None: tried = tried.replace(tzinfo=timezone.utc)
        done = c.get('done_at')
        if done and done.tzinfo is None: done = done.replace(tzinfo=timezone.utc)
        if not mc and done and now() - done > timedelta(days=1) and (not tried or now() - tried > timedelta(hours=6)):
            try: mc = await metacost.confirm_cost(company, c, delivered)
            except Exception as e: log.info('custo da Meta ainda indisponível: %s', getattr(e, 'detail', e)); mc = None
            await db.campaigns.update_one({'id': c['id']}, {'$set': {'meta_cost_tried_at': now(), **({'meta_cost': mc} if mc else {})}})
        out.append({**c, 'queued': st.get('queued', 0), 'sent': len(sent), 'errors': st.get('error', 0), 'skipped': st.get('skipped', 0),
                    'delivered': delivered, 'replied': replied, 'opted': opted, 'bought': bought, 'revenue': round(revenue, 2),
                    'cost_est': est, 'cost_real': (mc or {}).get('value')})
    return out

class ResumeIn(BaseModel):
    recent_days: Optional[int] = Field(default=None, ge=7, le=365)  # continuar só com quem conversou nesse período
    tomorrow: bool = True

@router.post('/campaigns/{cid}/resume')
async def resume(cid: str, body: ResumeIn, request: Request, user=Depends(managers)):
    c = await db.campaigns.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Campanha não encontrada.')
    if c['status'] != 'paused': raise HTTPException(400, 'Esta campanha não está pausada.')
    dropped = 0
    if body.recent_days:
        lim = now() - timedelta(days=body.recent_days)
        for s in await db.campaign_sends.find({'campaign_id': cid, 'status': 'queued'}, {'_id': 0, 'id': 1, 'contact_id': 1}).to_list(10000):
            ct = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0, 'last_in_at': 1}) or {}
            li = ct.get('last_in_at')
            if not li or (li if li.tzinfo else li.replace(tzinfo=timezone.utc)) < lim:
                await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'status': 'skipped', 'detail': f'fora dos últimos {body.recent_days} dias'}}); dropped += 1
    upd = {'status': 'sending', 'paused_reason': None, 'brake_cleared': c.get('brake')}
    if body.tomorrow:
        from agenda import tz_of
        from datetime import datetime, time as dtime
        tz = tz_of(user['company']); d = (now().astimezone(tz) + timedelta(days=1)).date()
        upd['resume_at'] = datetime.combine(d, dtime(9, 0), tz)
    await db.campaigns.update_one({'id': cid}, {'$set': upd, '$unset': {'brake': ''}})
    await audit(request, user, 'Retomou campanha', f'{c["name"]}' + (f' · só últimos {body.recent_days} dias ({dropped} fora)' if body.recent_days else ''))
    return {'ok': True, 'dropped': dropped}

@router.post('/campaigns/{cid}/stop')
async def stop(cid: str, request: Request, user=Depends(managers)):
    c = await db.campaigns.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Campanha não encontrada.')
    await db.campaign_sends.update_many({'campaign_id': cid, 'status': 'queued'}, {'$set': {'status': 'cancelled'}})
    await db.campaigns.update_one({'id': cid}, {'$set': {'status': 'stopped', 'done_at': now()}})
    await audit(request, user, 'Parou campanha', c['name'])
    return {'ok': True}
