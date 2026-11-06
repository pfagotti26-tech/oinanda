"""Campanhas no WhatsApp para um público (segmento).

Fora da janela de 24 h, a Meta só aceita modelo aprovado, então a campanha usa um modelo. Pode gerar um cupom
individual para cada pessoa ({cupom}). Envia aos poucos (para não prejudicar o número), só das 8h às 21h,
e nunca para quem pediu para não receber. Resultado: entregues, responderam, compraram e quanto faturou.
"""
import logging
from datetime import timedelta
from typing import Optional, List
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from wallet import Rule, run_segment

router = APIRouter()
log = logging.getLogger('oinanda.camp')
PER_TICK = 30  # mensagens por minuto
COST = {'MARKETING': 0.36, 'UTILITY': 0.05}  # estimativa em R$ por mensagem no Brasil (a Meta cobra em dólar)

class CampIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    segment_id: Optional[str] = None
    template_id: str
    coupon_promo_id: Optional[str] = None
    coupon_days: int = Field(default=15, ge=1, le=180)

async def audience(user, rules):
    rows = await run_segment(user, rules)
    return [c for c in rows if not c.get('opt_out') and c.get('wa_id')]

class PreviewIn(BaseModel):
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    template_id: str

@router.post('/campaigns/preview')
async def preview(body: PreviewIn, user=Depends(managers)):
    t = await db.templates.find_one({'id': body.template_id, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Modelo não encontrado.')
    rows = await run_segment(user, body.rules)
    ok = [c for c in rows if not c.get('opt_out') and c.get('wa_id')]
    return {'total': len(rows), 'reachable': len(ok), 'opt_out': len(rows) - len(ok), 'approved': t.get('status') == 'APPROVED',
            'cost': round(len(ok) * COST.get(t.get('category'), COST['MARKETING']), 2), 'category': t.get('category'),
            'minutes': max(1, -(-len(ok) // PER_TICK)), 'needs_coupon': 'cupom' in (t.get('params') or [])}

@router.post('/campaigns')
async def create(body: CampIn, request: Request, user=Depends(managers)):
    t = await db.templates.find_one({'id': body.template_id, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Modelo não encontrado.')
    if t.get('status') != 'APPROVED': raise HTTPException(400, 'Esse modelo ainda não foi aprovado pela Meta.')
    if 'cupom' in (t.get('params') or []) and not body.coupon_promo_id: raise HTTPException(400, 'O modelo usa {cupom}: escolha a promoção dos cupons.')
    if body.coupon_promo_id and not await db.promos.find_one({'id': body.coupon_promo_id, 'company_id': user['company_id']}): raise HTTPException(404, 'Promoção não encontrada.')
    if not await db.wa_numbers.find_one({'company_id': user['company_id']}): raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    rows = await audience(user, body.rules)
    if not rows: raise HTTPException(400, 'Ninguém neste público pode receber mensagem.')
    if len(rows) > 5000: raise HTTPException(400, 'Público grande demais para uma campanha (máximo 5.000). Divida em partes.')
    c = {'id': uid(), 'company_id': user['company_id'], 'name': body.name.strip(), 'rules': [r.model_dump() for r in body.rules], 'segment_id': body.segment_id,
         'template_id': t['id'], 'template': t['label'], 'coupon_promo_id': body.coupon_promo_id, 'coupon_days': body.coupon_days,
         'status': 'sending', 'total': len(rows), 'by': user['name'], 'created_at': now()}
    await db.campaigns.insert_one(c.copy())
    await db.campaign_sends.insert_many([{'id': uid(), 'campaign_id': c['id'], 'company_id': user['company_id'], 'contact_id': x['id'], 'status': 'queued'} for x in rows])
    await audit(request, user, 'Criou campanha', f'{c["name"]} · {len(rows)} pessoas')
    c.pop('_id', None)
    return c

async def send_one(company, camp, t, s):
    from automations import context, send_template, personal_coupon, in_quiet
    contact = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0})
    if not contact or contact.get('opt_out'): return 'skipped', 'pediu para não receber'
    ctx = await context(company, contact)
    code = None
    if camp.get('coupon_promo_id'):
        code = await personal_coupon(company['id'], camp['coupon_promo_id'], contact['id'], f'Campanha · {camp["name"]}', camp.get('coupon_days') or 15)
        ctx['cupom'] = code
    await send_template(company['id'], contact, t, ctx, f'Campanha · {camp["name"]}')
    return 'sent', code

async def tick(limit=PER_TICK):
    from automations import in_quiet
    sent = 0
    for camp in await db.campaigns.find({'status': 'sending'}, {'_id': 0}).to_list(200):
        company = await db.companies.find_one({'id': camp['company_id'], 'status': 'active'}, {'_id': 0})
        if not company or in_quiet(company): continue
        t = await db.templates.find_one({'id': camp['template_id']}, {'_id': 0})
        if not t or t.get('status') != 'APPROVED':
            await db.campaigns.update_one({'id': camp['id']}, {'$set': {'status': 'paused', 'paused_reason': 'modelo não está aprovado'}}); continue
        batch = await db.campaign_sends.find({'campaign_id': camp['id'], 'status': 'queued'}, {'_id': 0}).to_list(limit)
        for s in batch:
            try: st, extra = await send_one(company, camp, t, s)
            except HTTPException as e: st, extra = 'error', str(e.detail)[:200]
            except Exception as e: log.exception('falha no envio'); st, extra = 'error', type(e).__name__
            await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'status': st, 'at': now(), **({'coupon': extra} if st == 'sent' else {'detail': extra})}})
            sent += 1
        if not await db.campaign_sends.find_one({'campaign_id': camp['id'], 'status': 'queued'}):
            await db.campaigns.update_one({'id': camp['id']}, {'$set': {'status': 'done', 'done_at': now()}})
        if sent >= limit: break
    return sent

@router.get('/campaigns')
async def list_campaigns(user=Depends(managers)):
    out = []
    for c in await db.campaigns.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(100):
        sends = await db.campaign_sends.find({'campaign_id': c['id']}, {'_id': 0}).to_list(10000)
        st = {}
        for s in sends: st[s['status']] = st.get(s['status'], 0) + 1
        sent = [s for s in sends if s['status'] == 'sent']
        replied = bought = 0; revenue = 0.0
        ids = [s['contact_id'] for s in sent]
        cs = {x['id']: x for x in await db.contacts.find({'id': {'$in': ids}}, {'_id': 0, 'id': 1, 'last_in_at': 1}).to_list(len(ids) + 1)}
        for s in sent:
            if cs.get(s['contact_id'], {}).get('last_in_at') and cs[s['contact_id']]['last_in_at'] > s['at']: replied += 1
            sales = await db.sales.find({'company_id': user['company_id'], 'contact_id': s['contact_id'], 'status': 'ok', 'date': {'$gte': s['at'], '$lte': s['at'] + timedelta(days=30)}}, {'_id': 0, 'total': 1}).to_list(50)
            if sales: bought += 1; revenue += sum(x['total'] for x in sales)
        out.append({**c, 'queued': st.get('queued', 0), 'sent': st.get('sent', 0), 'errors': st.get('error', 0), 'skipped': st.get('skipped', 0),
                    'replied': replied, 'bought': bought, 'revenue': round(revenue, 2)})
    return out

@router.post('/campaigns/{cid}/stop')
async def stop(cid: str, request: Request, user=Depends(managers)):
    c = await db.campaigns.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Campanha não encontrada.')
    await db.campaign_sends.update_many({'campaign_id': cid, 'status': 'queued'}, {'$set': {'status': 'cancelled'}})
    await db.campaigns.update_one({'id': cid}, {'$set': {'status': 'stopped', 'done_at': now()}})
    await audit(request, user, 'Parou campanha', c['name'])
    return {'ok': True}
