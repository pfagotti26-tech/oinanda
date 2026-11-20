"""Ficha do cliente: quem é, o que comprou e tudo o que aconteceu com ele, num lugar só.

/contacts/{id}/profile  dados, situação, números (gasto, compras, ticket) e se dá para escrever agora (janela de 24 h)
/contacts/{id}/timeline linha do tempo: chegada, conversas (resumidas por dia), vendas, horários, campanhas, etapas,
                        oportunidades de pós-venda, marcos da jornada e avisos do sistema e do time

Abrir a ficha não marca a conversa como lida (isso é da tela de conversas).
"""
import re
from datetime import timedelta
from fastapi import APIRouter, Depends
from core import db, now, current_user
from whatsapp import get_contact, aware

router = APIRouter()
# avisos que já aparecem como item próprio (venda, etapa, horário) ou que não contam a história do cliente
NOISE = re.compile(r'registrou uma venda|aprovou a venda| moveu para | moveu (renovação|próximo produto|reativação)| agendou |^Alerta: |deixou a Nanda em modo|tirou da fila|A Nanda estimou|religou a Nanda', re.I)
CH_NAME = {'whatsapp': 'WhatsApp', 'instagram': 'Instagram', 'messenger': 'Messenger', 'telegram': 'Telegram', 'email': 'e-mail'}

@router.get('/contacts/{contact_id}/profile')
async def profile(contact_id: str, user=Depends(current_user)):
    from channels import view, channels_of, out_channel, window_of
    from origins import status_of, origins_of, STATUS
    raw = await get_contact(user, contact_id)
    c = view(raw)
    company = user['company']
    li = aware(c.get('last_in_at')); w = window_of(c.get('channel'))
    win_open = True if w is None else bool(li and now() - li < w)
    ends = (li + w) if (li and w) else None
    human_only = False
    if c.get('channel') in ('instagram', 'messenger'):
        from instagram import human_window
        if li and w and w <= now() - li < human_window(): human_only, win_open, ends = True, True, li + human_window()
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    onames = {o['id']: o['name'] for o in await origins_of(company)}
    sales = await db.sales.find({'company_id': user['company_id'], 'contact_id': contact_id, 'status': 'ok'}, {'_id': 0, 'total': 1, 'date': 1}).to_list(1000)
    spent = round(sum(s['total'] for s in sales), 2)
    dates = sorted(aware(s['date']) for s in sales if s.get('date'))
    team = await db.teams.find_one({'id': raw.get('team_id')}, {'_id': 0, 'name': 1}) if raw.get('team_id') else None
    org = raw.get('origin') or ('anuncio' if raw.get('ad') else None)
    kind = status_of(raw, company.get('inactive_days'))
    keep = ('id', 'name', 'wa_id', 'ig_username', 'email', 'tags', 'notes', 'opt_out', 'stage', 'value', 'created_at', 'origin_detail', 'referred_by_name', 'referred_by_id', 'assigned_to', 'team_id', 'channel')
    return {**{k: raw.get(k) for k in keep}, 'channel': c.get('channel') or 'whatsapp', 'channels': channels_of(raw), 'reply_channel': out_channel(raw),
            'kind': kind, 'kind_name': STATUS.get(kind), 'origin': org, 'origin_name': onames.get(org) if org else None,
            'owner': names.get(raw.get('assigned_to')), 'team': (team or {}).get('name'),
            'last_in_at': li, 'window_open': win_open, 'window_ends_at': ends, 'human_only': human_only,
            'stats': {'spent': spent, 'purchases': len(sales), 'ticket': round(spent / len(sales), 2) if sales else None,
                      'first_sale_at': dates[0] if dates else None, 'last_sale_at': dates[-1] if dates else None}}

def _day(t, tz):
    return aware(t).astimezone(tz).date().isoformat()

@router.get('/contacts/{contact_id}/timeline')
async def timeline(contact_id: str, user=Depends(current_user)):
    import agenda
    from funnel import stages_for
    c = await get_contact(user, contact_id)
    cid, company = user['company_id'], user['company']
    tz = agenda.tz_of(company)
    ev = []
    def add(at, kind, title, sub='', by=''):
        if at: ev.append({'at': aware(at), 'kind': kind, 'title': title, 'sub': sub, 'by': by})
    # chegada
    how = CH_NAME.get(c.get('channel') or 'whatsapp', 'WhatsApp')
    add(c.get('created_at'), 'start', f'Chegou pelo {how}', ('Anúncio: ' + ((c.get('ad') or {}).get('headline') or '')) if c.get('ad') else '')
    # conversas: um item por dia, com quem falou
    days = {}
    for m in await db.messages.find({'company_id': cid, 'contact_id': contact_id}, {'_id': 0, 'dir': 1, 'type': 1, 'text': 1, 'by': 1, 'at': 1}).sort('at', 1).to_list(20000):
        if m.get('dir') == 'sys':
            if not NOISE.search(m.get('text') or ''): add(m['at'], 'sys', m.get('text') or '')
            continue
        if m.get('dir') == 'note':
            add(m['at'], 'note', 'Anotação do time', m.get('text') or '', m.get('by') or ''); continue
        d = days.setdefault(_day(m['at'], tz), {'at': m['at'], 'last': m['at'], 'in': 0, 'ai': 0, 'team': 0, 'first': '', 'who': set()})
        d['last'] = m['at']
        if m.get('dir') == 'in':
            d['in'] += 1
            if not d['first'] and m.get('text'): d['first'] = m['text'][:140]
        elif '(IA)' in (m.get('by') or ''): d['ai'] += 1
        else:
            d['team'] += 1
            if m.get('by'): d['who'].add(m['by'])
    for d in days.values():
        parts = []
        if d['in']: parts.append(f"{d['in']} do cliente")
        if d['ai']: parts.append(f"{d['ai']} da Nanda")
        if d['team']: parts.append(f"{d['team']} do time")
        title = 'Conversou' if d['in'] else 'Recebeu mensagem'
        add(d['at'], 'chat', title, (f'"{d["first"]}"' if d['first'] else ''), ' · '.join(parts) + (f" ({', '.join(sorted(d['who']))})" if d['who'] else ''))
    # vendas
    for s in await db.sales.find({'company_id': cid, 'contact_id': contact_id}, {'_id': 0}).to_list(500):
        items = ', '.join(i['name'] for i in s.get('items') or [])
        st = {'cancelled': ' (cancelada)', 'pending': ' (aguardando aprovação)'}.get(s.get('status'), '')
        add(s.get('date'), 'sale' if s.get('status') == 'ok' else 'sale_x', f"Comprou{st}", items,
            f"R$ {s['total']:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.') + (f" · {s['sellers'][0].get('name')}" if s.get('sellers') and s['sellers'][0].get('name') else ''))
    # horários
    st_name = {'scheduled': 'Horário marcado', 'confirmed': 'Horário confirmado', 'done': 'Compareceu', 'no_show': 'Faltou', 'canceled': 'Horário cancelado'}
    for a in await db.appointments.find({'company_id': cid, 'contact_id': contact_id}, {'_id': 0}).to_list(300):
        when = aware(a['start']).astimezone(tz)
        what = f"{a.get('service_name') or ''} · {when.strftime('%d/%m às %H:%M')}".strip(' ·')
        add(a.get('created_at') or a['start'], 'appt', 'Horário marcado', what, a.get('by') or '')
        if a.get('status') in ('done', 'no_show', 'canceled'):
            add(a.get('status_at') or a['start'], 'appt' if a['status'] == 'done' else 'appt_x', st_name[a['status']], what)
    # campanhas
    sends = await db.campaign_sends.find({'company_id': cid, 'contact_id': contact_id, 'status': 'sent'}, {'_id': 0, 'campaign_id': 1, 'at': 1}).to_list(300)
    if sends:
        cn = {x['id']: x['name'] for x in await db.campaigns.find({'id': {'$in': list({s['campaign_id'] for s in sends})}}, {'_id': 0, 'id': 1, 'name': 1}).to_list(300)}
        for s in sends: add(s.get('at'), 'camp', 'Recebeu campanha', cn.get(s['campaign_id'], ''))
    # etapas do funil
    stages = {s['id']: s['name'] for s in await stages_for(company)}
    for e in await db.stage_events.find({'company_id': cid, 'contact_id': contact_id}, {'_id': 0}).to_list(500):
        if e.get('to') in stages and e.get('from') != e.get('to'): add(e.get('at'), 'stage', f"Foi para \"{stages[e['to']]}\"", '', e.get('by') or '')
    # pós-venda
    ok = {'renovacao': 'renovação', 'upsell': 'próximo produto', 'reativacao': 'reativação'}
    for o in await db.opps.find({'company_id': cid, 'contact_id': contact_id}, {'_id': 0}).to_list(200):
        add(o.get('created_at'), 'opp', f"Entrou no pós-venda ({ok.get(o.get('kind'), o.get('kind'))})", o.get('product_name') or '', o.get('assigned_name') or '')
    # marcos da jornada
    if True:
        try:
            from journey import hits_for, active_ms
            ms = {m['id']: m for m in active_ms(company)}
            seen = set()
            for h in await hits_for(company, [contact_id]):
                m = ms.get(h['milestone_id'])
                if not m or m['rule']['type'] in ('created', 'sale', 'stage', 'appt_booked', 'appt_done'): continue  # já aparecem acima
                k = (h['milestone_id'], _day(h['at'], tz))
                if k in seen: continue
                seen.add(k); add(h['at'], 'mile', f"{(m.get('icon') or '').strip()} {m['name']}".strip())
        except Exception:
            pass
    st = next((x for x in ev if x['kind'] == 'start'), None)
    if st and len(ev) > 1:  # a chegada é sempre o primeiro acontecimento
        st['at'] = min(st['at'], min(x['at'] for x in ev if x is not st) - timedelta(seconds=1))
    ev.sort(key=lambda x: x['at'], reverse=True)
    return {'items': ev[:400], 'total': len(ev)}
