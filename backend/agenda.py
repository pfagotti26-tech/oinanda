"""Etapa 5 — Agenda.

Serviços (ex.: aula experimental, avaliação), horário de funcionamento por dia da semana e agendamentos.
A Nanda oferece horários livres e marca sozinha (se o dono permitir). Horários ocupados no Google Agenda
também bloqueiam. Ao agendar, o contato pode andar para uma etapa do funil escolhida pelo dono.
Horários guardados em UTC; a empresa enxerga no fuso dela (padrão America/Sao_Paulo).
"""
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Literal, Dict
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, note, aware
import gcal

router = APIRouter()
WEEK = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']
STATUS = {'scheduled': 'Agendado', 'confirmed': 'Confirmado', 'done': 'Compareceu', 'no_show': 'Faltou', 'canceled': 'Cancelado'}
ACTIVE = ['scheduled', 'confirmed']

def default_settings(segment):
    from niches import niche
    svc = niche(segment)['service']
    week = [['09:00', '18:00']]
    return {'services': [{'id': uid()[:8], 'name': svc[0], 'duration': svc[1], 'capacity': svc[2], 'price': None}],
            'hours': {'0': week, '1': week, '2': week, '3': week, '4': week, '5': [['09:00', '13:00']], '6': []},
            'step': 30, 'min_notice_hours': 2, 'days_ahead': 14, 'nanda_books': True, 'stage_on_book': None}

async def settings_for(company):
    s = await db.agenda_settings.find_one({'company_id': company['id']}, {'_id': 0})
    if not s:
        s = {'company_id': company['id'], **default_settings(company.get('segment'))}
        from funnel import stages_for
        marcada = next((x for x in await stages_for(company) if 'marcad' in x['name'].lower()), None)
        if marcada: s['stage_on_book'] = marcada['id']
        await db.agenda_settings.update_one({'company_id': company['id']}, {'$setOnInsert': s}, upsert=True)
        s = await db.agenda_settings.find_one({'company_id': company['id']}, {'_id': 0})
    return s

def tz_of(company): return ZoneInfo(company.get('timezone') or 'America/Sao_Paulo')

def overlaps(a0, a1, b0, b1): return a0 < b1 and b0 < a1

async def taken(company_id, start, end, skip_id=None):
    q = {'company_id': company_id, 'status': {'$in': ACTIVE}, 'start': {'$lt': end}, 'end': {'$gt': start}}
    rows = await db.appointments.find(q, {'_id': 0, 'id': 1, 'start': 1, 'end': 1}).to_list(2000)
    return [(aware(r['start']), aware(r['end'])) for r in rows if r['id'] != skip_id]

async def free_slots(company, service, day_from, days, limit_per_day=None):
    """Horários livres de um serviço. Devolve {data_local: [datetime_local, ...]}."""
    s = await settings_for(company); tz = tz_of(company)
    dur = timedelta(minutes=service['duration']); step = timedelta(minutes=s.get('step') or 30)
    earliest = now() + timedelta(hours=s.get('min_notice_hours') or 0)
    start_utc = datetime.combine(day_from, dtime(0), tz).astimezone(ZoneInfo('UTC'))
    end_utc = start_utc + timedelta(days=days + 1)
    booked = await taken(company['id'], start_utc, end_utc)
    gbusy = await gcal.busy(company['id'], start_utc, end_utc)
    out = {}
    for i in range(days):
        d = day_from + timedelta(days=i)
        for a, b in s['hours'].get(str(d.weekday()), []):
            t = datetime.combine(d, dtime.fromisoformat(a), tz); stop = datetime.combine(d, dtime.fromisoformat(b), tz)
            while t + dur <= stop:
                if t >= earliest:
                    n = sum(1 for x in booked if overlaps(t, t + dur, *x))
                    if n < (service.get('capacity') or 1) and not any(overlaps(t, t + dur, *x) for x in gbusy):
                        out.setdefault(d.isoformat(), []).append(t)
                t += step
        if limit_per_day and d.isoformat() in out: out[d.isoformat()] = spread(out[d.isoformat()], limit_per_day)
    return out

def spread(items, k):
    if len(items) <= k: return items
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]

def service_of(s, service_id):
    sv = next((x for x in s['services'] if x['id'] == service_id), None)
    if not sv: raise HTTPException(400, 'Serviço não encontrado.')
    return sv

def appt_out(a, tz):
    a = {k: v for k, v in a.items() if k != '_id'}
    a['start'] = aware(a['start']).astimezone(tz).isoformat(); a['end'] = aware(a['end']).astimezone(tz).isoformat()
    a['status_label'] = STATUS.get(a.get('status'))
    return a

async def book(company, contact, service_id, start_local, by_id, by_name, notes=None, check=True):
    """Cria o agendamento conferindo se o horário ainda está livre."""
    s = await settings_for(company); tz = tz_of(company)
    sv = service_of(s, service_id)
    start = start_local if start_local.tzinfo else start_local.replace(tzinfo=tz)
    end = start + timedelta(minutes=sv['duration'])
    if check:
        free = await free_slots(company, sv, start.astimezone(tz).date(), 1)
        if start.astimezone(tz) not in free.get(start.astimezone(tz).date().isoformat(), []):
            raise HTTPException(409, 'Esse horário não está livre.')
    a = {'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'contact_name': contact.get('name'), 'contact_phone': contact.get('wa_id'),
         'service_id': sv['id'], 'service_name': sv['name'], 'start': start.astimezone(ZoneInfo('UTC')), 'end': end.astimezone(ZoneInfo('UTC')),
         'status': 'scheduled', 'notes': notes, 'by_id': by_id, 'by': by_name, 'created_at': now()}
    await db.appointments.insert_one(a.copy())
    when = start.astimezone(tz)
    await note(company['id'], contact['id'], f'{by_name} agendou {sv["name"]} para {WEEK[when.weekday()]} {when:%d/%m às %H:%M}')
    if s.get('stage_on_book'):
        from funnel import stages_for, move_stage
        stages = await stages_for(company)
        if any(x['id'] == s['stage_on_book'] for x in stages):
            await move_stage(company, contact, s['stage_on_book'], by_name, stages)
    await gcal.sync_event(a)
    return a

async def upcoming_for(company_id, contact_id):
    return await db.appointments.find({'company_id': company_id, 'contact_id': contact_id, 'status': {'$in': ACTIVE}, 'start': {'$gte': now()}},
                                      {'_id': 0}).sort('start', 1).to_list(5)

async def nanda_context(company):
    """Texto com serviços e horários livres dos próximos dias, para a Nanda oferecer."""
    s = await settings_for(company)
    if not s.get('nanda_books') or not s.get('services'): return None, s
    tz = tz_of(company); today = now().astimezone(tz).date()
    lines = []
    for sv in s['services']:
        free = await free_slots(company, sv, today, min(s.get('days_ahead') or 7, 6), limit_per_day=6)
        days = [f'{WEEK[datetime.fromisoformat(d).weekday()]} {datetime.fromisoformat(d):%d/%m}: ' + ', '.join(f'{t:%H:%M}' for t in ts) for d, ts in free.items()]
        price = f', R$ {sv["price"]:.0f}'.replace('.', ',') if sv.get('price') else ''
        lines.append(f'- {sv["name"]} (id {sv["id"]}, {sv["duration"]} min{price})\n  ' + ('\n  '.join(days) if days else 'sem horários livres nos próximos dias'))
    return '\n'.join(lines), s

# ---------- configuração ----------
class ServiceIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=60)
    duration: int = Field(ge=10, le=600)
    capacity: int = Field(default=1, ge=1, le=200)
    price: Optional[float] = Field(default=None, ge=0)

class SettingsIn(BaseModel):
    services: Optional[List[ServiceIn]] = Field(default=None, max_length=20)
    hours: Optional[Dict[str, List[List[str]]]] = None
    step: Optional[int] = Field(default=None, ge=5, le=240)
    min_notice_hours: Optional[int] = Field(default=None, ge=0, le=168)
    days_ahead: Optional[int] = Field(default=None, ge=1, le=90)
    nanda_books: Optional[bool] = None
    stage_on_book: Optional[str] = None

@router.get('/agenda/settings')
async def get_settings(user=Depends(current_user)):
    s = await settings_for(user['company']); s['google'] = await gcal.status(user)
    return s

@router.put('/agenda/settings')
async def put_settings(body: SettingsIn, request: Request, user=Depends(managers)):
    upd = body.model_dump(exclude_none=True)
    if 'services' in upd:
        upd['services'] = [{**sv, 'id': sv.get('id') or uid()[:8], 'name': sv['name'].strip()} for sv in upd['services']]
    if 'hours' in upd:
        for k, ranges in upd['hours'].items():
            if k not in [str(i) for i in range(7)]: raise HTTPException(400, 'Dia da semana inválido.')
            for r in ranges:
                try: a, b = dtime.fromisoformat(r[0]), dtime.fromisoformat(r[1])
                except (ValueError, IndexError): raise HTTPException(400, 'Horário inválido. Use 09:00.')
                if a >= b: raise HTTPException(400, f'No {WEEK[int(k)]}, o fim precisa ser depois do início.')
    if 'stage_on_book' in body.model_fields_set and body.stage_on_book in ('', None): upd['stage_on_book'] = None
    await settings_for(user['company'])
    await db.agenda_settings.update_one({'company_id': user['company_id']}, {'$set': upd})
    await audit(request, user, 'Ajustou a agenda', ', '.join(upd))
    return await get_settings(user)

# ---------- horários e agendamentos ----------
@router.get('/agenda/slots')
async def slots(service_id: str, date: str, user=Depends(current_user)):
    s = await settings_for(user['company']); sv = service_of(s, service_id)
    try: d = datetime.fromisoformat(date).date()
    except ValueError: raise HTTPException(400, 'Data inválida.')
    free = await free_slots(user['company'], sv, d, 1)
    return [t.strftime('%H:%M') for t in free.get(d.isoformat(), [])]

@router.get('/appointments')
async def appointments(start: str, end: str, user=Depends(current_user)):
    tz = tz_of(user['company'])
    try:
        a = datetime.combine(datetime.fromisoformat(start).date(), dtime(0), tz); b = datetime.combine(datetime.fromisoformat(end).date(), dtime(0), tz) + timedelta(days=1)
    except ValueError: raise HTTPException(400, 'Período inválido.')
    q = {'company_id': user['company_id'], 'start': {'$gte': a.astimezone(ZoneInfo('UTC')), '$lt': b.astimezone(ZoneInfo('UTC'))}}
    if user['role'] == 'agent':
        mine = [c['id'] for c in await db.contacts.find({'company_id': user['company_id'], 'assigned_to': {'$in': [None, user['id']]}}, {'_id': 0, 'id': 1}).to_list(5000)]
        q['contact_id'] = {'$in': mine}
    rows = await db.appointments.find(q, {'_id': 0}).sort('start', 1).to_list(2000)
    return [appt_out(r, tz) for r in rows]

@router.get('/contacts/{contact_id}/appointments')
async def contact_appointments(contact_id: str, user=Depends(current_user)):
    await get_contact(user, contact_id)
    rows = await db.appointments.find({'company_id': user['company_id'], 'contact_id': contact_id}, {'_id': 0}).sort('start', -1).to_list(50)
    return [appt_out(r, tz_of(user['company'])) for r in rows]

class BookIn(BaseModel):
    contact_id: str
    service_id: str
    date: str
    time: str = Field(pattern=r'^\d{2}:\d{2}$')
    notes: Optional[str] = Field(default=None, max_length=2000)
    force: bool = False  # encaixe: marca mesmo fora do horário livre (só dono e gestor)

@router.post('/appointments')
async def create(body: BookIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, body.contact_id)
    try: start = datetime.fromisoformat(f'{body.date}T{body.time}').replace(tzinfo=tz_of(user['company']))
    except ValueError: raise HTTPException(400, 'Data ou hora inválida.')
    force = body.force and user['role'] in ('owner', 'manager')
    a = await book(user['company'], c, body.service_id, start, user['id'], user.get('impersonated_by') or user['name'], body.notes, check=not force)
    return appt_out(a, tz_of(user['company']))

class ApptPatch(BaseModel):
    status: Optional[Literal['scheduled', 'confirmed', 'done', 'no_show', 'canceled']] = None
    notes: Optional[str] = Field(default=None, max_length=2000)
    date: Optional[str] = None
    time: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')

@router.patch('/appointments/{appt_id}')
async def patch(appt_id: str, body: ApptPatch, request: Request, user=Depends(current_user)):
    a = await db.appointments.find_one({'id': appt_id, 'company_id': user['company_id']}, {'_id': 0})
    if not a: raise HTTPException(404, 'Agendamento não encontrado.')
    c = await get_contact(user, a['contact_id'])
    tz = tz_of(user['company']); upd = {}; who = user.get('impersonated_by') or user['name']
    if body.date and body.time:  # remarcar
        s = await settings_for(user['company']); sv = service_of(s, a['service_id'])
        start = datetime.fromisoformat(f'{body.date}T{body.time}').replace(tzinfo=tz)
        free = await free_slots(user['company'], sv, start.date(), 1)
        others = await taken(user['company_id'], start.astimezone(ZoneInfo('UTC')), (start + timedelta(minutes=sv['duration'])).astimezone(ZoneInfo('UTC')), skip_id=a['id'])
        if start not in free.get(start.date().isoformat(), []) and not (len(others) < (sv.get('capacity') or 1) and user['role'] in ('owner', 'manager')):
            raise HTTPException(409, 'Esse horário não está livre.')
        upd.update(start=start.astimezone(ZoneInfo('UTC')), end=(start + timedelta(minutes=sv['duration'])).astimezone(ZoneInfo('UTC')))
        await note(user['company_id'], c['id'], f'{who} remarcou {a["service_name"]} para {WEEK[start.weekday()]} {start:%d/%m às %H:%M}')
    if body.status and body.status != a.get('status'):
        upd['status'] = body.status
        await note(user['company_id'], c['id'], f'{who} marcou {a["service_name"]} como {STATUS[body.status].lower()}')
    if body.notes is not None: upd['notes'] = body.notes
    if upd:
        await db.appointments.update_one({'id': appt_id}, {'$set': upd})
        a.update(upd); await gcal.sync_event(a)
    return appt_out(a, tz)
