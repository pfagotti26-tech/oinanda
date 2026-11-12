"""Ferramentas da caixa de conversas: respostas rápidas, nota interna, resolvido e contagens.

- Resposta rápida: texto pronto que o time chama digitando "/" (aceita {primeiro_nome}, {empresa}...).
- Nota interna: fica na conversa só para o time (dir 'note'); nunca vai para o cliente nem para a Nanda.
- Resolvido: tira a conversa da fila "Esperando resposta" até o cliente mandar mensagem de novo.
"""
import re, unicodedata
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user
from whatsapp import get_contact, aware, note

router = APIRouter()

def waiting(c):
    """Cliente falou por último e ninguém respondeu nem marcou como resolvido ou "não precisa responder" depois disso."""
    if c.get('last_dir') != 'in': return False
    li = aware(c.get('last_in_at'))
    for k in ('resolved_at', 'noreply_at'):
        r = aware(c.get(k))
        if r and li and r >= li: return False
    return True

LATE_DEFAULT = 15  # minutos: depois disso fica vermelho

def wait_from(li, hours, tz):
    """Quando a espera começa a contar: na hora da mensagem, ou na abertura seguinte se chegou fora do horário."""
    if not li or not hours or not any(hours.values()): return li
    from datetime import datetime, time
    t = li.astimezone(tz)
    for d in range(8):
        day = (t + timedelta(days=d)).date()
        for a, b in sorted(hours.get(str(day.weekday())) or []):
            o = datetime.combine(day, time.fromisoformat(a), tz); e = datetime.combine(day, time.fromisoformat(b), tz)
            if t < e: return max(t, o)
    return li

async def next_open(company):
    """Texto de quando o atendimento abre de novo ("amanhã às 9h", "segunda às 9h"); None se está aberto agora ou não há horário definido."""
    import agenda
    from datetime import datetime, time
    try: s = await agenda.settings_for(company)
    except Exception: return None
    hours = s.get('hours') if s.get('hours_set', True) else None
    if not hours or not any(hours.values()): return None
    tz = agenda.tz_of(company); t = now().astimezone(tz)
    for d in range(8):
        day = (t + timedelta(days=d)).date()
        for a, b in sorted(hours.get(str(day.weekday())) or []):
            o = datetime.combine(day, time.fromisoformat(a), tz); e = datetime.combine(day, time.fromisoformat(b), tz)
            if o <= t < e: return None
            if t < o:
                h = f'{o:%Hh}' if o.minute == 0 else f'{o:%Hh%M}'
                return ('hoje ' if d == 0 else 'amanhã ' if d == 1 else agenda.WEEK[day.weekday()] + ' ') + 'às ' + h
    return None

async def wait_ctx(company):
    """Horário de atendimento (o da agenda) e limite de cada equipe."""
    import agenda
    try:
        s = await agenda.settings_for(company)
        hours = s.get('hours') if s.get('hours_set', True) else None  # conta nova: só depois que a empresa define o horário
    except Exception: hours = None
    slas = {t['id']: t.get('sla_minutes') for t in await db.teams.find({'company_id': company['id']}, {'_id': 0, 'id': 1, 'sla_minutes': 1}).to_list(200)}
    return {'hours': hours, 'tz': agenda.tz_of(company), 'slas': slas}

def wait_info(c, ctx):
    """Para a tela: desde quando espera (contando horário de atendimento) e o limite para ficar vermelho."""
    if not waiting(c): return None, None
    wf = wait_from(aware(c.get('last_in_at')), ctx['hours'], ctx['tz'])
    return wf, ctx['slas'].get(c.get('team_id')) or LATE_DEFAULT

def visible_q(user):
    q = {'company_id': user['company_id']}
    if user['role'] == 'agent': q['assigned_to'] = {'$in': [None, user['id']]}
    return q

@router.get('/inbox/counts')
async def counts(user=Depends(current_user)):
    await db.users.update_one({'id': user['id']}, {'$set': {'last_seen': now()}})  # quem está com o painel aberto recebe o aviso na tela
    from teams import my_team_ids
    mine = set(await my_team_ids(user))
    rows = await db.contacts.find(visible_q(user), {'_id': 0, 'id': 1, 'name': 1, 'assigned_to': 1, 'last_dir': 1, 'resolved_at': 1, 'last_in_at': 1, 'needs_human': 1,
                                                    'unread': 1, 'team_id': 1, 'noreply_at': 1, 'transfer_seen': 1, 'transfer_by': 1, 'transfer_reason': 1, 'transfer_at': 1}).to_list(5000)
    ctx = await wait_ctx(user['company']); late = 0
    for r in rows:
        wf, lim = wait_info(r, ctx)
        if wf and now() - wf > timedelta(minutes=lim): late += 1
    pend = [{k: r.get(k) for k in ('id', 'name', 'transfer_by', 'transfer_reason', 'transfer_at')} for r in rows
            if r.get('assigned_to') == user['id'] and r.get('transfer_seen') is False]
    return {'team': sum(1 for r in rows if r.get('team_id') in mine), 'team_queue': sum(1 for r in rows if r.get('team_id') in mine and not r.get('assigned_to')),
            'has_team': bool(mine), 'transfers': pend,'all': len(rows), 'mine': sum(1 for r in rows if r.get('assigned_to') == user['id']),
            'unassigned': sum(1 for r in rows if not r.get('assigned_to')), 'waiting': sum(1 for r in rows if waiting(r)), 'late': late,
            'human': sum(1 for r in rows if r.get('needs_human')), 'unread': sum(1 for r in rows if r.get('unread')),
            'sales_pending': await db.sales.count_documents({'company_id': user['company_id'], 'status': 'pending'}) if user['role'] in ('owner', 'manager') else 0,
            'celebrations': [{'key': h['key'], 'text': (f"{h.get('owner_name') or 'A empresa'} {'venceu' if 'lugar' in h['level'] else 'bateu'} "
                                                         + (f"o desafio {h['goal']} ({h['level']})" if 'lugar' in h['level'] else f"{'a supermeta' if h['level'] == 'super' else 'a meta'} {h['goal']}"))}
                             for h in await db.goal_hits.find({'company_id': user['company_id'], 'at': {'$gte': now() - timedelta(minutes=30)}}, {'_id': 0}).to_list(10)]
                            if (user['company'].get('gamify') or {}).get('goals') or (user['company'].get('gamify') or {}).get('game') else []}

# ---------- respostas rápidas ----------
class QuickIn(BaseModel):
    shortcut: str = Field(min_length=1, max_length=30)
    text: str = Field(min_length=1, max_length=2000)

def clean_short(v):
    v = unicodedata.normalize('NFKD', v).encode('ascii', 'ignore').decode()
    v = re.sub(r'[^a-z0-9_-]+', '', v.strip().lstrip('/').lower().replace(' ', '-'))
    if not v: raise HTTPException(400, 'Atalho inválido. Use letras e números, ex.: preco')
    return v

@router.get('/quick-replies')
async def list_quick(user=Depends(current_user)):
    return await db.quick_replies.find({'company_id': user['company_id']}, {'_id': 0}).sort('shortcut', 1).to_list(300)

@router.post('/quick-replies')
async def add_quick(body: QuickIn, request: Request, user=Depends(current_user)):
    sc = clean_short(body.shortcut)
    if await db.quick_replies.find_one({'company_id': user['company_id'], 'shortcut': sc}): raise HTTPException(409, f'Já existe a resposta /{sc}.')
    if await db.quick_replies.count_documents({'company_id': user['company_id']}) >= 200: raise HTTPException(400, 'Limite de 200 respostas rápidas.')
    r = {'id': uid(), 'company_id': user['company_id'], 'shortcut': sc, 'text': body.text.strip(), 'by': user['name'], 'by_id': user['id'], 'created_at': now()}
    await db.quick_replies.insert_one(r.copy())
    await audit(request, user, 'Criou resposta rápida', '/' + sc)
    r.pop('_id', None)
    return r

@router.patch('/quick-replies/{rid}')
async def edit_quick(rid: str, body: QuickIn, user=Depends(current_user)):
    r = await db.quick_replies.find_one({'id': rid, 'company_id': user['company_id']}, {'_id': 0})
    if not r: raise HTTPException(404, 'Resposta não encontrada.')
    sc = clean_short(body.shortcut)
    if sc != r['shortcut'] and await db.quick_replies.find_one({'company_id': user['company_id'], 'shortcut': sc}): raise HTTPException(409, f'Já existe a resposta /{sc}.')
    await db.quick_replies.update_one({'id': rid}, {'$set': {'shortcut': sc, 'text': body.text.strip()}})
    return {**r, 'shortcut': sc, 'text': body.text.strip()}

@router.delete('/quick-replies/{rid}')
async def del_quick(rid: str, user=Depends(current_user)):
    r = await db.quick_replies.find_one({'id': rid, 'company_id': user['company_id']}, {'_id': 0})
    if not r: raise HTTPException(404, 'Resposta não encontrada.')
    if user['role'] == 'agent' and r.get('by_id') != user['id']: raise HTTPException(403, 'Só quem criou, o dono ou o gestor apagam.')
    await db.quick_replies.delete_one({'id': rid})
    return {'ok': True}

@router.post('/conversations/{contact_id}/no-reply')
async def no_reply(contact_id: str, user=Depends(current_user)):
    """Cliente mandou só "ok", "obrigado"...: sai da fila sem marcar como resolvida."""
    c = await get_contact(user, contact_id)
    await db.contacts.update_one({'id': c['id']}, {'$set': {'noreply_at': now(), 'unread': 0, 'needs_human': False}})
    await note(user['company_id'], c['id'], f'{user.get("impersonated_by") or user["name"]} tirou da fila de espera')
    from ai import resume
    await resume(user['company_id'], c, user.get('impersonated_by') or user['name'])
    return {'ok': True}

# ---------- nota interna e resolvido ----------
class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)

@router.post('/conversations/{contact_id}/note')
async def internal_note(contact_id: str, body: NoteIn, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    m = {'id': uid(), 'company_id': user['company_id'], 'contact_id': c['id'], 'dir': 'note', 'type': 'note', 'text': body.text.strip(),
         'by': user.get('impersonated_by') or user['name'], 'by_id': user['id'], 'at': now()}
    await db.messages.insert_one(m.copy())
    m.pop('_id', None)
    return m

class ResolveIn(BaseModel):
    resolved: bool = True

@router.post('/conversations/{contact_id}/resolve')
async def resolve(contact_id: str, body: ResolveIn, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    who = user.get('impersonated_by') or user['name']
    if body.resolved:
        await db.contacts.update_one({'id': c['id']}, {'$set': {'resolved_at': now(), 'needs_human': False, 'unread': 0}})
        from ai import resume
        await resume(user['company_id'], c, user.get('impersonated_by') or user['name'])
        await note(user['company_id'], c['id'], f'{who} marcou a conversa como resolvida')
    else:
        await db.contacts.update_one({'id': c['id']}, {'$set': {'resolved_at': None}})
        await note(user['company_id'], c['id'], f'{who} reabriu a conversa')
    return {'ok': True}
