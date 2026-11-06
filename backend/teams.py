"""Equipes e transferência de conversas.

Equipe: grupo de pessoas com fila própria (ex.: Pré-vendas, Vendas, Suporte, Gerência).
Transferir: qualquer pessoa do time passa a conversa para uma equipe (vai para a fila ou, em rodízio,
direto para alguém dela) ou para uma pessoa, com um motivo que vira nota interna. Quem recebe vê o aviso
até abrir a conversa.

Regras por equipe:
- stages: quando o contato entra numa dessas etapas do funil, a conversa vai para a equipe;
- topics: quando a Nanda passa a conversa para o time e o assunto bate com uma dessas palavras, vai para a equipe;
- handoff_default: equipe que recebe os demais repasses da Nanda;
- sla_minutes: cliente esperando mais que isso na fila da equipe gera alerta para a gerência.
"""
import re, unicodedata
from datetime import timedelta
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import get_contact, note, aware

router = APIRouter()
COLORS = ['#5B7DB1', '#8E6CC9', '#2F9AA0', '#D99A1E', '#C2557A', '#4F8A5B', '#E8603F', '#7A6A58']

def norm(t): return unicodedata.normalize('NFKD', (t or '').lower()).encode('ascii', 'ignore').decode()

async def teams_of(company_id):
    return await db.teams.find({'company_id': company_id}, {'_id': 0}).sort('created_at', 1).to_list(50)

async def my_team_ids(user):
    return [t['id'] for t in await db.teams.find({'company_id': user['company_id'], 'member_ids': user['id']}, {'_id': 0, 'id': 1}).to_list(50)]

class TeamIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    member_ids: List[str] = Field(default_factory=list, max_length=200)
    distribution: Literal['queue', 'roundrobin'] = 'queue'
    stages: List[str] = Field(default_factory=list, max_length=12)
    topics: str = Field(default='', max_length=300)
    handoff_default: bool = False
    sla_minutes: Optional[int] = Field(default=None, ge=5, le=24 * 60)
    max_open: Optional[int] = Field(default=None, ge=1, le=500)
    color: Optional[str] = Field(default=None, pattern=r'^#[0-9A-Fa-f]{6}$')

async def check_team(user, body, skip_id=None):
    users = {u['id'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1}).to_list(500)}
    if any(m not in users for m in body.member_ids): raise HTTPException(400, 'Pessoa não encontrada no time.')
    from funnel import stages_for
    ids = {s['id'] for s in await stages_for(user['company'])}
    if any(s not in ids for s in body.stages): raise HTTPException(400, 'Etapa não existe.')
    others = [t for t in await teams_of(user['company_id']) if t['id'] != skip_id]
    if any(norm(t['name']) == norm(body.name) for t in others): raise HTTPException(409, 'Já existe uma equipe com esse nome.')
    clash = [s for t in others for s in t.get('stages') or [] if s in body.stages]
    if clash: raise HTTPException(409, 'Essa etapa já leva para outra equipe.')
    if body.handoff_default:  # só uma equipe recebe os repasses gerais da Nanda
        await db.teams.update_many({'company_id': user['company_id'], 'id': {'$ne': skip_id}}, {'$set': {'handoff_default': False}})

@router.get('/teams')
async def list_teams(user=Depends(current_user)):
    rows = await teams_of(user['company_id'])
    for t in rows:
        t['mine'] = user['id'] in (t.get('member_ids') or [])
        t['queue'] = await db.contacts.count_documents({'company_id': user['company_id'], 'team_id': t['id'], 'assigned_to': None})
    return rows

@router.post('/teams')
async def add_team(body: TeamIn, request: Request, user=Depends(managers)):
    if await db.teams.count_documents({'company_id': user['company_id']}) >= 20: raise HTTPException(400, 'Limite de 20 equipes.')
    await check_team(user, body)
    n = await db.teams.count_documents({'company_id': user['company_id']})
    t = {'id': uid(), 'company_id': user['company_id'], **body.model_dump(), 'name': body.name.strip(), 'color': body.color or COLORS[n % len(COLORS)],
         'rr': 0, 'created_at': now()}
    await db.teams.insert_one(t.copy())
    await audit(request, user, 'Criou equipe', t['name'])
    t.pop('_id', None)
    return t

@router.put('/teams/{tid}')
async def edit_team(tid: str, body: TeamIn, request: Request, user=Depends(managers)):
    t = await db.teams.find_one({'id': tid, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Equipe não encontrada.')
    await check_team(user, body, tid)
    upd = {**body.model_dump(), 'name': body.name.strip(), 'color': body.color or t.get('color')}
    await db.teams.update_one({'id': tid}, {'$set': upd})
    await audit(request, user, 'Editou equipe', upd['name'])
    return {**t, **upd}

@router.delete('/teams/{tid}')
async def del_team(tid: str, request: Request, user=Depends(managers)):
    t = await db.teams.find_one({'id': tid, 'company_id': user['company_id']}, {'_id': 0})
    if not t: raise HTTPException(404, 'Equipe não encontrada.')
    await db.teams.delete_one({'id': tid})
    await db.contacts.update_many({'company_id': user['company_id'], 'team_id': tid}, {'$set': {'team_id': None}})
    await audit(request, user, 'Apagou equipe', t['name'])
    return {'ok': True}

# ---------- transferir ----------
def is_open(c):
    """Conversa em aberto: nunca foi resolvida ou o cliente voltou a escrever depois."""
    r, li = aware(c.get('resolved_at')), aware(c.get('last_in_at'))
    return not r or bool(li and li > r)

async def open_load(company_id, user_ids):
    rows = await db.contacts.find({'company_id': company_id, 'assigned_to': {'$in': list(user_ids)}}, {'_id': 0, 'assigned_to': 1, 'resolved_at': 1, 'last_in_at': 1}).to_list(20000)
    load = {i: 0 for i in user_ids}
    for c in rows:
        if is_open(c): load[c['assigned_to']] = load.get(c['assigned_to'], 0) + 1
    return load

async def pick_member(team, exclude=None):
    """Rodízio entre quem está ativo, disponível e abaixo do limite de conversas abertas. Ninguém livre: fica na fila."""
    ids = team.get('member_ids') or []
    ok = {u['id'] for u in await db.users.find({'id': {'$in': ids}, 'active': True, 'away': {'$ne': True}}, {'_id': 0, 'id': 1}).to_list(200)}
    cand = [i for i in ids if i in ok and i != exclude]
    if cand and team.get('max_open'):
        load = await open_load(team['company_id'], cand)
        cand = [i for i in cand if load.get(i, 0) < team['max_open']]
    if not cand: return None
    start = team.get('rr') or 0
    order = ids[start % len(ids):] + ids[:start % len(ids)] if ids else []
    pick = next(i for i in order if i in cand)
    await db.teams.update_one({'id': team['id']}, {'$set': {'rr': ids.index(pick) + 1}})
    return pick

async def do_transfer(company, contact, by_name, by_id, team=None, to_user=None, reason='', auto=False):
    """Passa a conversa para uma equipe e/ou pessoa. Devolve a descrição de para onde foi."""
    assignee = to_user['id'] if to_user else (await pick_member(team, by_id) if team and team.get('distribution') == 'roundrobin' else None)
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': company['id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    upd = {'team_id': team['id'] if team else contact.get('team_id'), 'assigned_to': assignee, 'transfer_by': by_name, 'transfer_by_id': by_id,
           'transfer_at': now(), 'transfer_reason': reason, 'transfer_seen': assignee is None}
    if team and not to_user: upd['team_id'] = team['id']
    if by_id and not contact.get('origin_by_id'): upd['origin_by_id'] = by_id  # quem trouxe a oportunidade (crédito do pré-vendas)
    await db.contacts.update_one({'id': contact['id']}, {'$set': upd})
    await db.transfers.insert_one({'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'by_id': by_id, 'by': by_name,
                                   'from_team': contact.get('team_id'), 'to_team': upd['team_id'], 'to_user': assignee, 'reason': reason, 'auto': auto, 'at': now()})
    contact.update(upd)
    where = (team['name'] if team else '') + ((' · ' if team else '') + names.get(assignee, '') if assignee else (' (fila)' if team else ''))
    await note(company['id'], contact['id'], f'{by_name} transferiu para {where}' + (f' · {reason}' if auto and reason else ''))
    if reason and not auto:
        await db.messages.insert_one({'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'dir': 'note', 'type': 'note',
                                      'text': f'Motivo da transferência: {reason}', 'by': by_name, 'by_id': by_id, 'at': now()})
    if assignee and assignee != by_id:
        try:
            from notify import transfer_alert
            await transfer_alert(company, assignee, contact, by_name, reason)
            from push import later, on_transfer
            later(on_transfer(assignee, contact, by_name, reason))
        except Exception as e:  # aviso nunca trava a transferência
            import logging; logging.getLogger('oinanda').warning('aviso de transferência falhou: %s', e)
    return where

class TransferIn(BaseModel):
    team_id: Optional[str] = None
    user_id: Optional[str] = None
    reason: str = Field(min_length=3, max_length=500)
    stage_id: Optional[str] = None

@router.post('/conversations/{contact_id}/transfer')
async def transfer(contact_id: str, body: TransferIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    if not body.team_id and not body.user_id: raise HTTPException(400, 'Escolha uma equipe ou uma pessoa.')
    team = await db.teams.find_one({'id': body.team_id, 'company_id': user['company_id']}, {'_id': 0}) if body.team_id else None
    if body.team_id and not team: raise HTTPException(404, 'Equipe não encontrada.')
    to_user = await db.users.find_one({'id': body.user_id, 'company_id': user['company_id'], 'active': True}, {'_id': 0, 'id': 1, 'name': 1}) if body.user_id else None
    if body.user_id and not to_user: raise HTTPException(404, 'Pessoa não encontrada.')
    if to_user and to_user['id'] == user['id'] and not team: raise HTTPException(400, 'A conversa já pode ficar com você: use Assumir.')
    who = user.get('impersonated_by') or user['name']
    if body.stage_id:
        from funnel import move_stage
        await move_stage(user['company'], c, body.stage_id, who, route=False)
    where = await do_transfer(user['company'], c, who, user['id'], team, to_user, body.reason.strip())
    await audit(request, user, 'Transferiu conversa', f'{c["name"]} → {where}')
    return {'ok': True, 'to': where, 'assigned_to': c.get('assigned_to'), 'team_id': c.get('team_id')}

@router.post('/conversations/{contact_id}/take')
async def take(contact_id: str, user=Depends(current_user)):
    """Pegar a próxima conversa da fila da equipe (ou esta)."""
    c = await get_contact(user, contact_id)
    if c.get('assigned_to') and c['assigned_to'] != user['id'] and user['role'] == 'agent': raise HTTPException(409, 'Outra pessoa já pegou esta conversa.')
    await db.contacts.update_one({'id': c['id']}, {'$set': {'assigned_to': user['id'], 'transfer_seen': True}})
    await note(user['company_id'], c['id'], f'{user.get("impersonated_by") or user["name"]} pegou a conversa')
    return {'ok': True}

# ---------- regras ----------
async def route_by_stage(company, contact, stage_id):
    team = await db.teams.find_one({'company_id': company['id'], 'stages': stage_id}, {'_id': 0})
    if team and contact.get('team_id') != team['id']:
        await do_transfer(company, contact, 'Regra da equipe', None, team, None, 'mudou de etapa', auto=True)

# ---------- disponibilidade ----------
class StatusIn(BaseModel):
    away: bool

@router.put('/me/status')
async def my_status(body: StatusIn, user=Depends(current_user)):
    await db.users.update_one({'id': user['id']}, {'$set': {'away': body.away, 'away_at': now() if body.away else None}})
    return {'away': body.away}

async def route_handoff(company, contact, reason, last_text=''):
    """Para onde vai a conversa que a Nanda passou para o time."""
    text = norm(f'{reason} {last_text}')
    teams = await teams_of(company['id'])
    target = next((t for t in teams if any(w.strip() and norm(w.strip()) in text for w in (t.get('topics') or '').split(','))), None)
    target = target or next((t for t in teams if t.get('handoff_default')), None)
    if target and contact.get('team_id') != target['id']:
        await do_transfer(company, contact, 'Nanda (IA)', None, target, None, reason or 'pediu uma pessoa', auto=True)

async def check_sla():
    """Cliente esperando demais na fila de uma equipe: alerta (uma vez por mensagem do cliente)."""
    from inbox import waiting
    n = 0
    for t in await db.teams.find({'sla_minutes': {'$gt': 0}}, {'_id': 0}).to_list(2000):
        cut = now() - timedelta(minutes=t['sla_minutes'])
        for c in await db.contacts.find({'company_id': t['company_id'], 'team_id': t['id'], 'last_dir': 'in', 'last_in_at': {'$lte': cut}}, {'_id': 0}).to_list(500):
            if not waiting(c) or (aware(c.get('sla_alert_for')) and aware(c['sla_alert_for']) >= aware(c['last_in_at'])): continue
            mins = int((now() - aware(c['last_in_at'])).total_seconds() // 60)
            await db.contacts.update_one({'id': c['id']}, {'$set': {'sla_alert_for': c['last_in_at'], 'sla_alert_at': now()}})
            await note(t['company_id'], c['id'], f'Alerta: esperando há {mins} min na fila de {t["name"]} (limite {t["sla_minutes"]} min)')
            n += 1
    return n
