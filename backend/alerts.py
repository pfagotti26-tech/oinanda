"""Sino de avisos: junta num lugar só o que pede ação da pessoa (cliente pediu alguém, conversa transferida, WhatsApp com problema,
pergunta que a Nanda não soube) e o que só informa (venda registrada, horário marcado), mais os avisos que o Oi Nanda mandou para a conta.

Nada é gravado por item: a lista é montada na hora a partir do que já existe. Cada pessoa guarda só quando abriu o sino pela última vez.
"""
from datetime import timedelta
from fastapi import APIRouter, Depends
from core import db, now, current_user
from whatsapp import aware

router = APIRouter()
MGR = ('owner', 'manager')

def iso(d):
    d = aware(d)
    return d.isoformat() if d else None

def first(name): return ((name or 'Cliente').split(' ') or ['Cliente'])[0]

def brl(v):
    s = f'{float(v or 0):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return 'R$ ' + (s[:-3] if s.endswith(',00') else s)

async def build(user):
    cid, uid_, mgr = user['company_id'], user['id'], user['role'] in MGR
    items = []
    if mgr:
        n = await db.wa_numbers.find_one({'company_id': cid}, {'_id': 0, 'alert': 1})
        if n and n.get('alert'):
            a = n['alert']
            items.append({'key': 'wa:' + str(a.get('kind')), 'tone': 'bad', 'title': a.get('title') or 'O WhatsApp está com problema', 'sub': a.get('detail') or '',
                          'to': 'empresa:whatsapp', 'cta': 'Ver o que fazer', 'at': iso(a.get('at'))})
        gaps = await db.ai_gaps.count_documents({'company_id': cid, 'status': 'open'})
        if gaps:
            top = await db.ai_gaps.find_one({'company_id': cid, 'status': 'open'}, {'_id': 0, 'q': 1, 'last_at': 1}, sort=[('last_at', -1)]) or {}
            items.append({'key': f'gaps:{gaps}', 'tone': 'warn', 'title': 'A Nanda não soube responder ' + ('1 pergunta' if gaps == 1 else f'{gaps} perguntas'),
                          'sub': f'“{top["q"]}”' if top.get('q') else '', 'to': 'nanda', 'cta': 'Ensinar a resposta', 'at': iso(top.get('last_at'))})
        autos = await db.automations.find({'company_id': cid, 'active': True, 'template_id': {'$ne': None}}, {'_id': 0, 'name': 1, 'template_id': 1}).to_list(200)
        if autos:
            tpls = {t['id']: t for t in await db.templates.find({'company_id': cid, 'id': {'$in': [a['template_id'] for a in autos]}}, {'_id': 0, 'id': 1, 'status': 1}).to_list(200)}
            bad = [a for a in autos if (tpls.get(a['template_id']) or {}).get('status') == 'REJECTED']
            if bad:
                items.append({'key': 'tpl:' + ','.join(sorted(a['template_id'] for a in bad)), 'tone': 'bad', 'title': f'A Meta recusou a mensagem de "{bad[0]["name"]}"',
                              'sub': 'Ajuste o texto e peça de novo.', 'to': 'automacoes', 'cta': 'Ajustar', 'at': None})
    # clientes que pediram uma pessoa
    q = {'company_id': cid, 'needs_human': True, 'internal': {'$ne': True}}
    if user['role'] == 'agent': q['assigned_to'] = {'$in': [None, uid_]}
    for c in await db.contacts.find(q, {'_id': 0, 'id': 1, 'name': 1, 'last_in_at': 1}).sort('last_in_at', -1).to_list(5):
        m = await db.messages.find_one({'company_id': cid, 'contact_id': c['id'], 'dir': 'in'}, {'_id': 0, 'text': 1}, sort=[('at', -1)]) or {}
        t = (m.get('text') or '').strip()
        items.append({'key': f'h:{c["id"]}:{iso(c.get("last_in_at"))}', 'tone': 'human', 'title': f'{first(c.get("name"))} pediu para falar com uma pessoa',
                      'sub': f'“{t[:120]}”' if t else '', 'to': 'conv:' + c['id'], 'cta': 'Abrir conversa', 'at': iso(c.get('last_in_at'))})
    # conversas passadas para mim
    for c in await db.contacts.find({'company_id': cid, 'assigned_to': uid_, 'transfer_seen': False}, {'_id': 0, 'id': 1, 'name': 1, 'transfer_by': 1, 'transfer_reason': 1, 'transfer_at': 1}).to_list(10):
        items.append({'key': f'tr:{c["id"]}:{iso(c.get("transfer_at"))}', 'tone': 'transfer', 'title': f'{first(c.get("transfer_by")) if c.get("transfer_by") else "Alguém do time"} passou uma conversa para você',
                      'sub': 'Cliente: ' + (c.get('name') or '') + (f' · {c["transfer_reason"]}' if c.get('transfer_reason') else ''), 'to': 'conv:' + c['id'], 'cta': 'Abrir conversa', 'at': iso(c.get('transfer_at'))})
    act = len(items)
    # só informação: vendas e horários dos últimos 3 dias
    since = now() - timedelta(days=3)
    sq = {'company_id': cid, 'created_at': {'$gte': since}, 'status': {'$ne': 'cancelled'}}
    if not mgr: sq['created_by'] = uid_
    for s in await db.sales.find(sq, {'_id': 0, 'id': 1, 'total': 1, 'contact_name': 1, 'items': 1, 'created_by_name': 1, 'created_at': 1}).sort('created_at', -1).to_list(5):
        nm = ((s.get('items') or [{}])[0].get('name') or '').strip()
        by = s.get('created_by_name') or ''
        items.append({'key': 's:' + s['id'], 'tone': 'sale', 'title': 'Venda registrada' + (' pela Nanda' if '(IA)' in by or by.lower().startswith('nanda') else f' por {first(by)}' if by else ''),
                      'sub': ' · '.join(x for x in [nm, brl(s.get('total')), s.get('contact_name') or ''] if x), 'to': 'vendas', 'cta': '', 'at': iso(s.get('created_at'))})
    aq = {'company_id': cid, 'created_at': {'$gte': since}, 'status': 'scheduled'}
    if not mgr: aq['$or'] = [{'pro_id': uid_}, {'by_id': uid_}]
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(user['company'].get('timezone') or 'America/Sao_Paulo')
    for a in await db.appointments.find(aq, {'_id': 0, 'id': 1, 'service_name': 1, 'contact_name': 1, 'start': 1, 'created_at': 1, 'by': 1}).sort('created_at', -1).to_list(5):
        st = aware(a.get('start'))
        items.append({'key': 'a:' + a['id'], 'tone': 'cal', 'title': 'Horário marcado' + (f' para {st.astimezone(tz):%d/%m às %H:%M}' if st else ''),
                      'sub': ' · '.join(x for x in [a.get('service_name') or '', a.get('contact_name') or ''] if x), 'to': 'agenda', 'cta': '', 'at': iso(a.get('created_at'))})
    info = sorted(items[act:], key=lambda x: x['at'] or '', reverse=True)
    items = items[:act] + info
    news = []
    if mgr:
        for n in await db.notice_log.find({'company_id': cid, 'at': {'$gte': now() - timedelta(days=30)}}, {'_id': 0, 'id': 1, 'subject': 1, 'name': 1, 'at': 1}).sort('at', -1).to_list(10):
            news.append({'key': 'n:' + str(n.get('id')), 'tone': 'news', 'title': n.get('subject') or n.get('name') or 'Aviso do Oi Nanda', 'sub': 'Enviado também por e-mail',
                         'to': '', 'cta': '', 'at': iso(n.get('at'))})
    seen, keys = aware(user.get('alerts_seen')), set(user.get('alerts_keys') or [])
    for x in items + news:
        when = x['at']
        x['new'] = x['key'] not in keys and (not seen or not when or when > seen.isoformat())
        x['act'] = x['tone'] in ('bad', 'warn', 'human', 'transfer')
    return items, news

@router.get('/alerts')
async def alerts(user=Depends(current_user)):
    items, news = await build(user)
    return {'items': items, 'news': news, 'unread': sum(1 for x in items + news if x['new'])}

@router.post('/alerts/seen')
async def seen(user=Depends(current_user)):
    items, news = await build(user)
    await db.users.update_one({'id': user['id']}, {'$set': {'alerts_seen': now(), 'alerts_keys': [x['key'] for x in items + news]}})
    return {'ok': True, 'unread': 0}
