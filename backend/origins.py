"""Origem do contato e se ele é cliente ou prospect.

Origem: de onde a pessoa veio (anúncio, Google, site, Instagram, indicação, evento, campanha...). A lista é da empresa.
Como a origem é descoberta, nesta ordem de confiança:
- anúncio da Meta com botão de WhatsApp: a Meta manda junto (automático);
- link rastreável / QR Code: cada canal tem o seu link, e a primeira mensagem leva um código curto (#oi-xxxxx);
- a Nanda pergunta com naturalidade, quando ainda não se sabe, e registra a resposta;
- o time ajusta à mão no painel do cliente.

Cliente ou prospect: sem venda é prospect; com venda é cliente; se a empresa definir um prazo, cliente que não compra
há mais que isso vira inativo.
"""
import re, secrets
from datetime import timedelta
from typing import Optional, List
from urllib.parse import quote
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, note, aware

router = APIRouter()
DEFAULT = [('anuncio', 'Anúncio (Meta)'), ('google', 'Google'), ('site', 'Site'), ('instagram', 'Instagram'), ('facebook', 'Facebook'),
           ('tiktok', 'TikTok'), ('indicacao', 'Indicação'), ('evento', 'Evento'), ('campanha', 'Campanha'), ('outro', 'Outros')]
CODE = re.compile(r'#oi-([a-z0-9]{5})\b', re.I)
HOW = {'anuncio': 'automático pelo anúncio', 'link': 'pelo link rastreável', 'nanda': 'a Nanda perguntou', 'cliente': 'o cliente contou', 'time': 'informado pelo time'}

# O cliente conta de onde veio ("vi o anúncio no Instagram", "a Carla me indicou"). Só frases claras, para não marcar errado.
_VERB = r'(?:vi|vim|achei|conheci|encontrei|soube|descobri|pesquisei|busquei|cheguei|apareceu|aparece|pelo|pela|atrav[eé]s d[oa]|l[aá] n[oa]|aqui d[oa]|no seu|na sua)'
SAID = [
    ('anuncio', re.compile(_VERB + r'[^.?!\n]{0,30}\b(?:an[uú]ncio|propaganda|patrocinad[oa])', re.I), 'Disse que viu um anúncio'),
    ('google', re.compile(_VERB + r'[^.?!\n]{0,25}\bgoogle\b', re.I), 'Disse que achou no Google'),
    ('instagram', re.compile(_VERB + r'[^.?!\n]{0,25}\b(?:instagram|insta)\b', re.I), 'Disse que veio do Instagram'),
    ('facebook', re.compile(_VERB + r'[^.?!\n]{0,25}\b(?:facebook|face)\b', re.I), 'Disse que veio do Facebook'),
    ('tiktok', re.compile(_VERB + r'[^.?!\n]{0,25}\btik ?tok\b', re.I), 'Disse que veio do TikTok'),
]
REF = [re.compile(r'(?:\b[Aa]|\b[Oo]|^)\s*([A-ZÁÉÍÓÚÂÊÔÃÕ][\wÀ-ÿ]{1,20}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕ][\wÀ-ÿ]{1,20})?)\s+(?:me\s+)?indic', re.U),
       re.compile(r'indica[çc][aã]o\s+d[aoe]\s+([A-ZÁÉÍÓÚÂÊÔÃÕ][\wÀ-ÿ]{1,20}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕ][\wÀ-ÿ]{1,20})?)', re.U),
       re.compile(r'indicad[ao]\s+(?:pel[ao]|por)\s+([A-ZÁÉÍÓÚÂÊÔÃÕ][\wÀ-ÿ]{1,20}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕ][\wÀ-ÿ]{1,20})?)', re.U)]
REF_ANY = re.compile(r'\b(?:me\s+indic|indica[çc][aã]o\s+d|indicad[ao]\s+(?:pel|por)|uma\s+amiga\s+(?:me\s+)?(?:falou|indicou)|um\s+amigo\s+(?:me\s+)?(?:falou|indicou))', re.I)

def said_origin(text, allowed):
    """(id, detalhe) quando o cliente diz claramente de onde veio; senão None. Anúncio ganha de rede social ("vi o anúncio no Insta")."""
    t = (text or '').strip()
    if not t or len(t) > 600: return None
    if 'indicacao' in allowed and REF_ANY.search(t):
        for r in REF:
            m = r.search(t)
            if m: return 'indicacao', m.group(1).strip()
        return 'indicacao', ''
    for oid, rx, det in SAID:
        if oid in allowed and rx.search(t):
            if oid == 'anuncio':
                net = next((n for n, w in (('Instagram', r'\binsta'), ('Facebook', r'\bface'), ('Google', r'\bgoogle'), ('TikTok', r'\btik ?tok')) if re.search(w, t, re.I)), '')
                return oid, ('Disse que viu um anúncio no ' + net) if net else det
            return oid, det
    return None

def default_origins(): return [{'id': i, 'name': n} for i, n in DEFAULT]

async def origins_of(company):
    if company.get('origins'): return company['origins']
    return default_origins()

def status_of(c, inactive_days=None):
    if not c.get('sales_count'): return 'prospect'
    last = aware(c.get('last_sale_at'))
    if inactive_days and last and now() - last > timedelta(days=inactive_days): return 'inativo'
    return 'cliente'

STATUS = {'prospect': 'Prospect', 'cliente': 'Cliente', 'inativo': 'Cliente inativo'}

# ---------- configuração ----------
class OriginItem(BaseModel):
    id: Optional[str] = Field(default=None, max_length=24)
    name: str = Field(min_length=2, max_length=40)

class OriginsIn(BaseModel):
    origins: List[OriginItem] = Field(min_length=1, max_length=30)
    ask_origin: bool = True
    inactive_days: Optional[int] = Field(default=None, ge=15, le=1095)

@router.get('/origins')
async def get_origins(user=Depends(current_user)):
    c = user['company']
    return {'origins': await origins_of(c), 'ask_origin': c.get('ask_origin', True), 'inactive_days': c.get('inactive_days'), 'status': STATUS}

@router.put('/origins')
async def put_origins(body: OriginsIn, request: Request, user=Depends(managers)):
    seen, rows = set(), []
    for o in body.origins:
        oid = o.id or re.sub(r'[^a-z0-9]+', '_', o.name.lower().encode('ascii', 'ignore').decode()).strip('_')[:24] or uid()[:6]
        if oid in seen: oid = oid[:19] + '_' + uid()[:4]
        seen.add(oid); rows.append({'id': oid, 'name': o.name.strip()})
    if 'anuncio' not in seen: rows.insert(0, {'id': 'anuncio', 'name': 'Anúncio (Meta)'})  # o automático da Meta sempre existe
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'origins': rows, 'ask_origin': body.ask_origin, 'inactive_days': body.inactive_days}})
    await audit(request, user, 'Alterou as origens de contato')
    return {'origins': rows, 'ask_origin': body.ask_origin, 'inactive_days': body.inactive_days}

# ---------- origem de um contato ----------
async def set_origin(company, contact, origin, detail='', how='time', referred_by_id=None, referred_by_name=None, force=False):
    """Grava a origem. Sem force, não troca uma origem que veio de fonte mais confiável."""
    rank = {'anuncio': 4, 'link': 3, 'time': 2, 'cliente': 1, 'nanda': 1}
    cur = contact.get('origin_by')
    if contact.get('origin') and not force and rank.get(cur, 0) > rank.get(how, 0): return False
    upd = {'origin': origin, 'origin_detail': (detail or '').strip()[:80], 'origin_by': how, 'origin_at': now(),
           'referred_by_id': referred_by_id, 'referred_by_name': (referred_by_name or '').strip()[:60] or None}
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd})
    contact.update(upd)
    return True

class OriginSet(BaseModel):
    origin: Optional[str] = Field(default=None, max_length=24)
    detail: str = Field(default='', max_length=80)
    referred_by_id: Optional[str] = None
    referred_by_name: Optional[str] = Field(default=None, max_length=60)

@router.put('/contacts/{contact_id}/origin')
async def put_contact_origin(contact_id: str, body: OriginSet, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    if body.origin is None:
        await db.contacts.update_one({'id': c['id']}, {'$set': {'origin': None, 'origin_detail': None, 'origin_by': None, 'referred_by_id': None, 'referred_by_name': None}})
        return {'ok': True}
    if body.origin not in {o['id'] for o in await origins_of(user['company'])}: raise HTTPException(400, 'Origem não existe.')
    ref_name = body.referred_by_name
    if body.referred_by_id:
        r = await db.contacts.find_one({'id': body.referred_by_id, 'company_id': user['company_id']}, {'_id': 0, 'name': 1})
        if not r: raise HTTPException(404, 'Quem indicou não foi encontrado.')
        ref_name = r['name']
    await set_origin(user['company'], c, body.origin, body.detail, 'time', body.referred_by_id, ref_name, force=True)
    names = {o['id']: o['name'] for o in await origins_of(user['company'])}
    await note(user['company_id'], c['id'], f'{user.get("impersonated_by") or user["name"]} marcou a origem: {names[body.origin]}' + (f' · {body.detail.strip()}' if body.detail.strip() else '') + (f' · indicado por {ref_name}' if ref_name else ''))
    return {'ok': True, 'origin': body.origin}

async def on_inbound(company_id, contact, text, ad):
    """Chamado a cada mensagem que chega: anúncio ou código de link rastreável marcam a origem."""
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if ad and contact.get('origin_by') != 'anuncio':
        await set_origin(company, contact, 'anuncio', ad.get('headline') or ad.get('source_id') or '', 'anuncio')
        return
    m = CODE.search(text or '')
    if not m:
        if contact.get('origin') or not (company or {}).get('ask_origin', True): return
        got = said_origin(text, {o['id'] for o in await origins_of(company)})
        if got and await set_origin(company, contact, got[0], got[1], 'cliente', None, got[1] if got[0] == 'indicacao' else None):
            names = {o['id']: o['name'] for o in await origins_of(company)}
            await note(company_id, contact['id'], f'O cliente contou de onde veio: {names.get(got[0], got[0])}' + (f' · {got[1]}' if got[1] and got[0] == 'indicacao' else ''))
        return
    link = await db.track_links.find_one({'company_id': company_id, 'code': m.group(1).lower()}, {'_id': 0})
    if not link: return
    if await set_origin(company, contact, link['origin'], link.get('detail') or link['label'], 'link'):
        await db.track_links.update_one({'id': link['id']}, {'$inc': {'contacts': 1}, '$set': {'last_contact_at': now()}})
        await note(company_id, contact['id'], f'Veio pelo link "{link["label"]}"')

# ---------- links rastreáveis ----------
class LinkIn(BaseModel):
    label: str = Field(min_length=2, max_length=60)
    origin: str = Field(max_length=24)
    detail: str = Field(default='', max_length=80)
    message: str = Field(default='Olá! Quero saber mais.', min_length=2, max_length=300)

async def company_phone(company_id):
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0, 'display_phone': 1})
    c = await db.companies.find_one({'id': company_id}, {'_id': 0, 'phone': 1})
    d = re.sub(r'\D', '', (n or {}).get('display_phone') or (c or {}).get('phone') or '')
    if len(d) in (10, 11): d = '55' + d
    return d or None

def wa_url(phone, link):
    return f'https://wa.me/{phone}?text=' + quote(f'{link["message"].strip()} #oi-{link["code"]}')

def link_out(l, base):
    return {k: v for k, v in l.items() if k != '_id'} | {'short': f'{base}/api/l/{l["code"]}', 'qr': f'{base}/api/l/{l["code"]}/qr.svg'}

def base_of(request): return str(request.base_url).rstrip('/').replace('http://', 'https://') if 'localhost' not in str(request.base_url) else str(request.base_url).rstrip('/')

@router.get('/track-links')
async def list_links(request: Request, user=Depends(current_user)):
    rows = await db.track_links.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(300)
    phone = await company_phone(user['company_id'])
    return {'phone': phone, 'items': [link_out(l, base_of(request)) | {'wa': wa_url(phone, l) if phone else None} for l in rows]}

@router.post('/track-links')
async def add_link(body: LinkIn, request: Request, user=Depends(managers)):
    if body.origin not in {o['id'] for o in await origins_of(user['company'])}: raise HTTPException(400, 'Origem não existe.')
    if await db.track_links.count_documents({'company_id': user['company_id']}) >= 300: raise HTTPException(400, 'Limite de 300 links.')
    for _ in range(5):
        code = ''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(5))
        if not await db.track_links.find_one({'code': code}): break
    l = {'id': uid(), 'company_id': user['company_id'], 'code': code, 'label': body.label.strip(), 'origin': body.origin, 'detail': body.detail.strip(),
         'message': body.message.strip(), 'clicks': 0, 'contacts': 0, 'created_at': now(), 'by': user['name']}
    await db.track_links.insert_one(l.copy())
    await audit(request, user, 'Criou link rastreável', l['label'])
    phone = await company_phone(user['company_id'])
    return link_out(l, base_of(request)) | {'wa': wa_url(phone, l) if phone else None}

@router.put('/track-links/{lid}')
async def edit_link(lid: str, body: LinkIn, request: Request, user=Depends(managers)):
    l = await db.track_links.find_one({'id': lid, 'company_id': user['company_id']}, {'_id': 0})
    if not l: raise HTTPException(404, 'Link não encontrado.')
    if body.origin not in {o['id'] for o in await origins_of(user['company'])}: raise HTTPException(400, 'Origem não existe.')
    upd = {'label': body.label.strip(), 'origin': body.origin, 'detail': body.detail.strip(), 'message': body.message.strip()}
    await db.track_links.update_one({'id': lid}, {'$set': upd})
    phone = await company_phone(user['company_id'])
    return link_out({**l, **upd}, base_of(request)) | {'wa': wa_url(phone, {**l, **upd}) if phone else None}

@router.delete('/track-links/{lid}')
async def del_link(lid: str, user=Depends(managers)):
    r = await db.track_links.delete_one({'id': lid, 'company_id': user['company_id']})
    if not r.deleted_count: raise HTTPException(404, 'Link não encontrado.')
    return {'ok': True}

@router.get('/l/{code}')
async def open_link(code: str):
    """Link curto público (vai no site, na bio, no QR Code do evento): conta o clique e abre o WhatsApp."""
    l = await db.track_links.find_one({'code': code.lower()}, {'_id': 0})
    if not l: raise HTTPException(404, 'Link não encontrado.')
    phone = await company_phone(l['company_id'])
    if not phone: raise HTTPException(404, 'Este WhatsApp ainda não está disponível.')
    await db.track_links.update_one({'id': l['id']}, {'$inc': {'clicks': 1}})
    return RedirectResponse(wa_url(phone, l), status_code=302)

@router.get('/l/{code}/qr.svg')
async def qr(code: str, request: Request):
    import segno
    l = await db.track_links.find_one({'code': code.lower()}, {'_id': 0, 'code': 1})
    if not l: raise HTTPException(404, 'Link não encontrado.')
    import io
    buf = io.BytesIO()
    segno.make(f'{base_of(request)}/api/l/{l["code"]}', error='m').save(buf, kind='svg', scale=8, border=2, dark='#17212B')
    return Response(buf.getvalue(), media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})

# ---------- relatório de origens ----------
@router.get('/origins/report')
async def origins_report(start: Optional[str] = None, end: Optional[str] = None, user=Depends(managers)):
    from sales import period, Filters, migrate_legacy
    company = user['company']
    await migrate_legacy(company)
    s0, s1, t0, t1 = period(company, Filters(start=start, end=end))
    names = {o['id']: o['name'] for o in await origins_of(company)}
    contacts = await db.contacts.find({'company_id': user['company_id'], 'created_at': {'$gte': t0, '$lt': t1}, 'source': {'$nin': ['historico', 'agenda do celular']}},
                                      {'_id': 0, 'id': 1, 'origin': 1, 'ad': 1, 'referred_by_name': 1, 'referred_by_id': 1}).to_list(50000)
    ids = [c['id'] for c in contacts]
    sales = await db.sales.find({'company_id': user['company_id'], 'contact_id': {'$in': ids}, 'status': 'ok', 'date': {'$lt': t1}}, {'_id': 0, 'contact_id': 1, 'total': 1}).to_list(50000)
    rev, buyers = {}, set()
    for s in sales: rev[s['contact_id']] = rev.get(s['contact_id'], 0) + s['total']; buyers.add(s['contact_id'])
    rows = {}
    for c in contacts:
        o = c.get('origin') or ('anuncio' if c.get('ad') else '_none')
        r = rows.setdefault(o, {'id': o, 'name': names.get(o, 'Não sabemos' if o == '_none' else o), 'contacts': 0, 'clients': 0, 'value': 0.0})
        r['contacts'] += 1
        if c['id'] in buyers: r['clients'] += 1; r['value'] += rev[c['id']]
    out = []
    for r in rows.values():
        r['value'] = round(r['value'], 2); r['conversion'] = round(100 * r['clients'] / r['contacts'], 1) if r['contacts'] else None
        r['ticket'] = round(r['value'] / r['clients'], 2) if r['clients'] else None
        out.append(r)
    out.sort(key=lambda r: (r['id'] == '_none', -r['value'], -r['contacts']))
    refs = {}
    for c in contacts:
        k = c.get('referred_by_id') or c.get('referred_by_name')
        if not k: continue
        x = refs.setdefault(k, {'name': c.get('referred_by_name') or '?', 'contacts': 0, 'clients': 0, 'value': 0.0})
        x['contacts'] += 1
        if c['id'] in buyers: x['clients'] += 1; x['value'] += rev[c['id']]
    known = sum(r['contacts'] for r in out if r['id'] != '_none')
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'rows': out, 'total': len(contacts), 'known': known,
            'referrals': sorted(refs.values(), key=lambda x: (-x['clients'], -x['contacts']))[:10]}
