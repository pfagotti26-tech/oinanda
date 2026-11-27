"""Avisos extras da Meta no Instagram e no Messenger: lido/entregue, reações, de onde a conversa veio
(anúncio "Clique para mensagem" ou link com código) e mensagens que chegam enquanto outro app cuida da conversa."""
import logging
from datetime import datetime, timezone
from core import db, now

log = logging.getLogger('oinanda.metaev')
CID = {'instagram': 'ig_id', 'messenger': 'fb_id'}

async def contact_of(a, m, ch):
    sid = str((m.get('sender') or {}).get('id') or '')
    if not sid: return None
    return await db.contacts.find_one({'company_id': a['company_id'], CID[ch]: sid}, {'_id': 0})

async def on_read(a, m, ch):
    c = await contact_of(a, m, ch)
    if not c: return
    r = m.get('read') or {}
    if r.get('watermark'):  # Messenger: tudo o que saiu até esse instante foi lido
        upto = datetime.fromtimestamp(int(r['watermark']) / 1000, tz=timezone.utc)
    elif r.get('mid'):  # Instagram: a última mensagem vista
        x = await db.messages.find_one({'wa_msg_id': r['mid']}, {'_id': 0, 'at': 1})
        upto = x['at'] if x else now()
    else: upto = now()
    await db.messages.update_many({'contact_id': c['id'], 'dir': 'out', 'at': {'$lte': upto}, 'status': {'$in': ['sent', 'delivered']}}, {'$set': {'status': 'read'}})

async def on_delivery(a, m, ch):
    c = await contact_of(a, m, ch)
    if not c: return
    d = m.get('delivery') or {}
    if d.get('mids'): await db.messages.update_many({'wa_msg_id': {'$in': d['mids']}, 'status': 'sent'}, {'$set': {'status': 'delivered'}})
    if d.get('watermark'):
        upto = datetime.fromtimestamp(int(d['watermark']) / 1000, tz=timezone.utc)
        await db.messages.update_many({'contact_id': c['id'], 'dir': 'out', 'at': {'$lte': upto}, 'status': 'sent'}, {'$set': {'status': 'delivered'}})

async def on_reaction(a, m, ch):
    r = m.get('reaction') or {}
    if not r.get('mid'): return
    if r.get('action') == 'unreact':
        await db.messages.update_one({'wa_msg_id': r['mid']}, {'$unset': {'reaction': ''}})
    else:
        await db.messages.update_one({'wa_msg_id': r['mid']}, {'$set': {'reaction': r.get('emoji') or '❤️'}})

async def on_referral(a, contact, ref, ch):
    """A pessoa chegou por um anúncio ou por um link com código (m.me/...?ref= ou ig.me/m/...?ref=)."""
    if not ref or not contact: return
    from whatsapp import note
    from origins import origins_of, set_origin
    company = await db.companies.find_one({'id': a['company_id']}, {'_id': 0}) or {}
    ctx = ref.get('ads_context_data') or {}
    code = (ref.get('ref') or '').strip()[:60]
    if ref.get('source') == 'ADS' or ref.get('ad_id'):
        title = (ctx.get('ad_title') or '').strip()[:80]
        await set_origin(company, contact, 'anuncio', title or 'Anúncio "Clique para mensagem"', 'anuncio', force=True)
        await db.contacts.update_one({'id': contact['id']}, {'$set': {'ad_id': ref.get('ad_id'), 'last_ref': {'source': 'ADS', 'ad_id': ref.get('ad_id'), 'title': title, 'at': now()}}})
        await note(a['company_id'], contact['id'], f'Chegou pelo anúncio{": " + title if title else ""}')
    elif code:
        oid = 'instagram' if ch == 'instagram' else 'facebook'
        ids = {o['id'] for o in await origins_of(company)}
        if oid in ids: await set_origin(company, contact, oid, f'Link: {code}', 'link', force=True)
        await db.contacts.update_one({'id': contact['id']}, {'$set': {'last_ref': {'source': ref.get('source') or 'LINK', 'ref': code, 'at': now()}}})
        await note(a['company_id'], contact['id'], f'Chegou pelo link com o código "{code}"')

async def dispatch(a, m, ch='instagram'):
    """Trata o que não é mensagem. Devolve True quando o evento já foi tratado aqui."""
    try:
        if 'read' in m: await on_read(a, m, ch); return True
        if 'delivery' in m: await on_delivery(a, m, ch); return True
        if 'reaction' in m: await on_reaction(a, m, ch); return True
        if 'referral' in m and not m.get('message'):  # clicou no link ou no anúncio com a conversa já aberta
            c = await contact_of(a, m, ch)
            if not c:
                from instagram import upsert
                c, _ = await upsert(a, str((m.get('sender') or {}).get('id')), ch=ch, source=ch)
            await on_referral(a, c, m['referral'], ch); return True
    except Exception:
        log.exception('falha num aviso da meta (%s)', ch); return True
    return False
