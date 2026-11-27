"""Automações por etapa e modelos de mensagem.

Automação = gatilho + mensagem (+ mudar de etapa, opcional). Gatilhos:
- stage_enter    X horas depois que o contato entrou na etapa
- no_reply       X horas sem o cliente responder (numa etapa, ou em qualquer etapa em andamento)
- before_appt    X horas antes de um horário marcado (lembrete)
- appt_no_show   X horas depois de um horário marcado como "Faltou"
- appt_done      X horas depois de um horário marcado como "Compareceu"
- before_expiry  X horas antes de um item da carteira vencer (renovação)
- after_expiry   X horas depois de vencer sem renovar (retomada)
- after_purchase X horas depois de uma compra (próxima oferta, pós-venda)
- checkout_pending   X horas depois de gerar boleto ou Pix na Hotmart, Kiwify ou Eduzz sem pagar
- checkout_abandoned X horas depois de abandonar o carrinho numa dessas plataformas
Pode gerar um cupom individual para cada envio ({cupom}).

Envio: se o cliente falou nas últimas 24 h, vai a mensagem normal. Fora disso a Meta só aceita modelo
aprovado; se a automação tiver um, ele é usado. Sem modelo, a vez é pulada e fica registrada.
Cada disparo tem uma chave única (automação + contato + momento), então nada sai em dobro.
Só envia entre 8h e 21h no fuso da empresa. Cliente que pede para parar não recebe mais nada automático.
"""
import os, re, asyncio, logging, secrets
from datetime import timedelta
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
import whatsapp
from whatsapp import get_contact, deliver, note, aware, WINDOW, decrypt
from niches import recipes_for
import agenda

log = logging.getLogger('oinanda.auto')
router = APIRouter()
TRIGGERS = {'stage_enter': 'Entrou na etapa', 'no_reply': 'Sem resposta do cliente', 'before_appt': 'Antes do horário marcado',
            'appt_no_show': 'Depois de uma falta', 'appt_done': 'Depois do atendimento', 'before_expiry': 'Antes de vencer',
            'after_expiry': 'Depois de vencer sem renovar', 'after_purchase': 'Depois de uma compra', 'after_milestone': 'Depois de atingir um marco da jornada',
            'checkout_pending': 'Boleto ou Pix sem pagar', 'checkout_abandoned': 'Carrinho abandonado'}
GRACE = timedelta(hours=24)  # depois do momento certo, ainda tenta por 24 h (ex.: servidor reiniciando, fora do horário)
QUIET = (8, 21)
VARS = {'primeiro_nome': 'Ana', 'nome': 'Ana Souza', 'empresa': 'Studio Forma', 'servico': 'Aula experimental', 'profissional': 'Léo', 'data': '12/11', 'hora': '09:00',
        'produto': 'Plano trimestral', 'vencimento': '20/11', 'cupom': 'VOLTA10', 'link_pagamento': 'https://pay.hotmart.com/X1', 'valor': 'R$ 997,00', 'tempo_sem_vir': '3 meses'}

# ---------- variáveis ----------
def since_txt(d):
    """Há quanto tempo, do jeito que se fala: "2 semanas", "3 meses", "1 ano"."""
    days = max(0, d.days)
    if days < 14: return f'{max(1, days)} dia' + ('s' if days != 1 else '')
    if days < 60: return f'{days // 7} semanas'
    if days < 365: return f'{days // 30} meses'
    y = days // 365
    return f'{y} ano' + ('s' if y > 1 else '')

def render(text, ctx):
    return re.sub(r'\{(\w+)\}', lambda m: str(ctx.get(m.group(1), m.group(0))), text or '')

async def context(company, contact, appt=None):
    tz = agenda.tz_of(company)
    if not appt:
        up = await agenda.upcoming_for(company['id'], contact['id'])
        appt = up[0] if up else None
    name = (contact.get('name') or '').strip()
    first = name.split(' ')[0] if name and not name.isdigit() else ''
    ctx = {'primeiro_nome': first or 'tudo bem', 'nome': name if first else '', 'empresa': company['name'], 'servico': '', 'profissional': '', 'data': '', 'hora': '', 'produto': '', 'vencimento': '', 'cupom': '', 'link_pagamento': '', 'valor': '', 'tempo_sem_vir': ''}
    hs = [x for x in contact.get('holdings') or [] if x.get('expires_at')]
    if hs:  # o item que vence primeiro (ou o que venceu por último)
        fut = [x for x in hs if aware(x['expires_at']) >= now()]
        x = min(fut, key=lambda y: aware(y['expires_at'])) if fut else max(hs, key=lambda y: aware(y['expires_at']))
        ctx.update(produto=x['name'], vencimento=f"{aware(x['expires_at']).astimezone(tz):%d/%m}")
    last = aware(contact.get('last_sale_at'))
    try:
        la = await db.appointments.find_one({'company_id': company['id'], 'contact_id': contact['id'], 'status': {'$in': ['done', 'confirmed', 'scheduled']}, 'start': {'$lt': now()}}, {'_id': 0, 'start': 1}, sort=[('start', -1)])
        if la and (not last or aware(la['start']) > last): last = aware(la['start'])
    except Exception: pass
    if last: ctx['tempo_sem_vir'] = since_txt(now() - last)
    if appt and appt.get('_extra'): ctx.update(appt['_extra']); appt = None
    if appt:
        t = aware(appt['start']).astimezone(tz)
        ctx.update(servico=appt.get('service_name') or '', profissional=appt.get('pro_name') or '', data=f'{agenda.WEEK[t.weekday()]} {t:%d/%m}', hora=f'{t:%H:%M}')
    return ctx

# ---------- modelos (templates da Meta) ----------
def to_meta(body):
    """'Oi, {primeiro_nome}!' -> ('Oi, {{1}}!', ['primeiro_nome'])"""
    params = []
    def sub(m):
        v = m.group(1)
        if v not in VARS: raise HTTPException(400, f'A variável {{{v}}} não existe. Use: ' + ', '.join('{' + k + '}' for k in VARS))
        params.append(v); return '{{' + str(len(params)) + '}}'
    text = re.sub(r'\{(\w+)\}', sub, body.strip())
    if re.match(r'^\{\{\d+\}\}', text) or re.search(r'\{\{\d+\}\}[\s.!?]*$', text):
        raise HTTPException(400, 'A Meta não aceita modelo começando ou terminando com variável. Ex.: "Oi, {primeiro_nome}!" funciona; "{primeiro_nome}, oi" não.')
    return text, params

def from_meta(text):
    return re.sub(r'\{\{(\d+)\}\}', r'{\1}', text or '')

async def number_of(company_id):
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0})
    if not n: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    return n

def tpl_out(t):
    t = {k: v for k, v in t.items() if k != '_id'}
    t['label_status'] = {'APPROVED': 'aprovado', 'PENDING': 'em análise na Meta', 'REJECTED': 'recusado', 'PAUSED': 'pausado', 'DISABLED': 'desativado'}.get(t.get('status'), (t.get('status') or '').lower())
    return t

async def sync_templates(company_id):
    n = await number_of(company_id)
    data = await whatsapp.graph('GET', f'/{n["waba_id"]}/message_templates', decrypt(n['token_enc']),
                                params={'fields': 'name,status,language,category,components,rejected_reason', 'limit': 200})
    seen = []
    for t in data.get('data') or []:
        body = next((c.get('text') for c in t.get('components') or [] if c.get('type') == 'BODY'), '') or ''
        local = await db.templates.find_one({'company_id': company_id, 'name': t['name'], 'language': t.get('language')}, {'_id': 0})
        upd = {'status': t.get('status'), 'category': t.get('category'), 'meta_id': t.get('id'), 'rejected_reason': t.get('rejected_reason'), 'synced_at': now()}
        if local: await db.templates.update_one({'id': local['id']}, {'$set': upd})
        else:
            n_params = len(re.findall(r'\{\{\d+\}\}', body))
            await db.templates.insert_one({'id': uid(), 'company_id': company_id, 'name': t['name'], 'language': t.get('language'), 'label': t['name'].replace('_', ' '),
                                           'body': from_meta(body), 'params': [str(i + 1) for i in range(n_params)], 'external': True, 'created_at': now(), **upd})
        seen.append(t['name'])
    return seen

@router.get('/templates')
async def templates(sync: bool = False, user=Depends(current_user)):
    if sync:
        try: await sync_templates(user['company_id'])
        except HTTPException as e:
            if 'Conecte' not in str(e.detail): raise
    rows = await db.templates.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(300)
    return [tpl_out(t) for t in rows]

class TemplateIn(BaseModel):
    label: str = Field(min_length=3, max_length=60)
    body: str = Field(min_length=10, max_length=1000)
    category: Literal['UTILITY', 'MARKETING'] = 'MARKETING'

OPT_OUT_FOOTER = 'Responda SAIR para não receber mais promoções'

async def create_template(company_id, label, body, category, quick=None, image=None):
    """image: (bytes, mime) para a imagem no topo da mensagem. A Meta aprova com essa imagem de exemplo; no envio, cada campanha manda a sua."""
    text, params = to_meta(body)
    n = await number_of(company_id)
    slug = re.sub(r'[^a-z0-9]+', '_', label.lower().encode('ascii', 'ignore').decode()).strip('_')[:40] or 'modelo'
    name = f'{slug}_{secrets.token_hex(2)}'
    comp = {'type': 'BODY', 'text': text}
    if params: comp['example'] = {'body_text': [[VARS[p] for p in params]]}
    comps = [comp]
    if image:
        import media
        handle = await media.meta_upload_handle(decrypt(n['token_enc']), image[0], image[1])
        comps.insert(0, {'type': 'HEADER', 'format': 'IMAGE', 'example': {'header_handle': [handle]}})
    stop_btn = any('parar' in (q or '').lower() for q in quick or [])
    footer = OPT_OUT_FOOTER if category == 'MARKETING' and not stop_btn else None  # promoção sempre ensina a sair: menos bloqueio, número mais saudável
    if footer: comps.append({'type': 'FOOTER', 'text': footer})
    if quick: comps.append({'type': 'BUTTONS', 'buttons': [{'type': 'QUICK_REPLY', 'text': q[:25]} for q in quick[:3]]})
    res = await whatsapp.graph('POST', f'/{n["waba_id"]}/message_templates', decrypt(n['token_enc']),
                               {'name': name, 'language': 'pt_BR', 'category': category, 'components': comps})
    t = {'id': uid(), 'company_id': company_id, 'name': name, 'language': 'pt_BR', 'label': label.strip(), 'body': body.strip(), 'params': params, 'footer': footer, 'header': 'IMAGE' if image else None, 'buttons': list(quick or [])[:3],
         'category': res.get('category') or category, 'status': res.get('status') or 'PENDING', 'meta_id': res.get('id'), 'created_at': now()}
    await db.templates.insert_one(t.copy())
    return tpl_out(t)

@router.post('/templates')
async def new_template(body: TemplateIn, request: Request, user=Depends(managers)):
    t = await create_template(user['company_id'], body.label, body.body, body.category)
    await audit(request, user, 'Enviou modelo para aprovação da Meta', t['label'])
    return t

@router.delete('/templates/{tid}')
async def del_template(tid: str, request: Request, user=Depends(managers)):
    t = await db.templates.find_one({'id': tid, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Modelo não encontrado.')
    try:
        n = await number_of(user['company_id'])
        await whatsapp.graph('DELETE', f'/{n["waba_id"]}/message_templates', decrypt(n['token_enc']), params={'name': t['name']})
    except HTTPException as e: log.warning('não apagou o modelo na Meta: %s', e.detail)
    await db.templates.delete_one({'id': tid})
    await db.automations.update_many({'company_id': user['company_id'], 'template_id': tid}, {'$set': {'template_id': None}})
    await audit(request, user, 'Apagou modelo', t['label'])
    return {'ok': True}

async def send_template(company_id, contact, t, ctx, by_name, by_id=None, values=None, header_media=None):
    n = await number_of(company_id)
    vals = values or [str(ctx.get(p) or '-') for p in t.get('params') or []]
    if not contact.get('wa_id'): raise HTTPException(400, 'Este contato não tem WhatsApp.')
    payload = {'messaging_product': 'whatsapp', 'to': contact['wa_id'], 'type': 'template',
               'template': {'name': t['name'], 'language': {'code': t.get('language') or 'pt_BR'}}}
    comps = []
    if t.get('header') == 'IMAGE':
        if not header_media: raise HTTPException(400, 'Este modelo tem imagem: escolha a imagem da campanha.')
        comps.append({'type': 'header', 'parameters': [{'type': 'image', 'image': {'id': header_media}}]})
    if vals: comps.append({'type': 'body', 'parameters': [{'type': 'text', 'text': v} for v in vals]})
    if comps: payload['template']['components'] = comps
    res = await whatsapp.graph('POST', f'/{n["phone_number_id"]}/messages', decrypt(n['token_enc']), payload)
    body = t.get('body') or ''
    text = render(body, ctx) if not t.get('external') else re.sub(r'\{(\d+)\}', lambda m: vals[int(m.group(1)) - 1] if int(m.group(1)) <= len(vals) else m.group(0), body)
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': contact['id'], 'dir': 'out', 'type': 'template', 'text': text, 'template': t['name'],
           'wa_msg_id': ((res.get('messages') or [{}])[0]).get('id'), 'status': 'sent', 'by_id': by_id, 'by': by_name, 'at': now()}
    await db.messages.insert_one(msg.copy())
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'last_at': msg['at'], 'last_text': text[:200], 'last_dir': 'out'}})
    msg.pop('_id', None)
    return msg

class SendTemplateIn(BaseModel):
    template_id: str
    values: Optional[List[str]] = Field(default=None, max_length=10)

@router.post('/conversations/{contact_id}/template')
async def send_tpl(contact_id: str, body: SendTemplateIn, user=Depends(current_user)):
    """O time chama de volta quem está fora da janela de 24 h (ou um contato novo) com um modelo aprovado."""
    c = await get_contact(user, contact_id)
    if c.get('opt_out'): raise HTTPException(400, 'Este cliente pediu para não receber mensagens. Espere ele chamar.')
    t = await db.templates.find_one({'id': body.template_id, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Modelo não encontrado.')
    if t.get('status') != 'APPROVED': raise HTTPException(400, 'Esse modelo ainda não foi aprovado pela Meta.')
    ctx = await context(user['company'], c)
    msg = await send_template(user['company_id'], c, t, ctx, user.get('impersonated_by') or user['name'], user['id'], body.values)
    if not c.get('assigned_to') and user['role'] == 'agent': await db.contacts.update_one({'id': c['id']}, {'$set': {'assigned_to': user['id']}})
    return msg

# ---------- automações ----------
def stage_by_hint(stages, hint):
    if not hint: return None
    h = hint.lower()
    s = next((x for x in stages if h in x['name'].lower()), None)
    return s['id'] if s else None

class AutoIn(BaseModel):
    recipe: Optional[str] = Field(default=None, max_length=40)
    name: Optional[str] = Field(default=None, min_length=2, max_length=60)
    trigger: Optional[Literal['stage_enter', 'no_reply', 'before_appt', 'appt_no_show', 'appt_done', 'before_expiry', 'after_expiry', 'after_purchase', 'after_milestone', 'checkout_pending', 'checkout_abandoned']] = None
    milestone_id: Optional[str] = Field(default=None, max_length=12)
    unless_id: Optional[str] = Field(default=None, max_length=12)
    product_id: Optional[str] = None
    coupon_promo_id: Optional[str] = None
    hours: Optional[float] = Field(default=None, ge=0, le=24 * 120)
    stage_id: Optional[str] = None
    message: Optional[str] = Field(default=None, min_length=5, max_length=1000)
    template_id: Optional[str] = None
    move_to: Optional[str] = None
    active: Optional[bool] = None

async def check_refs(user, a):
    from funnel import stages_for
    ids = {s['id'] for s in await stages_for(user['company'])}
    for k in ('stage_id', 'move_to'):
        if a.get(k) and a[k] not in ids: raise HTTPException(400, 'Etapa não existe.')
    if a.get('template_id') and not await db.templates.find_one({'id': a['template_id'], 'company_id': user['company_id']}):
        raise HTTPException(400, 'Modelo não encontrado.')
    if a.get('trigger') == 'stage_enter' and not a.get('stage_id'): raise HTTPException(400, 'Escolha a etapa.')
    if a.get('trigger') == 'after_milestone':
        from journey import active_ms
        ids = {m['id'] for m in active_ms(user['company'])}
        if a.get('milestone_id') not in ids: raise HTTPException(400, 'Escolha o marco.')
        if a.get('unless_id') and a['unless_id'] not in ids: raise HTTPException(400, 'Marco não encontrado.')
    if a.get('product_id') and not await db.products.find_one({'id': a['product_id'], 'company_id': user['company_id']}): raise HTTPException(400, 'Item não encontrado.')
    if a.get('coupon_promo_id') and not await db.promos.find_one({'id': a['coupon_promo_id'], 'company_id': user['company_id']}): raise HTTPException(400, 'Promoção não encontrada.')

def auto_out(a, stages=None):
    a = {k: v for k, v in a.items() if k != '_id'}
    a['trigger_label'] = TRIGGERS.get(a.get('trigger'))
    return a

@router.get('/automations')
async def list_autos(user=Depends(managers)):
    from funnel import stages_for
    stages = await stages_for(user['company'])
    items = await db.automations.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', 1).to_list(200)
    used = {a.get('recipe') for a in items}
    recipes = []
    for r in recipes_for(user['company'].get('segment')):
        recipes.append({**r, 'stage_id': stage_by_hint(stages, r['stage']), 'added': r['key'] in used})
    has_wa = bool(await db.wa_numbers.find_one({'company_id': user['company_id']}, {'_id': 1}))
    cid = user['company_id']
    uses_agenda = bool(await db.appointments.find_one({'company_id': cid}, {'_id': 1}))
    has_validity = bool(await db.products.find_one({'company_id': cid, 'duration_days': {'$gt': 0}}, {'_id': 1}))
    # pacote recomendado: depende de usar horário marcado e de ter item com validade
    pack = ['lembrete_24h', 'faltou', 'esfriou_1'] if uses_agenda else ['esfriou_1', 'esfriou_3'] + (['renova_7d', 'volta_7d'] if has_validity else ['proximo_15d'])
    pack = [k for k in pack if any(r['key'] == k and not r['added'] for r in recipes)]
    # resultado de cada automação: quem respondeu em até 3 dias e quem comprou em até 7 dias depois do envio
    runs = await db.automation_runs.find({'company_id': cid, 'status': 'sent', 'at': {'$gte': now() - timedelta(days=90)}}, {'_id': 0, 'automation_id': 1, 'contact_id': 1, 'at': 1}).to_list(20000)
    stats = {}
    if runs:
        ids = list({r['contact_id'] for r in runs})
        ins = {}
        for m in await db.messages.find({'company_id': cid, 'contact_id': {'$in': ids}, 'dir': 'in', 'at': {'$gte': now() - timedelta(days=95)}}, {'_id': 0, 'contact_id': 1, 'at': 1}).to_list(100000):
            ins.setdefault(m['contact_id'], []).append(aware(m['at']))
        buys = {}
        for x in await db.sales.find({'company_id': cid, 'contact_id': {'$in': ids}, 'status': 'ok'}, {'_id': 0, 'contact_id': 1, 'date': 1, 'total': 1}).to_list(20000):
            buys.setdefault(x['contact_id'], []).append((aware(x['date']), x['total']))
        for r in runs:
            st = stats.setdefault(r['automation_id'], {'replied': 0, 'bought': 0, 'revenue': 0.0})
            t0 = aware(r['at'])
            if any(t0 < t <= t0 + timedelta(days=3) for t in ins.get(r['contact_id'], [])): st['replied'] += 1
            b = [v for t, v in buys.get(r['contact_id'], []) if t and t0 <= t <= t0 + timedelta(days=7)]
            if b: st['bought'] += 1; st['revenue'] += sum(b)
    out = []
    for a in items:
        o = auto_out(a); o.update(stats.get(a['id'], {'replied': 0, 'bought': 0, 'revenue': 0.0})); out.append(o)
    return {'items': out, 'recipes': recipes, 'stages': stages, 'triggers': TRIGGERS, 'vars': list(VARS), 'whatsapp': has_wa,
            'uses_agenda': uses_agenda, 'has_validity': has_validity, 'pack': pack}

@router.post('/automations/preview')
async def preview_auto(body: AutoIn, user=Depends(managers)):
    """Quantas pessoas estão no momento de receber agora (o que sairia na próxima rodada)."""
    from funnel import stages_for
    if not body.trigger or body.hours is None: return {'count': 0, 'names': []}
    stages = await stages_for(user['company'])
    a = {'id': 'preview', 'trigger': body.trigger, 'hours': body.hours, 'stage_id': body.stage_id, 'product_id': body.product_id,
         'milestone_id': body.milestone_id, 'unless_id': body.unless_id}
    try: rows = await candidates(a, user['company'], stages)
    except Exception: rows = []
    from channels import view, window_of
    seen, names, ok, tpl, no = set(), [], 0, 0, 0
    for _, c, _ in rows:
        if c['id'] in seen: continue
        seen.add(c['id']); names.append((c.get('name') or '').split(' ')[0])
        v = view(c); ch = v.get('channel') or 'whatsapp'; w = window_of(ch); li = aware(v.get('last_in_at'))
        if not w or (li and now() - li < w): ok += 1  # dentro das 24 h: mensagem normal
        elif ch == 'whatsapp': tpl += 1  # precisa do modelo aprovado
        else: no += 1  # Instagram/Messenger fora do prazo: não dá para enviar
    return {'count': len(seen), 'names': names[:3], 'now': ok, 'needs_template': tpl, 'blocked': no}

@router.post('/automations')
async def create_auto(body: AutoIn, request: Request, user=Depends(managers)):
    from funnel import stages_for
    a = {'id': uid(), 'company_id': user['company_id'], 'created_at': now(), 'sent': 0, 'skipped': 0}
    if body.recipe:
        r = next((x for x in recipes_for(user['company'].get('segment')) if x['key'] == body.recipe), None)
        if not r: raise HTTPException(404, 'Sugestão não encontrada.')
        if await db.automations.find_one({'company_id': user['company_id'], 'recipe': r['key']}): raise HTTPException(409, 'Essa automação já foi adicionada.')
        stages = await stages_for(user['company'])
        sid = stage_by_hint(stages, r['stage'])
        a.update(recipe=r['key'], name=r['name'], trigger=r['trigger'], hours=r['hours'], stage_id=sid, message=r['message'], category=r['category'],
                 template_id=None, move_to=None, active=not (r['stage'] and not sid), only_unconfirmed=bool(r.get('only_unconfirmed')))
    else:
        if not (body.name and body.trigger and body.message and body.hours is not None): raise HTTPException(400, 'Preencha nome, gatilho, tempo e mensagem.')
        a.update(name=body.name.strip(), trigger=body.trigger, hours=body.hours, stage_id=body.stage_id, message=body.message.strip(),
                 template_id=body.template_id, move_to=body.move_to, active=True if body.active is None else body.active, category='MARKETING',
                 product_id=body.product_id, coupon_promo_id=body.coupon_promo_id, milestone_id=body.milestone_id, unless_id=body.unless_id)
    await check_refs(user, a)
    await db.automations.insert_one(a.copy())
    await audit(request, user, 'Criou automação', a['name'])
    return auto_out(a)

@router.patch('/automations/{aid}')
async def patch_auto(aid: str, body: AutoIn, request: Request, user=Depends(managers)):
    a = await db.automations.find_one({'id': aid, 'company_id': user['company_id']}, {'_id': 0})
    if not a: raise HTTPException(404, 'Automação não encontrada.')
    upd = body.model_dump(exclude_unset=True); upd.pop('recipe', None)
    for k in ('stage_id', 'move_to', 'template_id', 'product_id', 'coupon_promo_id', 'milestone_id', 'unless_id'):
        if k in upd and not upd[k]: upd[k] = None
    a.update(upd)
    await check_refs(user, a)
    await db.automations.update_one({'id': aid}, {'$set': upd})
    await audit(request, user, ('Ligou' if upd.get('active') else 'Desligou' if upd.get('active') is False else 'Editou') + ' automação', a['name'])
    return auto_out(a)

@router.delete('/automations/{aid}')
async def del_auto(aid: str, request: Request, user=Depends(managers)):
    a = await db.automations.find_one({'id': aid, 'company_id': user['company_id']}, {'_id': 0})
    if not a: raise HTTPException(404, 'Automação não encontrada.')
    await db.automations.delete_one({'id': aid})
    await audit(request, user, 'Apagou automação', a['name'])
    return {'ok': True}

@router.post('/automations/{aid}/template')
async def auto_template(aid: str, request: Request, user=Depends(managers)):
    """Manda a mensagem da automação para aprovação da Meta e liga o modelo a ela."""
    a = await db.automations.find_one({'id': aid, 'company_id': user['company_id']}, {'_id': 0})
    if not a: raise HTTPException(404, 'Automação não encontrada.')
    quick = None
    if a.get('trigger') == 'before_appt':  # lembrete: botões "Confirmo" e "Preciso remarcar" também quando sai por modelo
        import wachoice
        quick = [wachoice.CONFIRM_BTN, wachoice.RESCHED_BTN]
    t = await create_template(user['company_id'], a['name'], a['message'], a.get('category') or 'MARKETING', quick)
    await db.automations.update_one({'id': aid}, {'$set': {'template_id': t['id']}})
    await audit(request, user, 'Enviou modelo para aprovação da Meta', t['label'])
    return t

@router.get('/automations/runs')
async def runs(limit: int = 60, user=Depends(managers)):
    rows = await db.automation_runs.find({'company_id': user['company_id']}, {'_id': 0}).sort('at', -1).to_list(min(limit, 1000))
    ids = list({r['contact_id'] for r in rows})
    names = {c['id']: c['name'] for c in await db.contacts.find({'id': {'$in': ids}}, {'_id': 0, 'id': 1, 'name': 1}).to_list(2000)}
    for r in rows: r['contact_name'] = names.get(r['contact_id'])
    return rows

# ---------- execução ----------
def in_quiet(company):
    h = now().astimezone(agenda.tz_of(company)).hour
    return not (QUIET[0] <= h < QUIET[1])

async def candidates(a, company, stages):
    """Devolve [(chave, contato, agendamento)] que já passaram do momento e ainda estão no prazo."""
    t, h, cid = a['trigger'], timedelta(hours=a['hours'] or 0), company['id']
    cut, oldest = now() - h, now() - h - GRACE
    out = []
    if t in ('stage_enter', 'no_reply'):
        q = {'company_id': cid, 'opt_out': {'$ne': True}}
        if t == 'stage_enter':
            q.update(stage=a['stage_id'], stage_at={'$lte': cut, '$gt': oldest})
        else:
            open_ids = [s['id'] for s in stages if s['kind'] == 'open']
            if a.get('stage_id'): q['stage'] = a['stage_id'] if a['stage_id'] != stages[0]['id'] else {'$in': [a['stage_id'], None]}
            else: q['stage'] = {'$in': open_ids + [None]}
            q['last_dir'] = {'$ne': 'in'}  # se o último a falar foi o cliente, quem deve resposta é o time
        for c in await db.contacts.find(q, {'_id': 0}).to_list(3000):
            if t == 'stage_enter': anchor = aware(c['stage_at'])
            else:
                marks = [aware(c.get(k)) for k in ('last_in_at', 'stage_at') if c.get(k)]
                anchor = max(marks) if marks else aware(c.get('created_at'))
                if not anchor or not (oldest < anchor <= cut): continue
            out.append((f"{a['id']}:{c['id']}:{anchor.isoformat()}", c, None))
    elif t in ('before_expiry', 'after_expiry'):
        pid = a.get('product_id')
        lo, hi = (now(), now() + h) if t == 'before_expiry' else (cut - timedelta(days=3), cut)  # retomada: ainda tenta por 3 dias
        for c in await db.contacts.find({'company_id': cid, 'opt_out': {'$ne': True}, 'holdings.expires_at': {'$gt': lo, '$lte': hi}}, {'_id': 0}).to_list(3000):
            for x in c.get('holdings') or []:
                e = aware(x.get('expires_at'))
                if x.get('cancelled_at'): continue  # cancelou a assinatura: aviso de vencimento não faz sentido (a retomada é com o time)
                if e and lo < e <= hi and (not pid or x.get('product_id') == pid):
                    tz = agenda.tz_of(company)
                    out.append((f"{a['id']}:{c['id']}:{x['key']}:{e.isoformat()}", c, {'_extra': {'produto': x['name'], 'vencimento': f'{e.astimezone(tz):%d/%m}'}}))
    elif t == 'after_milestone':
        from journey import hits_for
        hits = await hits_for(company, start=oldest, end=cut)
        mine = [x for x in hits if x['milestone_id'] == a.get('milestone_id')]
        later = {}
        if a.get('unless_id') and mine:
            for x in await hits_for(company, list({m['contact_id'] for m in mine}), start=oldest):
                if x['milestone_id'] == a['unless_id']: later.setdefault(x['contact_id'], []).append(x['at'])
        for x in mine:
            if any(w >= x['at'] for w in later.get(x['contact_id'], [])): continue  # já avançou: não precisa
            c = await db.contacts.find_one({'id': x['contact_id']}, {'_id': 0})
            if c and not c.get('opt_out'): out.append((f"{a['id']}:{c['id']}:{x['at'].isoformat()}", c, None))
    elif t in ('checkout_pending', 'checkout_abandoned'):
        kind = 'pending' if t == 'checkout_pending' else 'abandoned'
        for ev in await db.checkout_events.find({'company_id': cid, 'kind': kind, 'resolved': {'$ne': True}, 'at': {'$lte': cut, '$gt': oldest}}, {'_id': 0}).to_list(3000):
            c = await db.contacts.find_one({'id': ev.get('contact_id')}, {'_id': 0}) if ev.get('contact_id') else None
            if not c or c.get('opt_out'): continue
            val = f'R$ {float(ev["value"]):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.') if ev.get('value') else ''
            out.append((f"{a['id']}:{ev['id']}", c, {'_extra': {'produto': ev.get('product') or 'produto', 'valor': val,
                        'link_pagamento': ev.get('url') or 'é só me responder que eu te mando o link'}}))
    elif t == 'after_purchase':
        q = {'company_id': cid, 'status': 'ok', 'date': {'$lte': cut, '$gt': oldest}}
        if a.get('product_id'): q['items.product_id'] = a['product_id']
        for sl in await db.sales.find(q, {'_id': 0}).to_list(3000):
            c = await db.contacts.find_one({'id': sl['contact_id']}, {'_id': 0})
            it = next((i for i in sl['items'] if not a.get('product_id') or i.get('product_id') == a['product_id']), sl['items'][0])
            if c and not c.get('opt_out'): out.append((f"{a['id']}:{sl['id']}", c, {'_extra': {'produto': it['name']}}))
    else:
        if t == 'before_appt': q = {'status': 'scheduled' if a.get('only_unconfirmed') else {'$in': agenda.ACTIVE}, 'start': {'$gt': now(), '$lte': now() + h}}
        elif t == 'appt_no_show': q = {'status': 'no_show', 'start': {'$lte': cut, '$gt': oldest - GRACE}}
        else: q = {'status': 'done', 'end': {'$lte': cut, '$gt': oldest - GRACE}}
        for ap in await db.appointments.find({'company_id': cid, **q}, {'_id': 0}).to_list(2000):
            if t == 'before_appt' and aware(ap['created_at']) > aware(ap['start']) - h + timedelta(minutes=30): continue  # marcado em cima da hora
            c = await db.contacts.find_one({'id': ap['contact_id']}, {'_id': 0})
            if c and not c.get('opt_out'): out.append((f"{a['id']}:{ap['id']}", c, ap))
    return out

async def fire(a, company, stages, key, contact, appt):
    from funnel import move_stage
    run = {'key': key, 'company_id': company['id'], 'automation_id': a['id'], 'automation': a['name'], 'contact_id': contact['id'], 'status': 'sending',
           'at': now(), 'expires_at': now() + timedelta(days=180)}
    try: await db.automation_runs.insert_one(run.copy())
    except Exception: return None  # já disparou (chave única)
    by = f'Automação · {a["name"]}'
    ctx = await context(company, contact, appt)
    if a.get('coupon_promo_id') and '{cupom}' in (a.get('message') or '') + str((await db.templates.find_one({'id': a.get('template_id')}, {'_id': 0, 'params': 1}) or {}).get('params')):
        ctx['cupom'] = await personal_coupon(company['id'], a['coupon_promo_id'], contact['id'], f'Automação · {a["name"]}')
    from channels import view, window_of, label as ch_label
    contact = view(contact)
    ch = contact.get('channel') or 'whatsapp'
    win = window_of(ch)
    li = aware(contact.get('last_in_at'))
    status, detail = 'skipped', ''
    try:
        if not win or (li and now() - li < win):  # canal sem janela (Telegram, e-mail) sempre pode
            if a['trigger'] == 'before_appt' and appt and appt.get('id') and a.get('buttons', True) and ch == 'whatsapp':  # lembrete com botões
                import wachoice
                await wachoice.send_choices(company['id'], contact, render(a['message'], ctx), by,
                                            buttons=[('ap_ok:' + appt['id'], wachoice.CONFIRM_BTN), ('ap_re:' + appt['id'], wachoice.RESCHED_BTN)])
            else: await deliver(company['id'], contact, render(a['message'], ctx), None, by)
            status = 'sent'
        elif ch != 'whatsapp':
            detail = f'{ch_label(ch)}: fora das 24 h só uma pessoa do time pode responder'
        else:
            t = await db.templates.find_one({'id': a.get('template_id'), 'company_id': company['id']}, {'_id': 0}) if a.get('template_id') else None
            if t and t.get('status') == 'APPROVED':
                await send_template(company['id'], contact, t, ctx, by); status, detail = 'sent', 'por modelo aprovado'
            else:
                detail = 'janela de 24 h fechada e sem modelo aprovado' if not t else 'modelo ainda não aprovado pela Meta'
        if status == 'sent' and a.get('move_to'): await move_stage(company, contact, a['move_to'], by, stages)
    except HTTPException as e:
        status, detail = 'error', str(e.detail)[:200]
    except Exception as e:
        log.exception('falha na automação'); status, detail = 'error', type(e).__name__
    await db.automation_runs.update_one({'key': key}, {'$set': {'status': status, 'detail': detail}})
    await db.automations.update_one({'id': a['id']}, {'$inc': {'sent' if status == 'sent' else 'skipped': 1}, '$set': {'last_run_at': now()}})
    return status

async def personal_coupon(company_id, promo_id, contact_id, by, days=30, prefix=''):
    """Cupom individual de uso único para este cliente (vale por alguns dias)."""
    import promos
    from datetime import datetime
    body = promos.CouponIn(max_uses=1, one_per_contact=True, expires=(now() + timedelta(days=days)).date().isoformat())
    for _ in range(5):
        code = promos.random_code(prefix or 'OI')
        if not await db.coupons.find_one({'company_id': company_id, 'code': code}): break
    c = await promos.make_coupon(company_id, promo_id, code, body, contact_id, by)
    await db.promos.update_one({'id': promo_id}, {'$set': {'requires_coupon': True}})
    return c['code']

async def run_once(limit=300):
    from funnel import stages_for
    done = 0; cache = {}
    for a in await db.automations.find({'active': True}, {'_id': 0}).to_list(5000):
        if a['company_id'] not in cache:
            co = await db.companies.find_one({'id': a['company_id'], 'status': 'active'}, {'_id': 0})
            if co:
                from billing import has
                if not has(co, 'automacoes'): co = None  # só leitura ou plano sem automações
            cache[a['company_id']] = (co, await stages_for(co) if co else None)
        company, stages = cache[a['company_id']]
        if not company or in_quiet(company): continue
        if not await db.wa_numbers.find_one({'company_id': company['id']}, {'_id': 1}): continue
        for key, c, ap in await candidates(a, company, stages):
            if await db.automation_runs.find_one({'key': key}, {'_id': 1}): continue
            if await fire(a, company, stages, key, c, ap): done += 1
            if done >= limit: return done
    return done

async def sync_pending():
    """Modelo em análise na Meta: confere sozinho a cada 15 min. Sem isso a automação ficava "Aguardando a Meta" até alguém clicar em atualizar."""
    from datetime import timedelta
    cut = now() - timedelta(minutes=15)
    q = {'status': 'PENDING', '$or': [{'synced_at': {'$lt': cut}}, {'synced_at': {'$exists': False}}]}
    for cid in (await db.templates.distinct('company_id', q))[:20]:
        try: await sync_templates(cid)
        except Exception as e: log.warning('conferir modelos na Meta (%s): %s', cid, e)
        await db.templates.update_many({'company_id': cid, 'status': 'PENDING'}, {'$set': {'synced_at': now()}})
    try:
        from notify import refresh_status
        for c in await db.companies.find({'alert_tpl.status': 'PENDING', '$or': [{'alert_tpl.synced_at': {'$lt': cut}}, {'alert_tpl.synced_at': {'$exists': False}}]}, {'_id': 0}).to_list(20):
            await refresh_status(c)
            await db.companies.update_one({'id': c['id']}, {'$set': {'alert_tpl.synced_at': now()}})
    except Exception: log.exception('falha ao conferir o modelo de aviso do time')

LOOP_ID = uid()

async def lead():
    """Só um processo roda o ciclo: o servidor tem mais de um worker, e dois ciclos juntos mandariam e-mail e mensagem em dobro."""
    from datetime import timedelta
    from pymongo.errors import DuplicateKeyError
    until = now() + timedelta(seconds=150)
    try:
        await db.locks.update_one({'_id': 'loop', '$or': [{'owner': LOOP_ID}, {'until': {'$lt': now()}}]}, {'$set': {'owner': LOOP_ID, 'until': until}}, upsert=True)
        return True
    except DuplicateKeyError:
        return False

async def loop():
    every = float(os.environ.get('AUTOMATIONS_EVERY', '60'))
    await asyncio.sleep(10)
    while True:
        try:
            if not await lead():
                await asyncio.sleep(every); continue
        except Exception: log.exception('falha ao decidir quem roda o ciclo')
        try: await run_once()
        except Exception: log.exception('falha no ciclo das automações')
        try:
            from teams import check_sla
            await check_sla()
        except Exception: log.exception('falha no alerta de fila das equipes')
        try:
            from campaigns import tick
            await tick()
        except Exception: log.exception('falha no envio das campanhas')
        try:
            from later import tick as later_tick
            await later_tick()
        except Exception: log.exception('falha no envio das mensagens agendadas')
        try:
            from goals import tick as goals_tick
            await goals_tick()
        except Exception: log.exception('falha nas metas')
        try:
            from billing import tick as billing_tick
            await billing_tick()
        except Exception: log.exception('falha na conferência das assinaturas')
        try:
            from notices import tick as notices_tick
            await notices_tick()
        except Exception: log.exception('falha nos avisos automáticos')
        try:
            from owneralerts import tick as owner_tick
            await owner_tick()
        except Exception: log.exception('falha nos avisos para o admin')
        try:
            from instagram import refresh_tokens
            await refresh_tokens()
        except Exception: log.exception('falha ao renovar o acesso do Instagram')
        try: await sync_pending()
        except Exception: log.exception('falha ao conferir os modelos na Meta')
        try:
            from wahealth import tick as wa_tick
            await wa_tick()
        except Exception: log.exception('falha ao conferir a saúde dos números')
        try:
            from push import late_tick
            await late_tick()
        except Exception: log.exception('falha no aviso de espera')
        await asyncio.sleep(every)
