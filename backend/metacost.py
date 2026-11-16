"""Custo da Meta e saúde do número, explicados para o dono do negócio.

A Meta cobra o WhatsApp oficial direto no cartão da conta Meta do cliente. A Oi Nanda não cobra por mensagem.
Aqui ficam:
- a tabela de referência (valores aproximados, com a data em que foram conferidos; o admin atualiza);
- a saúde do número (nota de qualidade e limite diário de pessoas novas, lidos da Meta com cache);
- o retorno esperado de uma campanha, pelo histórico da própria empresa;
- o custo confirmado pela Meta depois da campanha (pricing_analytics da conta WhatsApp).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, audit, current_user, admin

router = APIRouter()
log = logging.getLogger('oinanda.metacost')

# Referência da tabela da Meta para o Brasil (por mensagem entregue). Valores aproximados.
DEFAULT = {'MARKETING': 0.35, 'UTILITY': 0.04, 'AUTHENTICATION': 0.04, 'checked': '2026-11-16', 'usd_brl': 5.4,
           'source': 'https://developers.facebook.com/docs/whatsapp/pricing'}
STALE_DAYS = 90
TIERS = {'TIER_50': 50, 'TIER_250': 250, 'TIER_1K': 1000, 'TIER_2K': 2000, 'TIER_10K': 10000, 'TIER_100K': 100000, 'TIER_UNLIMITED': None}
NEXT = {250: (2000, 'verifique sua empresa na Meta'), 1000: (2000, 'sobe sozinho com bom uso'), 2000: (10000, 'sobe sozinho com bom uso'),
        10000: (100000, 'sobe sozinho com bom uso'), 100000: (None, 'sobe sozinho com bom uso')}
HEALTH_TTL = timedelta(minutes=15)


async def rates():
    doc = await db.platform.find_one({'id': 'meta_rates'}, {'_id': 0}) or {}
    r = {**DEFAULT, **{k: v for k, v in doc.items() if k in DEFAULT}}
    try: age = (now().date() - datetime.fromisoformat(r['checked']).date()).days
    except ValueError: age = 999
    r['stale'] = age > STALE_DAYS
    r['checked_label'] = datetime.fromisoformat(r['checked']).strftime('%m/%Y') if age < 999 else ''
    return r


def rate_of(r, category):
    return r.get(category or 'MARKETING') or r['MARKETING']


@router.get('/meta/rates')
async def get_rates(user=Depends(current_user)):
    r = await rates()
    if not user.get('is_admin'): r.pop('stale', None)
    return r


class RatesIn(BaseModel):
    MARKETING: float = Field(gt=0, lt=10)
    UTILITY: float = Field(gt=0, lt=10)
    AUTHENTICATION: Optional[float] = Field(default=None, gt=0, lt=10)
    usd_brl: Optional[float] = Field(default=None, gt=1, lt=20)


@router.put('/admin/meta-rates')
async def put_rates(body: RatesIn, request: Request, user=Depends(admin)):
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    upd.update(id='meta_rates', checked=now().date().isoformat())
    await db.platform.update_one({'id': 'meta_rates'}, {'$set': upd}, upsert=True)
    await audit(request, user, 'Atualizou a tabela de referência da Meta', ', '.join(f'{k}={v}' for k, v in upd.items() if k != 'id'))
    return await rates()


# ---------- saúde do número ----------
async def used_24h(company_id):
    """Pessoas diferentes que a empresa chamou primeiro (modelo) nas últimas 24 h: é isso que conta no limite da Meta."""
    ids = await db.messages.distinct('contact_id', {'company_id': company_id, 'dir': 'out', 'type': 'template', 'at': {'$gte': now() - timedelta(hours=24)}})
    return len(ids)


async def health(company_id, fresh=False):
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0})
    if not n: return None
    h = n.get('health') or {}
    at = h.get('at')
    if at and at.tzinfo is None: at = at.replace(tzinfo=timezone.utc)
    if fresh or not at or now() - at > HEALTH_TTL:
        try:
            import whatsapp
            info = await whatsapp.graph('GET', f'/{n["phone_number_id"]}', whatsapp.decrypt(n['token_enc']), params={'fields': 'quality_rating,messaging_limit_tier'})
            h = {'quality': info.get('quality_rating') or h.get('quality') or n.get('quality'), 'tier': info.get('messaging_limit_tier') or h.get('tier'), 'at': now()}
            await db.wa_numbers.update_one({'company_id': company_id}, {'$set': {'health': h, 'quality': h['quality']}})
        except Exception as e:
            log.info('não leu a saúde do número: %s', getattr(e, 'detail', e))
            h = {**h, 'quality': h.get('quality') or n.get('quality')}
    tier = h.get('tier')
    known = tier in TIERS
    limit = TIERS[tier] if known else 250  # sem informação da Meta, vai no limite inicial (mais seguro)
    used = await used_24h(company_id)
    q = (h.get('quality') or 'UNKNOWN').upper()
    nxt = NEXT.get(limit) if limit else None
    return {'quality': q, 'state': {'GREEN': 'ok', 'YELLOW': 'atencao', 'RED': 'risco'}.get(q, 'ok'), 'limit': limit, 'limit_known': known,
            'used': used, 'left': None if limit is None else max(0, limit - used), 'next': nxt[0] if nxt else None, 'next_how': nxt[1] if nxt else ''}


@router.get('/meta/health')
async def get_health(user=Depends(current_user)):
    h = await health(user['company_id'])
    if not h: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    return h


@router.post('/meta/intro-seen')
async def intro_seen(user=Depends(current_user)):
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'meta_intro_seen': now()}})
    return {'ok': True}


# ---------- retorno esperado ----------
async def expected(company_id, category):
    """Pelo histórico: de cada 100 que receberam campanhas parecidas, quantos compraram, e o ticket médio. None se ainda não há base."""
    sent = bought = 0; revenue = 0.0
    for c in await db.campaigns.find({'company_id': company_id, 'status': {'$in': ['done', 'stopped', 'paused']}, 'category': category or 'MARKETING'},
                                     {'_id': 0}).sort('created_at', -1).to_list(10):
        if not c.get('result'): continue
        sent += c['result'].get('sent', 0); bought += c['result'].get('bought', 0); revenue += c['result'].get('revenue', 0)
    if sent < 30 or not bought: return None
    return {'per100': round(100 * bought / sent, 1), 'ticket': round(revenue / bought, 2), 'base': sent}


# ---------- custo confirmado pela Meta ----------
async def confirm_cost(company, camp, delivered):
    """Custo real: mensagens entregues × o valor que a Meta efetivamente cobrou por mensagem dessa categoria nos dias da campanha."""
    n = await db.wa_numbers.find_one({'company_id': company['id']}, {'_id': 0})
    if not n or not delivered: return None
    import whatsapp
    start = camp['created_at']; end = camp.get('done_at') or now()
    if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    s0 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()); s1 = int((end + timedelta(days=1)).timestamp())
    field = f'pricing_analytics.start({s0}).end({s1}).granularity(DAILY).dimensions(["PRICING_CATEGORY"])'
    info = await whatsapp.graph('GET', f'/{n["waba_id"]}', whatsapp.decrypt(n['token_enc']), params={'fields': field + ',currency'})
    pts = []
    for d in ((info.get('pricing_analytics') or {}).get('data') or []): pts += d.get('data_points') or []
    cat = camp.get('category') or 'MARKETING'
    vol = sum(p.get('volume') or 0 for p in pts if (p.get('pricing_category') or '').upper() == cat)
    cost = sum(p.get('cost') or 0 for p in pts if (p.get('pricing_category') or '').upper() == cat)
    if not vol: return None
    per = cost / vol
    cur = (info.get('currency') or 'USD').upper()
    r = await rates()
    brl = per * (r['usd_brl'] if cur == 'USD' else 1)
    return {'value': round(delivered * brl, 2), 'per': round(brl, 4), 'currency': cur, 'at': now()}
