"""Devolver vendas para a Meta (API de Conversões para mensagens).

Quando alguém que veio de um anúncio compra, a Meta fica sabendo da compra e do valor. Assim ela aprende quem compra
de verdade e passa a mostrar o anúncio para gente parecida, em vez de quem só conversa.

- WhatsApp: só quem chegou por anúncio com botão de WhatsApp (a Meta mandou o ctwa_clid junto da 1ª mensagem).
- Messenger e Instagram: pela pessoa da conversa (PSID / IGSID). No Instagram a Meta recebe, mas ainda não otimiza por compra.
- Venda registrada vira "Purchase" com o valor. Lead qualificado (opcional) vira "QualifiedLead" quando o cliente chega
  numa etapa do funil escolhida pela empresa.
- A Meta não tira repetidos, então cada venda e cada lead vai uma vez só (marcado no próprio registro).
- O ciclo das automações chama tick() a cada minuto; vale para venda registrada à mão, por checkout ou aprovada depois.
"""
import logging, os
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, audit, current_user, managers
from whatsapp import decrypt, aware

router = APIRouter()
log = logging.getLogger('oinanda.capi')
WINDOW = timedelta(days=7)      # venda mais velha que isso a Meta não atribui mais ao anúncio
TRIES = 3

async def allowed(cid):
    """Até a Meta aprovar as permissões (whatsapp_business_manage_events, page_events, instagram_manage_events), só a
    empresa do admin da plataforma usa. CAPI_OPEN=1 libera para todos; CAPI_COMPANIES libera empresas avulsas."""
    if os.environ.get('CAPI_OPEN') == '1': return True
    if cid in {x.strip() for x in os.environ.get('CAPI_COMPANIES', '').split(',') if x.strip()}: return True
    from core import ADMIN_EMAIL
    return bool(ADMIN_EMAIL) and bool(await db.users.find_one({'company_id': cid, 'email': ADMIN_EMAIL}, {'_id': 1}))

def _err(e): return str(getattr(e, 'detail', e))[:300]

async def ctx(cid):
    """O que a empresa tem conectado para avisar a Meta."""
    wa = await db.wa_numbers.find_one({'company_id': cid}, {'_id': 0, 'waba_id': 1, 'token_enc': 1, 'status': 1})
    ig = await db.ig_accounts.find_one({'company_id': cid}, {'_id': 0, 'ig_id': 1, 'page_id': 1, 'mode': 1, 'token_enc': 1, 'status': 1})
    return wa, ig

def channel_of(contact):
    """Por qual canal dá para avisar a Meta sobre este cliente, ou o motivo de não dar."""
    if contact.get('fb_id'): return 'messenger', None
    if contact.get('ig_id'): return 'instagram', None
    if (contact.get('ad') or {}).get('ctwa_clid'): return 'whatsapp', None
    return None, 'not_ad'

async def dataset(cid, ch, wa, ig):
    c = await db.companies.find_one({'id': cid}, {'_id': 0, 'capi': 1}) or {}
    ds = ((c.get('capi') or {}).get('ds') or {}).get(ch)
    if ds: return ds
    if ch == 'whatsapp':
        from whatsapp import graph
        r = await graph('POST', f'/{wa["waba_id"]}/dataset', decrypt(wa['token_enc']))
    else:
        from instagram import ig as igcall
        owner = ig['page_id'] if ch == 'messenger' else ig['ig_id']
        r = await igcall('POST', f'/{owner}/dataset', decrypt(ig['token_enc']))
    ds = str(r.get('id') or r.get('dataset_id') or '')
    if not ds: raise HTTPException(400, 'A Meta não devolveu o conjunto de dados.')
    await db.companies.update_one({'id': cid}, {'$set': {f'capi.ds.{ch}': ds}})
    return ds

def event(name, ch, contact, wa, ig, at, value=None, eid=None):
    ud = {'whatsapp': lambda: {'whatsapp_business_account_id': wa['waba_id'], 'ctwa_clid': contact['ad']['ctwa_clid']},
          'messenger': lambda: {'page_id': ig['page_id'], 'page_scoped_user_id': contact['fb_id']},
          'instagram': lambda: {'instagram_business_account_id': ig['ig_id'], 'ig_sid': contact['ig_id']}}[ch]()
    e = {'event_name': name, 'event_time': int(aware(at).timestamp()), 'action_source': 'business_messaging', 'messaging_channel': ch, 'user_data': ud}
    if eid: e['event_id'] = eid
    if value is not None: e['custom_data'] = {'currency': 'BRL', 'value': round(float(value), 2)}
    return e

async def send(cid, ch, ev, wa, ig):
    ds = await dataset(cid, ch, wa, ig)
    if ch == 'whatsapp':
        from whatsapp import graph
        return await graph('POST', f'/{ds}/events', decrypt(wa['token_enc']), {'data': [ev]})
    from instagram import ig as igcall
    return await igcall('POST', f'/{ds}/events', decrypt(ig['token_enc']), payload={'data': [ev]})

def ready(ch, wa, ig):
    if ch == 'whatsapp': return bool(wa and wa.get('waba_id') and wa.get('token_enc'))
    if ch == 'messenger': return bool(ig and ig.get('mode') == 'page' and ig.get('page_id'))
    return bool(ig and ig.get('ig_id') and ig.get('token_enc'))

async def mark(coll, key, val, status, err=None, tries=0):
    await db[coll].update_one({'id': key}, {'$set': {val: {'status': status, 'at': now(), 'error': err, 'tries': tries}}})

async def run_company(company):
    cid, cfg = company['id'], company.get('capi') or {}
    wa, ig = await ctx(cid)
    since = now() - WINDOW
    sales = await db.sales.find({'company_id': cid, 'status': 'ok', 'created_at': {'$gte': since},
                                 '$or': [{'capi': {'$exists': False}}, {'capi.status': 'failed', 'capi.tries': {'$lt': TRIES}}]},
                                {'_id': 0, 'id': 1, 'contact_id': 1, 'total': 1, 'created_at': 1, 'capi': 1}).to_list(200)
    for s in sales:
        c = await db.contacts.find_one({'id': s['contact_id']}, {'_id': 0}) or {}
        ch, why = channel_of(c)
        if not ch: await mark('sales', s['id'], 'capi', 'skip', why); continue
        if not ready(ch, wa, ig): await mark('sales', s['id'], 'capi', 'skip', 'no_channel'); continue
        tries = ((s.get('capi') or {}).get('tries') or 0) + 1
        try:
            await send(cid, ch, event('Purchase', ch, c, wa, ig, s['created_at'], s['total'], 'sale_' + s['id']), wa, ig)
            await mark('sales', s['id'], 'capi', 'sent', None, tries)
        except Exception as e:
            await mark('sales', s['id'], 'capi', 'failed', _err(e), tries)
            await db.companies.update_one({'id': cid}, {'$set': {'capi.last_error': _err(e), 'capi.last_error_at': now()}})
    stage = cfg.get('ql_stage')
    if not stage: return
    leads = await db.contacts.find({'company_id': cid, 'stage': stage, 'stage_at': {'$gte': since}, 'capi_ql': {'$exists': False}}, {'_id': 0}).to_list(200)
    for c in leads:
        ch, why = channel_of(c)
        if not ch or not ready(ch, wa, ig):
            await db.contacts.update_one({'id': c['id']}, {'$set': {'capi_ql': {'status': 'skip', 'error': why or 'no_channel', 'at': now()}}}); continue
        try:
            await send(cid, ch, event('QualifiedLead', ch, c, wa, ig, c['stage_at'], None, 'ql_' + c['id']), wa, ig)
            await db.contacts.update_one({'id': c['id']}, {'$set': {'capi_ql': {'status': 'sent', 'at': now()}}})
        except Exception as e:
            await db.contacts.update_one({'id': c['id']}, {'$set': {'capi_ql': {'status': 'failed', 'error': _err(e), 'at': now()}}})

async def tick():
    async for co in db.companies.find({'capi.on': True}, {'_id': 0, 'id': 1, 'capi': 1}):
        try:
            if await allowed(co['id']): await run_company(co)
        except Exception: log.exception('falha ao avisar a Meta (%s)', co['id'])

# ---------- tela ----------
class CapiIn(BaseModel):
    on: bool
    ql_stage: Optional[str] = Field(default=None, max_length=40)

@router.get('/capi')
async def get_capi(user=Depends(current_user)):
    cid = user['company_id']
    c = await db.companies.find_one({'id': cid}, {'_id': 0, 'capi': 1}) or {}
    cfg = c.get('capi') or {}
    wa, ig = await ctx(cid)
    from funnel import stages_for
    stages = [{'id': s['id'], 'name': s['name']} for s in await stages_for(user['company']) if s.get('kind') not in ('won', 'lost')]
    since = now() - timedelta(days=30)
    rows = await db.sales.find({'company_id': cid, 'status': 'ok', 'created_at': {'$gte': since}, 'capi': {'$exists': True}}, {'_id': 0, 'total': 1, 'capi': 1}).to_list(20000)
    sent = [r for r in rows if r['capi'].get('status') == 'sent']
    failed = [r for r in rows if r['capi'].get('status') == 'failed']
    last = max((aware(r['capi']['at']) for r in sent), default=None)
    ql = await db.contacts.count_documents({'company_id': cid, 'capi_ql.status': 'sent', 'capi_ql.at': {'$gte': since}})
    ch = {'whatsapp': {'connected': bool(wa), 'ready': ready('whatsapp', wa, ig)},
          'instagram': {'connected': bool(ig), 'ready': ready('instagram', wa, ig)},
          'messenger': {'connected': bool(ig), 'ready': ready('messenger', wa, ig) and bool(((user['company'].get('channels')) or {}).get('messenger'))}}
    return {'available': await allowed(cid), 'on': bool(cfg.get('on')), 'ql_stage': cfg.get('ql_stage'), 'stages': stages, 'channels': ch,
            'stats': {'sent': len(sent), 'value': round(sum(r.get('total') or 0 for r in sent), 2), 'failed': len(failed), 'qualified': ql},
            'last_sent': last, 'last_error': cfg.get('last_error') if failed else None}

@router.put('/capi')
async def put_capi(body: CapiIn, request: Request, user=Depends(managers)):
    cid = user['company_id']
    wa, ig = await ctx(cid)
    if body.on and not await allowed(cid): raise HTTPException(400, 'A Meta ainda está liberando esse recurso para o Oi Nanda. Avisamos quando estiver pronto.')
    if body.on and not (wa or ig): raise HTTPException(400, 'Conecte o WhatsApp ou o Instagram primeiro.')
    if body.ql_stage:
        from funnel import stages_for
        if body.ql_stage not in {s['id'] for s in await stages_for(user['company'])}: raise HTTPException(400, 'Escolha uma etapa do funil.')
    upd = {'capi.on': body.on, 'capi.ql_stage': body.ql_stage or None}
    if body.on: upd['capi.since'] = now()
    await db.companies.update_one({'id': cid}, {'$set': upd})
    await audit(request, user, ('Ligou' if body.on else 'Desligou') + ' o aviso de vendas para a Meta', '')
    return await get_capi(user)
