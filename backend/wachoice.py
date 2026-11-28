"""Botões e listas no WhatsApp: o cliente toca em vez de digitar.

- Lembrete de horário com dois botões: "Confirmo" (o horário vira Confirmado) e "Preciso remarcar"
  (chega uma lista com os próximos horários livres do mesmo serviço; ao tocar, o horário muda sozinho).
- A Nanda, quando vai oferecer horários, pode mandar a lista em vez de escrever os horários: o cliente toca e já fica marcado.

Botão e lista só existem no WhatsApp e dentro da janela de 24 h (fora dela vale o modelo aprovado, com botões de resposta
rápida, e a resposta chega como texto: "Confirmo" ou "Preciso remarcar"). Em outros canais, a mesma coisa vai como texto.
Os ids que voltam do WhatsApp dizem o que fazer: ap_ok:<id>, ap_re:<id>, slot:<serviço>:<AAAA-MM-DDTHH:MM>[:<id a remarcar>].
"""
import re, logging
from datetime import datetime, timedelta
from core import db, now, uid
from whatsapp import graph, decrypt, note, aware, WINDOW, has_wa, wa_to

log = logging.getLogger('wachoice')
CONFIRM_BTN, RESCHED_BTN = 'Confirmo', 'Preciso remarcar'
RESCHED = re.compile(r'^\s*(preciso remarcar|quero remarcar|remarcar|preciso mudar( o hor[aá]rio)?|n[aã]o vou conseguir( ir)?|n[aã]o posso( ir)?)[\s!.,😕😔🙏]*$', re.I)

def in_window(contact):
    from channels import view
    c = view(contact)
    li = aware(c.get('last_in_at'))
    return (c.get('channel') or 'whatsapp') == 'whatsapp' and has_wa(c) and li and now() - li < WINDOW

async def send_choices(company_id, contact, body, by_name, buttons=None, rows=None, list_label='Ver horários'):
    """Mensagem com botões (até 3) ou lista (até 10). Fora do WhatsApp/janela, manda o texto com as opções escritas."""
    from whatsapp import deliver
    if not in_window(contact):
        opts = '\n'.join(f'• {t}' for _, t in (buttons or rows or []))
        return await deliver(company_id, contact, body + ('\n\n' + opts if opts else ''), None, by_name)
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0})
    if buttons:
        inter = {'type': 'button', 'body': {'text': body[:1024]},
                 'action': {'buttons': [{'type': 'reply', 'reply': {'id': i[:256], 'title': t[:20]}} for i, t in buttons[:3]]}}
    else:
        inter = {'type': 'list', 'body': {'text': body[:1024]},
                 'action': {'button': list_label[:20], 'sections': [{'title': 'Horários livres', 'rows': [{'id': i[:200], 'title': t[:24]} for i, t in rows[:10]]}]}}
    res = await graph('POST', f'/{n["phone_number_id"]}/messages', decrypt(n['token_enc']),
                      {'messaging_product': 'whatsapp', **wa_to(contact), 'type': 'interactive', 'interactive': inter})
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': contact['id'], 'dir': 'out', 'type': 'interactive', 'text': body,
           'choices': [{'id': i, 'title': t} for i, t in (buttons or rows or [])], 'wa_msg_id': ((res.get('messages') or [{}])[0]).get('id'),
           'status': 'sent', 'by_id': None, 'by': by_name, 'at': now()}
    await db.messages.insert_one(msg.copy())
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'last_at': msg['at'], 'last_text': body[:200], 'last_dir': 'out', 'last_by': by_name}})
    return msg

async def slot_rows(company, service_id, resched_id=None, days=10, per_day=3, total=10):
    import agenda
    s = await agenda.settings_for(company); sv = agenda.service_of(s, service_id)
    tz = agenda.tz_of(company)
    free = await agenda.free_slots(company, sv, now().astimezone(tz).date(), days, limit_per_day=per_day)
    rows = []
    for d in sorted(free):
        for t in free[d]:
            rows.append((f'slot:{service_id}:{t:%Y-%m-%dT%H:%M}' + (f':{resched_id}' if resched_id else ''), f'{agenda.WEEK[t.weekday()]} {t:%d/%m} · {t:%H:%M}'))
            if len(rows) >= total: return rows
    return rows

async def offer_slots(company, contact, service_id, intro, by_name, resched_id=None):
    from whatsapp import deliver
    try: rows = await slot_rows(company, service_id, resched_id)
    except Exception: rows = []
    if not rows:
        await deliver(company['id'], contact, 'Não encontrei horário livre nos próximos dias. Vou chamar alguém do time para ver uma opção com você. 😊', None, by_name)
        from ai import handoff
        await handoff(company['id'], contact, 'sem horário livre para oferecer')
        return False
    await send_choices(company['id'], contact, intro, by_name, rows=rows)
    return True

async def nanda_name(company_id):
    s = await db.ai_settings.find_one({'company_id': company_id}, {'_id': 0, 'name': 1}) or {}
    return (s.get('name') or 'Nanda') + ' (IA)'

async def on_choice(company_id, contact, choice_id, text=''):
    """O cliente tocou num botão ou numa linha da lista. Devolve True se cuidou (aí a Nanda não precisa responder)."""
    import agenda
    from whatsapp import deliver
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    who = await nanda_name(company_id)
    kind, _, rest = (choice_id or '').partition(':')
    if kind == 'ap_ok':
        a = await db.appointments.find_one({'id': rest, 'company_id': company_id, 'contact_id': contact['id']}, {'_id': 0})
        if not a or a['status'] not in agenda.ACTIVE: return False
        await db.appointments.update_one({'id': a['id']}, {'$set': {'status': 'confirmed', 'confirmed_at': now(), 'confirmed_by': 'cliente'}})
        t = aware(a['start']).astimezone(agenda.tz_of(company))
        await note(company_id, contact['id'], f'Horário confirmado pelo cliente no botão do lembrete: {a.get("service_name")} em {t:%d/%m às %H:%M}.')
        await deliver(company_id, contact, f'Confirmado! ✅ Te esperamos {agenda.WEEK[t.weekday()]}, {t:%d/%m}, às {t:%H:%M}.', None, who)
        return True
    if kind == 'ap_re':
        a = await db.appointments.find_one({'id': rest, 'company_id': company_id, 'contact_id': contact['id']}, {'_id': 0})
        if not a or a['status'] not in agenda.ACTIVE: return False
        await note(company_id, contact['id'], f'O cliente pediu para remarcar {a.get("service_name")}. Os horários livres foram enviados para ele escolher.')
        return await offer_slots(company, contact, a['service_id'], 'Sem problema! Escolha um novo horário na lista abaixo. O anterior fica liberado assim que você escolher. 😊', who, resched_id=a['id']) or True
    if kind == 'slot':
        parts = rest.split(':')
        if len(parts) < 3: return False
        service_id, iso, old = parts[0], parts[1] + ':' + parts[2], (parts[3] if len(parts) > 3 else None)
        try: start = datetime.fromisoformat(iso)
        except ValueError: return False
        try: a = await agenda.book(company, contact, service_id, start, None, who)
        except Exception:
            return await offer_slots(company, contact, service_id, 'Esse horário acabou de ser ocupado 😅 Escolha outro, por favor:', who, resched_id=old) or True
        if old:
            o = await db.appointments.find_one({'id': old, 'company_id': company_id}, {'_id': 0})
            if o and o['status'] in agenda.ACTIVE:
                await db.appointments.update_one({'id': old}, {'$set': {'status': 'canceled', 'canceled_at': now(), 'replaced_by': a['id']}})
                try: await agenda.gcal.sync_event({**o, 'status': 'canceled'})
                except Exception: log.exception('google agenda ao remarcar')
        t = aware(a['start']).astimezone(agenda.tz_of(company))
        msg = f'Prontinho! {a["service_name"]} marcado para {agenda.WEEK[t.weekday()]}, {t:%d/%m}, às {t:%H:%M}.' + (f' Com {a["pro_name"]}.' if a.get('pro_name') else '') + ' Até lá! 😊'
        await deliver(company_id, contact, msg, None, who)
        return True
    return False

async def on_text(company_id, contact, text):
    """Resposta escrita a um lembrete enviado por modelo (botão de resposta rápida chega como texto)."""
    import agenda
    if await agenda.confirm_by_reply(company_id, contact['id'], text): return 'confirmed'
    if not text or len(text) > 60 or not RESCHED.match(text): return None
    ap = await db.appointments.find_one({'company_id': company_id, 'contact_id': contact['id'], 'status': {'$in': agenda.ACTIVE}, 'start': {'$gt': now(), '$lte': now() + timedelta(hours=72)}},
                                        {'_id': 0}, sort=[('start', 1)])
    if not ap: return None
    sent = await db.automation_runs.find_one({'company_id': company_id, 'contact_id': contact['id'], 'status': 'sent',
                                              'key': {'$regex': ':' + re.escape(ap['id']) + '$'}, 'at': {'$gte': now() - timedelta(hours=48)}}, {'_id': 1})
    if not sent: return None
    await on_choice(company_id, contact, 'ap_re:' + ap['id'])
    return 'resched'
