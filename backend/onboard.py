"""Chamada de onboarding: o cliente novo escolhe um horário livre do time de treinamento do Oi Nanda e já fica marcado.

Os horários vêm da agenda da própria conta do Oi Nanda (a empresa do admin da plataforma), serviço "Onboarding Oi Nanda"
(criado na primeira vez, 30 minutos, interno: a Nanda dessa conta não oferece para os clientes dela). Quem atende é quem
estiver livre entre os profissionais da agenda; o time troca à vontade pela própria Agenda. "Nenhum horário serve" vira um
pedido que aparece para o admin combinar outro horário.
"""
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, uid, owners, admin, ADMIN_EMAIL, audit
import mailer

router = APIRouter()
SVC = 'onboarding'
WEEK = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']

async def platform_company():
    st = await db.settings.find_one({'id': 'platform'}, {'_id': 0, 'onboard_company_id': 1}) or {}
    cid = st.get('onboard_company_id')
    if not cid and ADMIN_EMAIL:
        u = await db.users.find_one({'email': ADMIN_EMAIL}, {'_id': 0, 'company_id': 1})
        cid = u and u.get('company_id')
    return await db.companies.find_one({'id': cid}, {'_id': 0}) if cid else None

async def ensure_service(co):
    from agenda import settings_for
    s = await settings_for(co)
    if not any(x['id'] == SVC for x in s['services']):
        await db.agenda_settings.update_one({'company_id': co['id']}, {'$push': {'services': {'id': SVC, 'name': 'Onboarding Oi Nanda', 'duration': 30, 'capacity': 1, 'price': None, 'internal': True}}})
        s = await settings_for(co)
    return s, next(x for x in s['services'] if x['id'] == SVC)

def label(t, today):
    d = (t.date() - today).days
    day = 'Hoje' if d == 0 else 'Amanhã' if d == 1 else WEEK[t.weekday()].capitalize()
    return f'{day}, {t:%d/%m} · {t.hour}h' + (f'{t.minute:02d}' if t.minute else '')

async def slots(n=6):
    co = await platform_company()
    if not co: return None, []
    from agenda import free_slots, tz_of, spread
    _, sv = await ensure_service(co)
    tz = tz_of(co); today = now().astimezone(tz).date()
    free = await free_slots(co, sv, today, 10)
    out = []
    for d in sorted(free):
        out += spread(free[d], 2)
        if len(out) >= n: break
    return co, [{'start': t.isoformat(), 'label': label(t, today)} for t in out[:n]]

@router.get('/onboard')
async def get_onboard(user=Depends(owners)):
    c = user['company']; ob = c.get('onboard') or {}
    _, sl = await slots()
    return {'booked': ob.get('booked'), 'requested': ob.get('requested'), 'slots': sl}

class BookIn(BaseModel):
    start: Optional[str] = None  # horário escolhido (ISO); vazio = nenhum serve
    note: str = Field(default='', max_length=300)

def phone_of(c, u):
    from channels import norm_phone
    return norm_phone(c.get('notice_phone') or u.get('wa_phone') or c.get('phone') or '') or ''

@router.post('/onboard')
async def book_onboard(body: BookIn, request: Request, user=Depends(owners)):
    c = user['company']
    co = await platform_company()
    if not co: raise HTTPException(503, 'A agenda do time ainda não está pronta. Fale com o suporte.')
    if not body.start:
        req = {'at': now(), 'note': body.note.strip()}
        await db.companies.update_one({'id': c['id']}, {'$set': {'onboard.requested': req}})
        await db.onboard_requests.insert_one({'id': uid(), 'company_id': c['id'], 'company': c.get('name'), 'name': user['name'], 'email': user['email'], 'phone': phone_of(c, user),
                                              'note': body.note.strip(), 'status': 'open', 'at': now()})
        await audit(request, user, 'Pediu outro horário de onboarding', body.note[:120])
        try:
            import owneralerts
            await owneralerts.send('onboard_req', f'Pedido de onboarding: {c.get("name")}', {'title': 'Pediu outro horário de onboarding', 'body': [f'{user["name"]} ({user["email"]}), da {c.get("name")}, não achou horário e pediu para combinar outro.'] + ([f'Recado: {body.note.strip()}'] if body.note.strip() else [])}, cid=c['id'])
        except Exception: pass
        return {'requested': req}
    from agenda import book, tz_of
    _, sv = await ensure_service(co)
    try: start = datetime.fromisoformat(body.start)
    except ValueError: raise HTTPException(400, 'Horário inválido.')
    phone = phone_of(c, user)
    ct = await db.contacts.find_one({'company_id': co['id'], '$or': [{'onboard_company_id': c['id']}] + ([{'wa_id': phone}] if phone else [])}, {'_id': 0})
    if not ct:
        ct = {'id': uid(), 'company_id': co['id'], 'name': user['name'], 'wa_id': phone or None, 'email': user['email'], 'onboard_company_id': c['id'],
              'notes': f'Cliente do Oi Nanda: {c.get("name")}', 'created_at': now(), 'source': 'onboarding'}
        await db.contacts.insert_one(ct.copy())
    a = await book(co, ct, SVC, start, None, 'Cadastro do cliente', notes=f'Onboarding · {c.get("name")} · {user["email"]}')
    tz = tz_of(co); st = a['start'].astimezone(tz)
    booked = {'appt_id': a['id'], 'start': a['start'], 'pro_name': a.get('pro_name'), 'label': label(st, now().astimezone(tz).date())}
    await db.companies.update_one({'id': c['id']}, {'$set': {'onboard.booked': booked}, '$unset': {'onboard.requested': ''}})
    await audit(request, user, 'Marcou a chamada de onboarding', booked['label'])
    who = f' com {a["pro_name"].split(" ")[0]}' if a.get('pro_name') else ''
    html = mailer.layout('Sua chamada está marcada', [f'{user["name"].split(" ")[0]}, a chamada para ligar a Nanda no WhatsApp da {c.get("name")} ficou para <b>{booked["label"]}</b>{who}, por vídeo, 30 minutos.',
                         'O link chega por e-mail perto do horário. Deixe por perto o celular do WhatsApp do negócio e o login do Facebook da empresa.'],
                         ('Abrir o Oi Nanda', mailer.APP + '/'))
    await mailer.send(user['email'], f'Chamada marcada: {booked["label"]}', html, f'Sua chamada de onboarding ficou para {booked["label"]}{who}.')
    try:
        import owneralerts
        await owneralerts.send('onboard_book', f'Onboarding marcado: {c.get("name")} · {booked["label"]}', {'title': 'Chamada de onboarding marcada', 'body': [f'{user["name"]} ({user["email"]}), da {c.get("name")}, marcou para {booked["label"]}{who}. Já está na Agenda.']}, cid=c['id'])
    except Exception: pass
    return {'booked': booked}

@router.get('/admin/onboarding')
async def admin_list(user=Depends(admin)):
    return await db.onboard_requests.find({'status': 'open'}, {'_id': 0}).sort('at', -1).to_list(100)

@router.post('/admin/onboarding/{rid}/done')
async def admin_done(rid: str, user=Depends(admin)):
    await db.onboard_requests.update_one({'id': rid}, {'$set': {'status': 'done', 'done_at': now()}})
    return {'ok': True}
