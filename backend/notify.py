"""Aviso no WhatsApp de quem recebe uma conversa transferida.

Sai pelo WhatsApp da própria empresa, para o celular do vendedor. Como o vendedor quase nunca escreveu para o número
da empresa nas últimas 24 h, a Meta exige um modelo aprovado: o gestor cria o modelo "aviso para o time" uma vez
(categoria utilidade, a Meta cobra por envio). Se o vendedor está com o painel aberto, o aviso fica só na tela.
"""
import re, logging, secrets
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, current_user, managers

import whatsapp
from whatsapp import aware, WINDOW, decrypt

router = APIRouter()
log = logging.getLogger('oinanda')
BODY = 'Olá, {{1}}! {{2}} passou para você a conversa com {{3}}. Motivo: {{4}}. Abra o Oi Nanda para responder.'
SAMPLE = ['João', 'Ana', 'Mariana Alves', 'quer fechar o plano anual']
ONLINE = timedelta(minutes=3)

def norm_phone(v):
    d = re.sub(r'\D', '', v or '')
    if len(d) in (10, 11): d = '55' + d
    return d if 12 <= len(d) <= 15 else None

async def number(company_id):
    return await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0})

async def refresh_status(company):
    t = company.get('alert_tpl')
    if not t or t.get('status') == 'APPROVED': return t
    n = await number(company['id'])
    if not n: return t
    try:
        res = await whatsapp.graph('GET', f'/{n["waba_id"]}/message_templates', decrypt(n['token_enc']), params={'name': t['name'], 'fields': 'name,status'})
        row = next((r for r in res.get('data') or [] if r.get('name') == t['name']), None)
        if row and row.get('status') != t.get('status'):
            t['status'] = row['status']
            await db.companies.update_one({'id': company['id']}, {'$set': {'alert_tpl.status': row['status']}})
    except HTTPException as e: log.warning('status do modelo de aviso: %s', e.detail)
    return t

@router.get('/notify')
async def get_notify(user=Depends(current_user)):
    c = await db.companies.find_one({'id': user['company_id']}, {'_id': 0})
    t = await refresh_status(c)
    me = await db.users.find_one({'id': user['id']}, {'_id': 0, 'wa_phone': 1, 'wa_notify': 1, 'away': 1})
    return {'connected': bool(await number(user['company_id'])), 'template': {'status': t.get('status')} if t else None, 'me': me or {}}

@router.post('/notify/template')
async def make_template(user=Depends(managers)):
    c = user['company']
    if (c.get('alert_tpl') or {}).get('status') in ('APPROVED', 'PENDING'): return {'status': c['alert_tpl']['status']}
    n = await number(c['id'])
    if not n: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    name = f'aviso_time_{secrets.token_hex(2)}'
    res = await whatsapp.graph('POST', f'/{n["waba_id"]}/message_templates', decrypt(n['token_enc']),
                               {'name': name, 'language': 'pt_BR', 'category': 'UTILITY',
                                'components': [{'type': 'BODY', 'text': BODY, 'example': {'body_text': [SAMPLE]}}]})
    t = {'name': name, 'status': res.get('status') or 'PENDING', 'meta_id': res.get('id'), 'created_at': now()}
    await db.companies.update_one({'id': c['id']}, {'$set': {'alert_tpl': t}})
    return {'status': t['status']}

class MeNotify(BaseModel):
    wa_phone: Optional[str] = Field(default=None, max_length=30)
    wa_notify: bool = False

@router.put('/me/notify')
async def set_mine(body: MeNotify, user=Depends(current_user)):
    phone = norm_phone(body.wa_phone) if body.wa_phone else None
    if body.wa_phone and not phone: raise HTTPException(400, 'Confira o número: DDD + celular, ex.: (11) 91234-5678.')
    if body.wa_notify and not phone: raise HTTPException(400, 'Informe seu celular para receber o aviso.')
    await db.users.update_one({'id': user['id']}, {'$set': {'wa_phone': phone, 'wa_notify': body.wa_notify}})
    return {'wa_phone': phone, 'wa_notify': body.wa_notify}

async def transfer_alert(company, user_id, contact, by_name, reason):
    """Manda o aviso se a pessoa pediu, não está com o painel aberto e há um jeito permitido pela Meta."""
    u = await db.users.find_one({'id': user_id, 'active': True}, {'_id': 0})
    if not u or not u.get('wa_notify') or not u.get('wa_phone'): return 'off'
    seen = aware(u.get('last_seen'))
    if seen and now() - seen < ONLINE: return 'online'
    n = await number(company['id'])
    if not n: return 'no_number'
    first = (u.get('name') or '').split(' ')[0] or 'time'
    vals = [first, by_name or 'O time', contact.get('name') or 'um cliente', (reason or 'sem motivo informado')[:200]]
    me_as_contact = await db.contacts.find_one({'company_id': company['id'], 'wa_id': u['wa_phone']}, {'_id': 0, 'last_in_at': 1})
    li = aware((me_as_contact or {}).get('last_in_at'))
    tpl = company.get('alert_tpl') or {}
    if li and now() - li < WINDOW:  # o vendedor escreveu para a empresa nas últimas 24 h: texto simples, sem custo de modelo
        payload = {'messaging_product': 'whatsapp', 'to': u['wa_phone'], 'type': 'text',
                   'text': {'body': f'Olá, {vals[0]}! {vals[1]} passou para você a conversa com {vals[2]}. Motivo: {vals[3]}.'}}
    elif tpl.get('status') == 'APPROVED':
        payload = {'messaging_product': 'whatsapp', 'to': u['wa_phone'], 'type': 'template',
                   'template': {'name': tpl['name'], 'language': {'code': 'pt_BR'},
                                'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': v} for v in vals]}]}}
    else: return 'no_template'
    await whatsapp.graph('POST', f'/{n["phone_number_id"]}/messages', decrypt(n['token_enc']), payload)
    await db.notify_log.insert_one({'company_id': company['id'], 'user_id': user_id, 'contact_id': contact['id'], 'kind': payload['type'], 'at': now()})
    return 'sent'
