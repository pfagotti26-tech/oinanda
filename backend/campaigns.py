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
import logging, re, io
from datetime import timedelta, timezone, datetime
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
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

GOALS = {'agendar': 'Agendar horários', 'vender': 'Vender uma oferta', 'voltar': 'Trazer de volta', 'avisar': 'Avisar algo'}

class AfterIn(BaseModel):
    """O que acontece depois do envio, por pessoa."""
    nudge_hours: Optional[int] = Field(default=2, ge=1, le=20)     # tocou em agendar/quero e não fechou: a Nanda chama de novo
    remind_days: Optional[int] = Field(default=None, ge=1, le=14)  # leu e não respondeu: lembrete curto (custa de novo)
    remind_text: str = Field(default='', max_length=400)
    resend_unread: bool = False                                     # desligado de fábrica: insistir com quem não lê derruba a nota

class CampIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    rules: List[Rule] = Field(default_factory=list, max_length=12)
    segment_id: Optional[str] = None
    template_id: Optional[str] = None       # modelo já aprovado; sem ele, o texto abaixo vira um modelo novo
    text: str = Field(default='', max_length=1000)
    buttons: List[str] = Field(default_factory=list, max_length=3)
    image_id: Optional[str] = None
    goal: Literal['agendar', 'vender', 'voltar', 'avisar'] = 'vender'
    service_id: Optional[str] = None
    pro_id: Optional[str] = None
    offer: str = Field(default='', max_length=200)
    stock: Optional[int] = Field(default=None, ge=1, le=100000)
    start_at: Optional[datetime] = None     # vazio = agora
    stop_when_full: bool = True
    after: AfterIn = Field(default_factory=AfterIn)
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
    company = user['company']
    if not await db.wa_numbers.find_one({'company_id': user['company_id']}): raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    kind = practice_of(company)
    if kind == 'advocacia' and body.goal != 'avisar': raise HTTPException(400, 'Na advocacia, campanha só pode avisar clientes (Provimento 205/2021 da OAB).')
    img = await db.campaign_images.find_one({'id': body.image_id, 'company_id': user['company_id']}, {'_id': 0}) if body.image_id else None
    if body.image_id and not img: raise HTTPException(404, 'Imagem não encontrada. Escolha de novo.')
    if body.template_id:
        t = await db.templates.find_one({'id': body.template_id, 'company_id': user['company_id']}, {'_id': 0})
        if not t: raise HTTPException(404, 'Modelo não encontrado.')
        chk = check_text(t.get('body') or '', kind)
    else:
        if len(body.text.strip()) < 10: raise HTTPException(400, 'Escreva a mensagem da campanha.')
        chk = check_text(body.text, kind)
        if chk['block']: raise HTTPException(400, chk['issues'][0]['reason'])
        from automations import create_template
        cat = 'UTILITY' if body.goal == 'avisar' and kind == 'advocacia' else 'MARKETING'
        t = await create_template(user['company_id'], body.name, body.text, cat, body.buttons or None, (img_bytes(img), 'image/jpeg') if img else None)
    if chk['block']: raise HTTPException(400, chk['issues'][0]['reason'])
    if t.get('header') == 'IMAGE' and not img: raise HTTPException(400, 'Este modelo tem imagem: escolha a imagem da campanha.')
    if 'cupom' in (t.get('params') or []) and not body.coupon_promo_id: raise HTTPException(400, 'O modelo usa {cupom}: escolha a promoção dos cupons.')
    if body.coupon_promo_id and not await db.promos.find_one({'id': body.coupon_promo_id, 'company_id': user['company_id']}): raise HTTPException(404, 'Promoção não encontrada.')
    rows = await audience(user, body.rules)
    if kind == 'advocacia': rows = [x for x in rows if x.get('sales_count') or x.get('consent')]  # só clientes e quem autorizou
    if not rows: raise HTTPException(400, 'Ninguém neste público pode receber mensagem.')
    if len(rows) > 5000: raise HTTPException(400, 'Público grande demais para uma campanha (máximo 5.000). Divida em partes.')
    import metacost
    h = await metacost.health(user['company_id'], fresh=True) or {}
    r = await metacost.rates()
    c = {'id': uid(), 'company_id': user['company_id'], 'name': body.name.strip(), 'rules': [r.model_dump() for r in body.rules], 'segment_id': body.segment_id,
         'template_id': t['id'], 'template': t['label'], 'coupon_promo_id': body.coupon_promo_id, 'coupon_days': body.coupon_days,
         'category': t.get('category') or 'MARKETING', 'per': metacost.rate_of(r, t.get('category')), 'quality_start': h.get('quality'),
         'status': 'sending', 'total': len(rows), 'by': user['name'], 'created_at': now(),
         'goal': body.goal, 'service_id': body.service_id, 'pro_id': body.pro_id, 'offer': body.offer.strip(), 'stock': body.stock,
         'image_id': img['id'] if img else None, 'start_at': body.start_at, 'stop_when_full': body.stop_when_full, 'after': body.after.model_dump(),
         'text': t.get('body') or body.text, 'buttons': t.get('buttons') or body.buttons, 'audience_label': await audience_label(user, body)}
    if body.goal == 'agendar' and body.service_id:
        from agenda import settings_for
        sv = next((x for x in (await settings_for(company))['services'] if x['id'] == body.service_id), None)
        if sv: c['service_name'] = sv['name']
    if t.get('status') != 'APPROVED': c['status'] = 'waiting_approval'
    elif body.start_at and aware_(body.start_at) > now(): c['status'] = 'scheduled'
    if body.after.remind_days:
        rt = body.after.remind_text.strip() or default_reminder(body.goal, c.get('service_name') or body.offer)
        try:
            from automations import create_template
            r = await create_template(user['company_id'], (body.name + ' lembrete')[:60], rt, 'MARKETING', [b for b in body.buttons if 'parar' not in b.lower()][:1] + ['Parar promoções'])
            c['remind_template_id'] = r['id']
        except HTTPException as e: log.info('lembrete da campanha não criado: %s', e.detail)
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
    msg = await send_template(company['id'], contact, t, ctx, f'Campanha · {camp["name"]}', header_media=await header_media(company, camp) if t.get('header') == 'IMAGE' else None)
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
    camp['_read'] = await db.messages.count_documents({'wa_msg_id': {'$in': mids}, 'status': 'read'}) if mids else 0
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
    await promote()
    try: await after_send()
    except Exception: log.exception('falha no depois do envio')
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
        if camp.get('goal') == 'agendar' and camp.get('stop_when_full') and camp.get('service_id') and await free_total(company, camp['service_id'], camp.get('pro_id'), 14) == 0:
            await db.campaign_sends.update_many({'campaign_id': camp['id'], 'status': 'queued'}, {'$set': {'status': 'cancelled', 'detail': 'vagas preenchidas'}})
            await db.campaigns.update_one({'id': camp['id']}, {'$set': {'status': 'full', 'done_at': now()}})
            continue
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
                    'read': c.get('_read', 0), 'booked': await booked_after(c, sent),
                    'cost_est': est, 'cost_real': (mc or {}).get('value'), 'cost_max': round(c['total'] * (c.get('per') or metacost.rate_of(await metacost.rates(), c.get('category'))), 2)})
    for x in out: x.pop('_read', None)
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


# ---------- campanhas guiadas: objetivo, imagem, agenda, regras de conselho, depois do envio ----------
def aware_(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)

def practice_of(company):
    """Tipo de prática que tem regra de conselho. Clínica precisa dizer se é estética, odonto, médica ou outra saúde."""
    p = company.get('practice')
    return p if p in PRACTICES else None

PRICE = re.compile(r'(r\$\s*\d|\d+\s*%|\bdesconto|\bpromo[çc][ãa]o|\bgr[áa]tis\b|\bgratuit|\bde gra[çc]a\b|\bbrinde|\bsorteio|\bpre[çc]o especial|\bparcel|\bsai por\b|\bpor apenas\b|\boff\b)', re.I)
PRACTICES = {'estetica': 'Estética', 'odonto': 'Odontologia', 'medica': 'Médica', 'saude': 'Outra área da saúde', 'advocacia': 'Advocacia'}

def check_text(text, kind):
    """Regras de conselho. Odonto: bloqueia preço, desconto e gratuidade (Código de Ética do CFO).
    Médica: avisa (CFM 2.336/2023 veda mercantilização). Advocacia: bloqueia oferta e preço (OAB 205/2021). Não substitui um jurídico."""
    hits = [m.group(0) for m in PRICE.finditer(text or '')]
    issues = []
    if hits and kind == 'odonto':
        issues.append({'terms': hits, 'level': 'block', 'reason': 'O Código de Ética Odontológica não permite divulgar preço, desconto ou gratuidade. Tire esse trecho para continuar.'})
    elif hits and kind == 'advocacia':
        issues.append({'terms': hits, 'level': 'block', 'reason': 'A OAB não permite mencionar honorários, desconto ou gratuidade para captar clientes (Provimento 205/2021). Tire esse trecho para continuar.'})
    elif hits and kind == 'medica':
        issues.append({'terms': hits, 'level': 'warn', 'reason': 'O CFM veda a mercantilização da medicina. Promoção e desconto podem dar problema: prefira convidar para a consulta.'})
    elif hits and kind == 'saude':
        issues.append({'terms': hits, 'level': 'warn', 'reason': 'Cada conselho de saúde tem regra sobre preço e promoção. Confira com o seu antes de enviar.'})
    return {'block': any(i['level'] == 'block' for i in issues), 'issues': issues}

def safe_rewrite(goal, service):
    s = (service or 'sua consulta').lower()
    if goal == 'agendar' or goal == 'voltar':
        return f'Oi, {{primeiro_nome}}! Faz {{tempo_sem_vir}} desde a sua última visita aqui no {{empresa}}. Quer que eu veja um horário para {s}?'
    return 'Oi, {primeiro_nome}! Passando para lembrar que estamos à disposição aqui no {empresa}. Quer que eu veja um horário para você?'

def default_reminder(goal, what):
    w = (what or '').strip()
    if goal == 'agendar': return f'Oi, {{primeiro_nome}}! Ainda dá tempo: separei horários{" para " + w.lower() if w else ""} nesta semana. Quer que eu reserve um?'
    if goal == 'vender': return f'Oi, {{primeiro_nome}}! Só lembrando{": " + w if w else " da oferta"}. Quer que eu separe para você?'
    return 'Oi, {primeiro_nome}! Só passando para saber se viu a mensagem que mandei. Posso ajudar?'

GOAL_TXT = {
    'agendar': 'Oi, {primeiro_nome}! Faz {tempo_sem_vir} desde a sua última visita aqui no {empresa}. {oferta}Quer que eu veja um horário para você?',
    'vender': 'Oi, {primeiro_nome}! Tem novidade aqui no {empresa}: {oferta}Quer que eu separe para você?',
    'voltar': 'Oi, {primeiro_nome}! Faz {tempo_sem_vir} que você não vem aqui no {empresa} e a gente sente sua falta. {oferta}Quer que eu veja um horário?',
    'avisar': 'Oi, {primeiro_nome}! Aviso importante do {empresa}: {oferta}Qualquer dúvida, é só responder esta mensagem.'}
BUTTONS = {'agendar': ['Quero agendar', 'Parar promoções'], 'vender': ['Quero', 'Parar promoções'], 'voltar': ['Quero agendar', 'Parar promoções'], 'avisar': ['Entendi', 'Tenho dúvida']}

class WriteIn(BaseModel):
    goal: Literal['agendar', 'vender', 'voltar', 'avisar'] = 'vender'
    service: str = Field(default='', max_length=80)
    offer: str = Field(default='', max_length=200)
    audience: str = Field(default='', max_length=120)
    variant: int = Field(default=0, ge=0, le=20)

@router.post('/campaigns/write')
async def write(body: WriteIn, user=Depends(managers)):
    """A Nanda escreve a mensagem a partir do objetivo. Sem a IA, um texto pronto por objetivo."""
    company = user['company']; kind = practice_of(company)
    import ai
    offer = body.offer.strip()
    if kind in ('odonto', 'advocacia') and PRICE.search(offer): offer = ''
    if ai.key_ok():
        rules = {'odonto': 'NÃO cite preço, desconto, gratuidade nem promoção (Código de Ética Odontológica).', 'advocacia': 'NÃO ofereça serviço, preço, desconto nem gratuidade: é só um aviso a clientes (OAB).',
                 'medica': 'Evite preço, desconto e promoção (CFM): convide para a consulta.'}.get(kind, '')
        sysm = ('Você escreve UMA mensagem curta de WhatsApp (máx. 320 caracteres) para uma campanha de um negócio brasileiro. Tom próximo e direto, '
                'no máximo 1 emoji, sem hashtags, sem CAIXA ALTA, sem promessas exageradas. Use as variáveis {primeiro_nome} (obrigatória, nunca no começo nem no fim) '
                'e, se fizer sentido, {tempo_sem_vir} e {empresa}. Termine com uma pergunta que convide a responder. ' + rules + ' Responda só com o texto.')
        prompt = f"Negócio: {company['name']} ({company.get('segment') or 'outro'}). Objetivo: {GOALS[body.goal]}. Serviço ou produto: {body.service or '-'}. Oferta: {offer or '-'}. Público: {body.audience or '-'}." + (f' Escreva uma versão diferente (nº {body.variant}).' if body.variant else '')
        try:
            txt, usage = await ai.llm(sysm, [{'role': 'user', 'content': prompt}], 400)
            await ai.track(user['company_id'], usage, 'campanha')
            txt = txt.strip().strip('"')
            if '{primeiro_nome}' in txt and 20 <= len(txt) <= 1000: return {'text': txt, 'buttons': BUTTONS[body.goal], 'by': 'nanda'}
        except HTTPException as e: log.info('IA indisponível para escrever campanha: %s', e.detail)
    of = (offer.rstrip('.') + '. ') if offer else ''
    if body.service and not offer and body.goal in ('agendar', 'voltar'): of = ''
    txt = GOAL_TXT[body.goal].replace('{oferta}', of)
    if kind == 'odonto' or kind == 'advocacia': txt = safe_rewrite(body.goal, body.service) if body.goal != 'avisar' else txt
    return {'text': txt, 'buttons': BUTTONS[body.goal], 'by': 'pronto'}

class CheckIn(BaseModel):
    text: str = Field(max_length=1000)
    goal: Literal['agendar', 'vender', 'voltar', 'avisar'] = 'vender'
    service: str = Field(default='', max_length=80)

@router.post('/campaigns/check')
async def check(body: CheckIn, user=Depends(managers)):
    kind = practice_of(user['company'])
    r = check_text(body.text, kind)
    if r['issues'] and r['issues'][0]['level'] == 'block': r['suggestion'] = safe_rewrite(body.goal, body.service)
    try:
        from automations import to_meta
        to_meta(body.text); r['vars_ok'] = True
    except HTTPException as e: r['vars_ok'] = False; r['vars_error'] = e.detail
    return {**r, 'practice': kind}

class PracticeIn(BaseModel):
    practice: Literal['estetica', 'odonto', 'medica', 'saude', 'advocacia', 'outro']

@router.put('/company/practice')
async def set_practice(body: PracticeIn, request: Request, user=Depends(managers)):
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'practice': body.practice}})
    await audit(request, user, 'Informou o tipo de negócio', PRACTICES.get(body.practice, 'Outro'))
    return {'ok': True}

# imagem: chega recortada pela tela (1125 x 590), a gente confere e guarda
IMG_W, IMG_H = 1125, 590

def img_dir():
    from media import media_dir
    d = media_dir() / 'campaigns'; d.mkdir(parents=True, exist_ok=True); return d

def img_bytes(img): return (img_dir() / f'{img["id"]}.jpg').read_bytes()

@router.post('/campaigns/image')
async def upload_image(file: UploadFile = File(...), x: float = Form(default=0), y: float = Form(default=0), w: float = Form(default=1), h: float = Form(default=1), user=Depends(managers)):
    """Recebe a foto e o recorte escolhido (frações de 0 a 1). Devolve a imagem pronta no tamanho do WhatsApp e os avisos."""
    data = await file.read()
    if len(data) > 15 * 1024 * 1024: raise HTTPException(400, 'Foto grande demais. Use uma de até 15 MB: a gente reduz para o limite da Meta.')
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    except Exception: raise HTTPException(400, 'Não consegui abrir essa imagem. Use JPG ou PNG.')
    W, H = im.size
    x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y)); w, h = max(0.05, min(1.0 - x, w)), max(0.05, min(1.0 - y, h))
    box = (round(x * W), round(y * H), round((x + w) * W), round((y + h) * H))
    crop = im.crop(box)
    cw, ch = crop.size
    out = crop.resize((IMG_W, IMG_H), Image.LANCZOS)
    buf = io.BytesIO(); q = 88
    out.save(buf, 'JPEG', quality=q, optimize=True)
    while buf.tell() > 5 * 1024 * 1024 and q > 50: q -= 10; buf = io.BytesIO(); out.save(buf, 'JPEG', quality=q, optimize=True)
    iid = uid()
    (img_dir() / f'{iid}.jpg').write_bytes(buf.getvalue())
    warn = []
    if cw < 700 or ch < 360: warn.append(f'Foto pequena ({W} × {H}). Pode aparecer borrada no celular. Se tiver uma versão maior, use ela.')
    doc = {'id': iid, 'company_id': user['company_id'], 'name': (file.filename or 'imagem')[:80], 'orig': [W, H], 'crop': [cw, ch], 'warn': warn, 'at': now()}
    await db.campaign_images.insert_one(doc.copy())
    doc.pop('_id', None)
    return {**doc, 'url': f'/api/campaigns/image/{iid}', 'size': [IMG_W, IMG_H]}

@router.get('/campaigns/image/{iid}')
async def get_image(iid: str, user=Depends(managers)):
    if not await db.campaign_images.find_one({'id': iid, 'company_id': user['company_id']}): raise HTTPException(404, 'Imagem não encontrada.')
    p = img_dir() / f'{iid}.jpg'
    if not p.exists(): raise HTTPException(404, 'Imagem não encontrada.')
    return FileResponse(p, media_type='image/jpeg')

async def header_media(company, camp):
    """Sobe a imagem da campanha para a Meta uma vez (vale 30 dias) e reaproveita em todos os envios."""
    hm = camp.get('header_media')
    at = aware_(camp.get('header_media_at'))
    if hm and at and now() - at < timedelta(days=25): return hm
    img = await db.campaign_images.find_one({'id': camp.get('image_id')}, {'_id': 0})
    if not img: raise HTTPException(400, 'A imagem da campanha não foi encontrada.')
    import media
    from whatsapp import decrypt
    n = await db.wa_numbers.find_one({'company_id': company['id']}, {'_id': 0})
    mid = await media.upload(n['phone_number_id'], decrypt(n['token_enc']), img_bytes(img), 'image/jpeg', 'campanha.jpg')
    await db.campaigns.update_one({'id': camp['id']}, {'$set': {'header_media': mid, 'header_media_at': now()}})
    camp['header_media'] = mid
    return mid

# agenda: vagas livres para o serviço da campanha
async def free_by_day(company, service_id, pro_id=None, days=14):
    from agenda import settings_for, free_slots, tz_of
    s = await settings_for(company)
    sv = next((x for x in s['services'] if x['id'] == service_id), None)
    if not sv: raise HTTPException(404, 'Serviço não encontrado na agenda.')
    tz = tz_of(company); today = now().astimezone(tz).date()
    free = await free_slots(company, sv, today, days, pro_id=pro_id)
    cap = max(1, int(sv.get('capacity') or 1))
    return sv, s, {d: len(v) * cap for d, v in sorted(free.items())}

async def free_total(company, service_id, pro_id=None, days=14):
    try: _, _, f = await free_by_day(company, service_id, pro_id, days)
    except HTTPException: return None
    return sum(f.values())

@router.get('/campaigns/capacity')
async def capacity(service_id: str, pro_id: Optional[str] = None, days: int = 14, user=Depends(managers)):
    from agenda import pros_for, WEEK
    sv, s, f = await free_by_day(user['company'], service_id, pro_id, max(1, min(30, days)))
    pros = [p['name'] for p in pros_for(s, service_id) if not pro_id or p['id'] == pro_id]
    from datetime import date as _d
    dd = lambda d: d if not isinstance(d, str) else _d.fromisoformat(d[:10])
    out = [{'date': dd(d).isoformat(), 'label': f'{WEEK[dd(d).weekday()]} {dd(d).day}', 'free': n} for d, n in f.items()]
    return {'service': {'id': sv['id'], 'name': sv['name'], 'duration': sv['duration'], 'price': sv.get('price'), 'capacity': sv.get('capacity') or 1},
            'days': out, 'total': sum(f.values()), 'pros': pros}

# públicos prontos por nicho (cada um vira as mesmas regras de "Base e públicos")
def presets_for(segment):
    base = [
        ('inativos', 'Clientes que sumiram', 'Compraram, mas faz tempo que não voltam', [{'field': 'kind', 'op': 'is', 'value': 'inativo'}]),
        ('clientes', 'Todos os clientes', 'Já compraram pelo menos uma vez', [{'field': 'kind', 'op': 'is', 'value': 'cliente'}]),
        ('prospects', 'Conversaram e ainda não compraram', 'Interesse que esfriou', [{'field': 'kind', 'op': 'is', 'value': 'prospect'}]),
        ('recentes', 'Compraram nos últimos 90 dias', 'Base quente', [{'field': 'last_purchase', 'op': 'less', 'value': 90}]),
    ]
    extra = {
        'salao': [('volta30', 'Sem vir há mais de 30 dias', 'Hora de voltar', [{'field': 'last_purchase', 'op': 'more', 'value': 30}])],
        'clinica': [('volta60', 'Sem vir há mais de 60 dias', 'Retorno ou manutenção', [{'field': 'last_purchase', 'op': 'more', 'value': 60}])],
        'studio': [('sumidos', 'Alunos sem vir há 30 dias', 'Antes de cancelar', [{'field': 'last_purchase', 'op': 'more', 'value': 30}])],
        'escola': [('sumidos', 'Alunos sem vir há 30 dias', 'Antes de cancelar', [{'field': 'last_purchase', 'op': 'more', 'value': 30}])],
        'loja': [('recompra', 'Compraram há mais de 45 dias', 'Recompra', [{'field': 'last_purchase', 'op': 'more', 'value': 45}])],
        'restaurante': [('volta15', 'Pediram há mais de 15 dias', 'Saudade', [{'field': 'last_purchase', 'op': 'more', 'value': 15}])],
    }.get(segment, [])
    return extra + base

@router.get('/campaigns/presets')
async def presets(user=Depends(managers)):
    from wallet import reachable
    out = []
    for key, label, sub, rules in presets_for(user['company'].get('segment')):
        rows = await run_segment(user, [Rule(**r) for r in rules])
        out.append({'key': key, 'label': label, 'sub': sub, 'rules': rules, 'count': len(rows), 'reach': sum(1 for c in rows if reachable(c))})
    segs = await db.segments.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1, 'rules': 1}).sort('created_at', -1).to_list(30)
    return {'presets': out, 'segments': segs, 'practice': practice_of(user['company']), 'segment': user['company'].get('segment')}

class ReachIn(BaseModel):
    rules: List[Rule] = Field(default_factory=list, max_length=12)

@router.post('/campaigns/reach')
async def reach(body: ReachIn, user=Depends(managers)):
    """Quantos recebem de verdade, e por que os outros ficam de fora."""
    from wallet import reachable, no_campaign
    rows = await run_segment(user, body.rules)
    if practice_of(user['company']) == 'advocacia': rows = [x for x in rows if x.get('sales_count') or x.get('consent')]
    ok = [c for c in rows if reachable(c)]
    recent = await recent_promo(user['company_id'], [c['id'] for c in ok])
    import metacost
    r = await metacost.rates(); per = metacost.rate_of(r, 'MARKETING')
    return {'total': len(rows), 'reach': len(ok) - len(recent), 'opt_out': sum(1 for c in rows if c.get('opt_out')), 'no_wa': sum(1 for c in rows if not c.get('wa_id')),
            'no_consent': sum(1 for c in rows if no_campaign(c)), 'recent_promo': len(recent), 'per': per, 'cost_max': round((len(ok) - len(recent)) * per, 2)}

async def booked_after(camp, sent):
    if camp.get('goal') not in ('agendar', 'voltar') or not sent: return 0
    n = 0
    for s in sent:
        if s.get('at') and await db.appointments.find_one({'company_id': camp['company_id'], 'contact_id': s['contact_id'], 'created_at': {'$gte': s['at']}}): n += 1
    return n

async def audience_label(user, body):
    if body.segment_id:
        sg = await db.segments.find_one({'id': body.segment_id, 'company_id': user['company_id']}, {'_id': 0, 'name': 1})
        if sg: return sg['name']
    return ''

async def promote():
    """Agendada chega na hora: começa. Aguardando a Meta: quando o modelo é aprovado, começa (ou fica agendada)."""
    for c in await db.campaigns.find({'status': {'$in': ['scheduled', 'waiting_approval']}}, {'_id': 0}).to_list(200):
        if c['status'] == 'waiting_approval':
            t = await db.templates.find_one({'id': c['template_id']}, {'_id': 0, 'status': 1})
            st = (t or {}).get('status')
            if st in ('REJECTED', 'DISABLED'):
                await db.campaigns.update_one({'id': c['id']}, {'$set': {'status': 'rejected', 'done_at': now()}}); continue
            if st != 'APPROVED': continue
            nxt = 'scheduled' if c.get('start_at') and aware_(c['start_at']) > now() else 'sending'
            await db.campaigns.update_one({'id': c['id']}, {'$set': {'status': nxt, 'approved_at': now()}}); continue
        if not c.get('start_at') or aware_(c['start_at']) <= now():
            await db.campaigns.update_one({'id': c['id']}, {'$set': {'status': 'sending', 'started_at': now()}})

NUDGE = re.compile(r'(quero|agendar|marcar|reserv|separ|sim|tenho interesse|pode)', re.I)

async def after_send():
    """Depois do envio, por pessoa: chama de novo quem pediu e não fechou (dentro das 24 h, sem custo de promoção)
    e manda o lembrete para quem leu e não respondeu (se ligado)."""
    import whatsapp
    recent = now() - timedelta(days=15)
    for camp in await db.campaigns.find({'status': {'$in': ['sending', 'done', 'full']}, 'created_at': {'$gte': recent}}, {'_id': 0}).to_list(100):
        after = camp.get('after') or {}
        company = await db.companies.find_one({'id': camp['company_id'], 'status': 'active'}, {'_id': 0})
        if not company: continue
        nh = after.get('nudge_hours')
        if nh and camp.get('goal') in ('agendar', 'voltar', 'vender'):
            for s in await db.campaign_sends.find({'campaign_id': camp['id'], 'status': 'sent', 'nudged': {'$exists': False}}, {'_id': 0}).to_list(2000):
                ct = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0})
                li = aware_((ct or {}).get('last_in_at'))
                if not ct or not li or li < aware_(s['at']) or ct.get('opt_out') or ct.get('needs_human'): continue
                if not (timedelta(hours=nh) <= now() - li < timedelta(hours=20)): continue
                lastin = await db.messages.find_one({'contact_id': ct['id'], 'dir': 'in', 'at': {'$gte': s['at']}}, {'_id': 0, 'text': 1}, sort=[('at', 1)])
                if not lastin or not NUDGE.search(lastin.get('text') or ''): continue
                if await db.appointments.find_one({'company_id': company['id'], 'contact_id': ct['id'], 'created_at': {'$gte': s['at']}}) or \
                   await db.sales.find_one({'company_id': company['id'], 'contact_id': ct['id'], 'date': {'$gte': s['at']}}):
                    await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'nudged': 'fechou'}}); continue
                if (ct.get('last_dir') == 'out') and aware_(ct.get('last_at')) and now() - aware_(ct['last_at']) < timedelta(hours=1): continue  # alguém acabou de falar com ele
                text = await nudge_text(company, camp, ct)
                try: await whatsapp.deliver(company['id'], ct, text, None, 'Nanda (IA)')
                except HTTPException as e: log.info('chamar de novo não saiu: %s', e.detail)
                await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'nudged': now()}})
        rd = after.get('remind_days'); rt = camp.get('remind_template_id')
        if rd and rt:
            t = await db.templates.find_one({'id': rt}, {'_id': 0})
            if not t or t.get('status') != 'APPROVED': continue
            cut = now() - timedelta(days=rd)
            for s in await db.campaign_sends.find({'campaign_id': camp['id'], 'status': 'sent', 'at': {'$lte': cut}, 'reminded': {'$exists': False}}, {'_id': 0}).limit(30).to_list(30):
                ct = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0})
                m = await db.messages.find_one({'wa_msg_id': s.get('wa_msg_id')}, {'_id': 0, 'status': 1}) if s.get('wa_msg_id') else None
                answered = ct and aware_(ct.get('last_in_at')) and aware_(ct['last_in_at']) > aware_(s['at'])
                if not ct or ct.get('opt_out') or answered or (m or {}).get('status') != 'read':
                    await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'reminded': 'nao'}}); continue
                try:
                    from automations import context, send_template
                    await send_template(company['id'], ct, t, await context(company, ct), f'Campanha · {camp["name"]} (lembrete)')
                    await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'reminded': now()}})
                except HTTPException as e:
                    await db.campaign_sends.update_one({'id': s['id']}, {'$set': {'reminded': 'erro', 'remind_detail': str(e.detail)[:200]}})

async def nudge_text(company, camp, ct):
    first = (ct.get('name') or '').split(' ')[0]
    if camp.get('goal') in ('agendar', 'voltar') and camp.get('service_id'):
        try:
            sv, _, _ = await free_by_day(company, camp['service_id'], camp.get('pro_id'), 7)
            from agenda import free_slots, tz_of, WEEK
            tz = tz_of(company); free = await free_slots(company, sv, now().astimezone(tz).date(), 7, pro_id=camp.get('pro_id'))
            opts = [t for d in sorted(free) for t in free[d]][:2]
            if len(opts) == 2:
                f = lambda t: f"{WEEK[t.weekday()]} {t:%d/%m} às {t:%H:%M}"
                return f'Oi{", " + first if first else ""}! Ainda quer agendar? Tenho {f(opts[0])} ou {f(opts[1])}. Qual fica melhor?'
        except Exception: log.exception('horários para chamar de novo')
        return f'Oi{", " + first if first else ""}! Ainda quer agendar? Me diz o melhor dia e horário que eu vejo aqui para você.'
    return f'Oi{", " + first if first else ""}! Ficou alguma dúvida? Se quiser, já separo para você.'

@router.get('/campaigns/{cid}')
async def detail(cid: str, user=Depends(managers)):
    c = await db.campaigns.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Campanha não encontrada.')
    rows = [x for x in await list_campaigns(user) if x['id'] == cid]
    base = rows[0] if rows else c
    sent = await db.campaign_sends.find({'campaign_id': cid, 'status': 'sent'}, {'_id': 0}).to_list(5000)
    people = []
    for s in sent:
        ct = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0, 'id': 1, 'name': 1, 'last_in_at': 1, 'needs_human': 1, 'opt_out_at': 1, 'last_text': 1, 'last_dir': 1, 'last_by': 1})
        if not ct or not aware_(ct.get('last_in_at')) or aware_(ct['last_in_at']) < aware_(s['at']): continue
        first_in = await db.messages.find_one({'contact_id': ct['id'], 'dir': 'in', 'at': {'$gte': s['at']}}, {'_id': 0, 'text': 1}, sort=[('at', 1)])
        ap = await db.appointments.find_one({'company_id': c['company_id'], 'contact_id': ct['id'], 'created_at': {'$gte': s['at']}}, {'_id': 0, 'start': 1})
        sale = await db.sales.find_one({'company_id': c['company_id'], 'contact_id': ct['id'], 'status': 'ok', 'date': {'$gte': s['at']}}, {'_id': 0, 'total': 1})
        oo = aware_(ct.get('opt_out_at'))
        if oo and oo >= aware_(s['at']): st = ('saiu', 'Saiu da lista')
        elif ap:
            from agenda import tz_of, WEEK
            t = aware_(ap['start']).astimezone(tz_of(user['company'])); st = ('agendou', f'Agendou {WEEK[t.weekday()]} {t:%H}h' + (f'{t:%M}' if t.minute else ''))
        elif sale: st = ('comprou', 'Comprou')
        elif ct.get('needs_human'): st = ('pessoa', 'Pediu uma pessoa')
        elif '(IA)' in (ct.get('last_by') or '') and ct.get('last_dir') == 'out': st = ('nanda', 'Nanda respondendo')
        else: st = ('respondeu', 'Respondeu')
        people.append({'id': ct['id'], 'name': ct['name'], 'text': (first_in or {}).get('text') or '', 'state': st[0], 'label': st[1]})
    unread_no_reply = 0
    for s in sent:
        m = await db.messages.find_one({'wa_msg_id': s.get('wa_msg_id')}, {'_id': 0, 'status': 1}) if s.get('wa_msg_id') else None
        if (m or {}).get('status') == 'read' and not any(p['id'] == s['contact_id'] for p in people): unread_no_reply += 1
    img = f'/api/campaigns/image/{c["image_id"]}' if c.get('image_id') else None
    return {**base, 'people': people[:200], 'read_no_reply': unread_no_reply, 'image_url': img, 'goal_label': GOALS.get(c.get('goal') or 'vender')}

@router.post('/campaigns/{cid}/test')
async def test_send(cid: str, user=Depends(managers)):
    """Manda a campanha para o WhatsApp de quem está criando, para ver como chega. Só depois que a Meta aprova o modelo."""
    c = await db.campaigns.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Campanha não encontrada.')
    t = await db.templates.find_one({'id': c['template_id']}, {'_id': 0})
    if not t or t.get('status') != 'APPROVED': raise HTTPException(400, 'O teste fica disponível quando a Meta aprovar a mensagem.')
    from channels import norm_phone
    phone = norm_phone(user.get('wa_phone') or user['company'].get('notice_phone') or user['company'].get('phone') or '')
    if not phone: raise HTTPException(400, 'Cadastre o seu WhatsApp no seu perfil para receber o teste.')
    me = await db.contacts.find_one({'company_id': user['company_id'], 'wa_id': phone}, {'_id': 0}) or {'id': uid(), 'company_id': user['company_id'], 'name': user['name'], 'wa_id': phone, 'internal': True, 'created_at': now()}
    if '_id' not in me and not await db.contacts.find_one({'id': me['id']}): await db.contacts.insert_one(me.copy())
    from automations import context, send_template
    await send_template(user['company_id'], me, t, await context(user['company'], me), 'Teste de campanha', header_media=await header_media(user['company'], c) if t.get('header') == 'IMAGE' else None)
    return {'ok': True, 'to': phone}

@router.post('/campaigns/{cid}/soldout')
async def soldout(cid: str, request: Request, user=Depends(managers)):
    """Acabou (estoque, cozinha, vagas): a Nanda passa a dizer que esgotou e a campanha para de enviar."""
    c = await db.campaigns.find_one({'id': cid, 'company_id': user['company_id']}, {'_id': 0})
    if not c: raise HTTPException(404, 'Campanha não encontrada.')
    await db.campaign_sends.update_many({'campaign_id': cid, 'status': 'queued'}, {'$set': {'status': 'cancelled', 'detail': 'esgotou'}})
    await db.campaigns.update_one({'id': cid}, {'$set': {'soldout_at': now(), **({'status': 'done', 'done_at': now()} if c['status'] in ('sending', 'scheduled', 'waiting_approval') else {})}})
    await audit(request, user, 'Marcou a campanha como esgotada', c['name'])
    return {'ok': True}

async def nanda_block(company, contact):
    """O que a Nanda precisa saber quando alguém responde a uma campanha dos últimos 3 dias."""
    if not contact or not contact.get('id'): return ''
    s = await db.campaign_sends.find_one({'contact_id': contact['id'], 'status': 'sent', 'at': {'$gte': now() - timedelta(days=3)}}, {'_id': 0}, sort=[('at', -1)])
    if not s: return ''
    c = await db.campaigns.find_one({'id': s['campaign_id']}, {'_id': 0})
    if not c: return ''
    from automations import render, context
    try: sent_txt = render(c.get('text') or '', await context(company, contact))
    except Exception: sent_txt = c.get('text') or ''
    goal = {'agendar': f"marcar um horário{(' de ' + c['service_name']) if c.get('service_name') else ''}. Ofereça 2 horários livres desse serviço e marque",
            'voltar': 'trazer a pessoa de volta e marcar um horário. Ofereça 2 horários livres',
            'vender': 'vender a oferta. Tire dúvidas, confirme o que a pessoa quer e separe/registre o pedido',
            'avisar': 'informar. Responda dúvidas sobre o aviso; não ofereça serviços'}.get(c.get('goal') or 'vender')
    out = f"\nCAMPANHA QUE ESTE CLIENTE RECEBEU HÁ POUCO: \"{sent_txt[:400]}\". Objetivo: {goal}."
    if c.get('offer'): out += f" Oferta: {c['offer']}."
    if c.get('stock'): out += f" Quantidade limitada: {c['stock']}."
    if c.get('soldout_at'): out += ' ESGOTOU: avise com gentileza que acabou e ofereça uma alternativa.'
    return out + '\n'
