"""Mensagem agendada: o time escreve agora e o Oi Nanda manda na data e hora combinadas.

Para o caso mais comum do dia a dia: "me chama na segunda". Fica na fila, o relógio das automações olha
a cada minuto e envia pelo canal da conversa.

A janela de cada canal continua valendo. No WhatsApp, se a hora marcada cair fora das 24 h da última
mensagem do cliente, a Meta só aceita modelo aprovado: a tela avisa isso na hora de agendar, e se mesmo
assim a janela fechar antes, a mensagem não sai e vira uma nota interna na conversa.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user
from whatsapp import get_contact, note, aware

log = logging.getLogger('oinanda.later')
router = APIRouter()
MAX_DAYS = 180


def tz_of(company): return ZoneInfo((company or {}).get('timezone') or 'America/Sao_Paulo')


def out(d, company=None):
    r = {k: d.get(k) for k in ('id', 'contact_id', 'text', 'status', 'by', 'error')}
    r['at'] = d['at']
    if company: r['local'] = aware(d['at']).astimezone(tz_of(company)).strftime('%d/%m às %H:%M')
    return r


class LaterIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    at: datetime  # hora local da empresa (o navegador manda sem fuso) ou com fuso


def to_utc(at, company):
    at = at if at.tzinfo else at.replace(tzinfo=tz_of(company))
    return at.astimezone(ZoneInfo('UTC'))


@router.post('/conversations/{contact_id}/later')
async def create(contact_id: str, body: LaterIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    at = to_utc(body.at, user['company'])
    if at <= now() + timedelta(seconds=30): raise HTTPException(400, 'Escolha uma data e hora no futuro.')
    if at > now() + timedelta(days=MAX_DAYS): raise HTTPException(400, f'Dá para agendar até {MAX_DAYS} dias à frente.')
    if await db.scheduled.count_documents({'contact_id': contact_id, 'status': 'queued'}) >= 20:
        raise HTTPException(400, 'Já são 20 mensagens agendadas para este cliente. Envie ou cancele alguma antes.')
    d = {'id': uid(), 'company_id': user['company_id'], 'contact_id': contact_id, 'text': body.text.strip(), 'at': at,
         'status': 'queued', 'by_id': user['id'], 'by': user.get('impersonated_by') or user['name'], 'created_at': now()}
    await db.scheduled.insert_one(d.copy())
    await audit(request, user, f'Agendou uma mensagem para {c["name"]}', out(d, user['company'])['local'])
    return out(d, user['company'])


@router.get('/conversations/{contact_id}/later')
async def listar(contact_id: str, user=Depends(current_user)):
    await get_contact(user, contact_id)
    rows = await db.scheduled.find({'contact_id': contact_id, 'company_id': user['company_id'], 'status': 'queued'}, {'_id': 0}).sort('at', 1).to_list(20)
    return [out(d, user['company']) for d in rows]


@router.delete('/later/{sid}')
async def cancelar(sid: str, request: Request, user=Depends(current_user)):
    d = await db.scheduled.find_one({'id': sid, 'company_id': user['company_id']}, {'_id': 0})
    if not d or d['status'] != 'queued': raise HTTPException(404, 'Esta mensagem agendada não existe mais.')
    if user['role'] == 'agent' and d.get('by_id') != user['id']: raise HTTPException(403, 'Só quem agendou (ou o gestor) cancela.')
    await db.scheduled.update_one({'id': sid}, {'$set': {'status': 'canceled', 'canceled_at': now(), 'canceled_by': user['id']}})
    await audit(request, user, 'Cancelou uma mensagem agendada')
    return {'ok': True}


# ---------- relógio ----------
async def tick(limit=50):
    """Chamado a cada volta do relógio das automações."""
    from whatsapp import deliver
    due = await db.scheduled.find({'status': 'queued', 'at': {'$lte': now()}}, {'_id': 0}).sort('at', 1).to_list(limit)
    for d in due:
        c = None
        try:
            c = await db.contacts.find_one({'id': d['contact_id'], 'company_id': d['company_id']}, {'_id': 0})
            if not c:
                await db.scheduled.update_one({'id': d['id']}, {'$set': {'status': 'failed', 'error': 'o cliente não está mais na lista', 'done_at': now()}})
                continue
            if c.get('opt_out'):
                await fail(d, c, 'o cliente pediu para não receber mensagens')
                continue
            await deliver(d['company_id'], c, d['text'], d.get('by_id'), f'{d.get("by") or "Time"} · agendada')
            await db.scheduled.update_one({'id': d['id']}, {'$set': {'status': 'sent', 'done_at': now()}})
        except HTTPException as e:
            await fail(d, c, str(e.detail))
        except Exception as e:
            log.exception('falha ao enviar mensagem agendada')
            await fail(d, None, 'erro no envio')


async def fail(d, contact, why):
    await db.scheduled.update_one({'id': d['id']}, {'$set': {'status': 'failed', 'error': why[:300], 'done_at': now()}})
    try:
        await note(d['company_id'], d['contact_id'], f'A mensagem agendada não saiu: {why} — "{(d.get("text") or "")[:120]}"')
    except Exception: log.exception('falha ao registrar a nota da mensagem agendada')
