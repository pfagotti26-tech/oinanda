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
            'step': 30, 'min_notice_hours': 2, 'days_ahead': 14, 'nanda_books': True, 'stage_on_book': None, 'pros': []}

async def settings_for(company):
    s = await db.agenda_settings.find_one({'company_id': company['id']}, {'_id': 0})
    if not s:
        s = {'company_id': company['id'], **default_settings(company.get('segment')), 'hours_set': False}
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
    rows = await db.appointments.find(q, {'_id': 0, 'id': 1, 'start': 1, 'end': 1, 'pro_id': 1}).to_list(2000)
    return [(aware(r['start']), aware(r['end']), r.get('pro_id')) for r in rows if r['id'] != skip_id]

# ---------- profissionais (opcional) ----------
def pros_for(s, service_id):
    """Profissionais ativos que fazem este serviço. Lista vazia = a agenda funciona por vagas, como sempre."""
    return [p for p in (s.get('pros') or []) if p.get('active', True) and (not p.get('services') or service_id in p['services'])]

def pro_of(s, pro_id):
    return next((p for p in (s.get('pros') or []) if p['id'] == pro_id), None)

def in_hours(hours, t, dur):
    """O intervalo [t, t+dur) cabe num horário de trabalho desse dia?"""
    d = t.date()
    for a, b in (hours or {}).get(str(d.weekday()), []):
        o = datetime.combine(d, dtime.fromisoformat(a), t.tzinfo); e = datetime.combine(d, dtime.fromisoformat(b), t.tzinfo)
        if o <= t and t + dur <= e: return True
    return False

def pros_free_at(s, pros, t, dur, booked):
    """Quais destes profissionais estão livres em t (horário próprio + sem agendamento em cima). Agendamentos sem profissional ocupam um lugar."""
    avail = [p for p in pros if in_hours(p.get('hours') or s['hours'], t, dur) and not any(x[2] == p['id'] and overlaps(t, t + dur, x[0], x[1]) for x in booked)]
    anon = sum(1 for x in booked if x[2] is None and overlaps(t, t + dur, x[0], x[1]))
    return avail[:max(0, len(avail) - anon)]

async def free_slots(company, service, day_from, days, limit_per_day=None, pro_id=None):
    """Horários livres de um serviço. Devolve {data_local: [datetime_local, ...]}.
    Com profissionais cadastrados, livre = algum profissional que faz o serviço está livre (ou o pedido, se pro_id vier)."""
    s = await settings_for(company); tz = tz_of(company)
    dur = timedelta(minutes=service['duration']); step = timedelta(minutes=s.get('step') or 30)
    earliest = now() + timedelta(hours=s.get('min_notice_hours') or 0)
    start_utc = datetime.combine(day_from, dtime(0), tz).astimezone(ZoneInfo('UTC'))
    end_utc = start_utc + timedelta(days=days + 1)
    booked = await taken(company['id'], start_utc, end_utc)
    gbusy = await gcal.busy(company['id'], start_utc, end_utc)
    pros = pros_for(s, service['id'])
    if pro_id: pros = [p for p in pros if p['id'] == pro_id]
    if pro_id and not pros: return {}
    out = {}
    for i in range(days):
        d = day_from + timedelta(days=i)
        for a, b in s['hours'].get(str(d.weekday()), []):
            t = datetime.combine(d, dtime.fromisoformat(a), tz); stop = datetime.combine(d, dtime.fromisoformat(b), tz)
            while t + dur <= stop:
                if t >= earliest and not any(overlaps(t, t + dur, x[0], x[1]) for x in gbusy):
                    if pros: ok = bool(pros_free_at(s, pros, t, dur, booked))
                    else: ok = sum(1 for x in booked if overlaps(t, t + dur, x[0], x[1])) < (service.get('capacity') or 1)
                    if ok: out.setdefault(d.isoformat(), []).append(t)
                t += step
        if limit_per_day and d.isoformat() in out: out[d.isoformat()] = spread(out[d.isoformat()], limit_per_day)
    return out

async def pick_pro(company, s, service, start, pro_id=None, skip_id=None):
    """Quem atende neste horário: o pedido (se estiver livre) ou o que tem menos agendamentos no dia. None = agenda sem profissionais."""
    pros = pros_for(s, service['id'])
    if not pros:
        if pro_id and pro_of(s, pro_id): return pro_of(s, pro_id)  # profissional que não faz o serviço, mas o gestor escolheu
        return None
    dur = timedelta(minutes=service['duration'])
    day0 = datetime.combine(start.date(), dtime(0), start.tzinfo).astimezone(ZoneInfo('UTC'))
    booked = await taken(company['id'], day0, day0 + timedelta(days=1), skip_id=skip_id)
    if pro_id:
        p = pro_of(s, pro_id)
        if not p: raise HTTPException(400, 'Profissional não encontrado.')
        if p not in pros_free_at(s, [p], start, dur, booked): raise HTTPException(409, f'{p["name"]} não está livre nesse horário.')
        return p
    free = pros_free_at(s, pros, start, dur, booked)
    if not free: raise HTTPException(409, 'Esse horário não está livre.')
    load = {p['id']: sum(1 for x in booked if x[2] == p['id']) for p in free}
    return min(free, key=lambda p: load[p['id']])

def ranges_text(ts, step):
    """Horários livres de forma compacta e completa: "09:00 às 12:30, 15:00, 16:30 às 19:00".
    Antes a Nanda via só 6 horários espalhados por dia e dizia "não tenho" para um horário livre."""
    if not ts: return 'nenhum'
    out, a, b = [], ts[0], ts[0]
    for t in ts[1:]:
        if (t - b) == timedelta(minutes=step): b = t; continue
        out.append(a if a == b else (a, b)); a = b = t
    out.append(a if a == b else (a, b))
    return ', '.join(f'{x:%H:%M}' if not isinstance(x, tuple) else (f'{x[0]:%H:%M} ou {x[1]:%H:%M}' if (x[1] - x[0]) == timedelta(minutes=step) else f'{x[0]:%H:%M} às {x[1]:%H:%M}') for x in out)

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

async def book(company, contact, service_id, start_local, by_id, by_name, notes=None, check=True, pro_id=None):
    """Cria o agendamento conferindo se o horário ainda está livre (e com quem, se houver profissionais)."""
    s = await settings_for(company); tz = tz_of(company)
    sv = service_of(s, service_id)
    start = start_local if start_local.tzinfo else start_local.replace(tzinfo=tz)
    start = start.astimezone(tz)
    end = start + timedelta(minutes=sv['duration'])
    pro = None
    if check:
        free = await free_slots(company, sv, start.date(), 1, pro_id=pro_id)
        if start not in free.get(start.date().isoformat(), []):
            p = pro_of(s, pro_id) if pro_id else None
            raise HTTPException(409, f'{p["name"]} não está livre nesse horário.' if p else 'Esse horário não está livre.')
        pro = await pick_pro(company, s, sv, start, pro_id)
    elif pro_id: pro = pro_of(s, pro_id)
    a = {'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'contact_name': contact.get('name'), 'contact_phone': contact.get('wa_id'),
         'service_id': sv['id'], 'service_name': sv['name'], 'start': start.astimezone(ZoneInfo('UTC')), 'end': end.astimezone(ZoneInfo('UTC')),
         'pro_id': pro['id'] if pro else None, 'pro_name': pro['name'] if pro else None,
         'status': 'scheduled', 'notes': notes, 'by_id': by_id, 'by': by_name, 'created_at': now()}
    await db.appointments.insert_one(a.copy())
    when = start
    await note(company['id'], contact['id'], f'{by_name} agendou {sv["name"]} para {WEEK[when.weekday()]} {when:%d/%m às %H:%M}' + (f' com {pro["name"]}' if pro else ''))
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
        free = await free_slots(company, sv, today, min(s.get('days_ahead') or 7, 6))
        step = s.get('step') or 30
        days = [f'{WEEK[datetime.fromisoformat(d).weekday()]} {datetime.fromisoformat(d):%d/%m}: ' + ranges_text(ts, step) for d, ts in free.items()]
        price = f', R$ {sv["price"]:.0f}'.replace('.', ',') if sv.get('price') else ''
        lines.append(f'- {sv["name"]} (id {sv["id"]}, {sv["duration"]} min{price})\n  ' + ('\n  '.join(days) if days else 'sem horários livres nos próximos dias'))
    pros = [p for p in (s.get('pros') or []) if p.get('active', True)]
    if pros:
        lines.append('PROFISSIONAIS (se o cliente pedir alguém, ofereça só os horários dessa pessoa e preencha "pro_id" no book; se não pedir, deixe pro_id null que o sistema escolhe quem está livre):')
        for p in pros:
            svs = [x for x in s['services'] if not p.get('services') or x['id'] in p['services']]
            if not svs: continue
            sv = max(svs, key=lambda x: x['duration'])  # o mais longo: toda faixa listada serve para qualquer serviço dessa pessoa
            free = await free_slots(company, sv, today, min(s.get('days_ahead') or 7, 6), pro_id=p['id'])
            days = [f'{WEEK[datetime.fromisoformat(d).weekday()]} {datetime.fromisoformat(d):%d/%m}: ' + ranges_text(ts, s.get('step') or 30) for d, ts in free.items()]
            faz = '' if not p.get('services') or len(p['services']) == len(s['services']) else ' · faz: ' + ', '.join(x['name'] for x in svs)
            lines.append(f'- {p["name"]} (pro_id {p["id"]}{faz})\n  ' + ('\n  '.join(days) if days else 'sem horários livres nos próximos dias'))
    return '\n'.join(lines), s

# ---------- configuração ----------
class ServiceIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=60)
    duration: int = Field(ge=10, le=600)
    capacity: int = Field(default=1, ge=1, le=200)
    price: Optional[float] = Field(default=None, ge=0)

class ProIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=40)
    user_id: Optional[str] = None           # alguém do time (conta nas metas por pessoa)
    color: Optional[str] = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')
    services: List[str] = Field(default_factory=list, max_length=20)  # vazio = todos
    hours: Optional[Dict[str, List[List[str]]]] = None               # None = horário da empresa
    active: bool = True

class SettingsIn(BaseModel):
    services: Optional[List[ServiceIn]] = Field(default=None, max_length=20)
    pros: Optional[List[ProIn]] = Field(default=None, max_length=50)
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
    def check_hours(h):
        for k, ranges in h.items():
            if k not in [str(i) for i in range(7)]: raise HTTPException(400, 'Dia da semana inválido.')
            for r in ranges:
                try: a, b = dtime.fromisoformat(r[0]), dtime.fromisoformat(r[1])
                except (ValueError, IndexError): raise HTTPException(400, 'Horário inválido. Use 09:00.')
                if a >= b: raise HTTPException(400, f'No {WEEK[int(k)]}, o fim precisa ser depois do início.')
    if 'hours' in upd:
        check_hours(upd['hours'])
        upd['hours_set'] = True  # a partir daqui o "chegou fora do horário" vale
    if 'pros' in upd:
        PAL = ['#2A9D8F', '#E76F51', '#5B7DB1', '#C77DFF', '#E9A23B', '#3A86FF', '#8AC926', '#FF6B9D']
        out = []
        for i, p in enumerate(upd['pros']):
            if p.get('hours') is not None: check_hours(p['hours'])
            out.append({**p, 'id': p.get('id') or uid()[:8], 'name': p['name'].strip(), 'color': p.get('color') or PAL[i % len(PAL)]})
        if len({p['name'].lower() for p in out}) < len(out): raise HTTPException(400, 'Dois profissionais com o mesmo nome.')
        upd['pros'] = out
    if 'stage_on_book' in body.model_fields_set and body.stage_on_book in ('', None): upd['stage_on_book'] = None
    await settings_for(user['company'])
    await db.agenda_settings.update_one({'company_id': user['company_id']}, {'$set': upd})
    await audit(request, user, 'Ajustou a agenda', ', '.join(upd))
    return await get_settings(user)

# ---------- horários e agendamentos ----------
@router.get('/agenda/slots')
async def slots(service_id: str, date: str, pro_id: Optional[str] = None, user=Depends(current_user)):
    s = await settings_for(user['company']); sv = service_of(s, service_id)
    try: d = datetime.fromisoformat(date).date()
    except ValueError: raise HTTPException(400, 'Data inválida.')
    free = await free_slots(user['company'], sv, d, 1, pro_id=pro_id or None)
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
    pro_id: Optional[str] = None  # com quem; vazio = quem estiver livre
    force: bool = False  # encaixe: marca mesmo fora do horário livre (só dono e gestor)

@router.post('/appointments')
async def create(body: BookIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, body.contact_id)
    try: start = datetime.fromisoformat(f'{body.date}T{body.time}').replace(tzinfo=tz_of(user['company']))
    except ValueError: raise HTTPException(400, 'Data ou hora inválida.')
    force = body.force and user['role'] in ('owner', 'manager')
    a = await book(user['company'], c, body.service_id, start, user['id'], user.get('impersonated_by') or user['name'], body.notes, check=not force, pro_id=body.pro_id or None)
    return appt_out(a, tz_of(user['company']))

class ApptPatch(BaseModel):
    status: Optional[Literal['scheduled', 'confirmed', 'done', 'no_show', 'canceled']] = None
    notes: Optional[str] = Field(default=None, max_length=2000)
    date: Optional[str] = None
    time: Optional[str] = Field(default=None, pattern=r'^\d{2}:\d{2}$')
    pro_id: Optional[str] = None  # trocar o profissional ('' = tirar)

@router.patch('/appointments/{appt_id}')
async def patch(appt_id: str, body: ApptPatch, request: Request, user=Depends(current_user)):
    a = await db.appointments.find_one({'id': appt_id, 'company_id': user['company_id']}, {'_id': 0})
    if not a: raise HTTPException(404, 'Agendamento não encontrado.')
    c = await get_contact(user, a['contact_id'])
    tz = tz_of(user['company']); upd = {}; who = user.get('impersonated_by') or user['name']
    s = await settings_for(user['company'])
    if body.pro_id is not None and body.pro_id != (a.get('pro_id') or ''):  # trocar quem atende
        p = pro_of(s, body.pro_id) if body.pro_id else None
        if body.pro_id and not p: raise HTTPException(400, 'Profissional não encontrado.')
        if p:
            sv = service_of(s, a['service_id']); st = aware(a['start']).astimezone(tz)
            day0 = datetime.combine(st.date(), dtime(0), tz).astimezone(ZoneInfo('UTC'))
            booked = await taken(user['company_id'], day0, day0 + timedelta(days=1), skip_id=a['id'])
            if p not in pros_free_at(s, [p], st, timedelta(minutes=sv['duration']), booked) and user['role'] not in ('owner', 'manager'):
                raise HTTPException(409, f'{p["name"]} não está livre nesse horário.')
        upd.update(pro_id=p['id'] if p else None, pro_name=p['name'] if p else None)
        await note(user['company_id'], c['id'], f'{who} passou {a["service_name"]} para {p["name"]}' if p else f'{who} tirou o profissional de {a["service_name"]}')
    if body.date and body.time:  # remarcar
        sv = service_of(s, a['service_id'])
        start = datetime.fromisoformat(f'{body.date}T{body.time}').replace(tzinfo=tz)
        pid = upd.get('pro_id', a.get('pro_id'))
        free = await free_slots(user['company'], sv, start.date(), 1, pro_id=pid)
        others = await taken(user['company_id'], start.astimezone(ZoneInfo('UTC')), (start + timedelta(minutes=sv['duration'])).astimezone(ZoneInfo('UTC')), skip_id=a['id'])
        livre = start in free.get(start.date().isoformat(), []) or (pid and start.date() == aware(a['start']).astimezone(tz).date() and not any(x[2] == pid for x in others) and in_hours((pro_of(s, pid) or {}).get('hours') or s['hours'], start, timedelta(minutes=sv['duration'])))
        if not livre and not (len(others) < (sv.get('capacity') or 1) and user['role'] in ('owner', 'manager')):
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
