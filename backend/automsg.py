"""Mensagens automáticas: boas-vindas, fora do horário, ao encerrar e assinatura com o nome de quem atende.
Saem só quando a Nanda não está respondendo sozinha naquela conversa (com ela ligada, quem cuida disso é ela)."""
import logging
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, audit, current_user, managers
from whatsapp import aware

log = logging.getLogger('oinanda.automsg')
router = APIRouter()

TEXTS = {'welcome': 'Oi, {primeiro_nome}! Que bom te ver por aqui 😊 Já vou te atender.',
         'away': 'Oi, {primeiro_nome}! Agora estamos fechados. Voltamos {abertura} e te respondemos primeiro. 😊',
         'bye': 'Obrigada pelo contato, {primeiro_nome}! Qualquer coisa, é só chamar por aqui. 💛'}
AWAY_EVERY = timedelta(hours=12)  # o mesmo cliente recebe o aviso de fechado no máximo uma vez a cada 12 h
WHO = 'Mensagem automática'

def cfg_of(company):
    s = company.get('automsg') or {}
    out = {k: {'on': bool((s.get(k) or {}).get('on')), 'text': (s.get(k) or {}).get('text') or TEXTS[k]} for k in TEXTS}
    out['sign'] = bool(s.get('sign'))
    return out

def fill(t, c, co, abertura=''):
    first = ((c.get('name') or '').split(' ') or [''])[0]
    if not first or first.lstrip('+').isdigit(): first = ''  # sem nome salvo: não chama o cliente pelo número
    t = t.replace('{primeiro_nome}', first).replace('{nome}', c.get('name') or '').replace('{empresa}', co.get('name') or '').replace('{abertura}', abertura or 'em breve')
    return t.replace('Oi, !', 'Oi!').replace(', !', '!').replace('Oi, ,', 'Oi,').replace(' ,', ',')

def hours_text(hours):
    """{'0': [['09:00','18:00']], ...} -> 'Seg a sex, 9h às 18h · sáb, 9h às 13h'."""
    from agenda import WEEK
    def h(x): a, b = x.split(':'); return f'{int(a)}h' + (b if b != '00' else '')
    groups = []
    for d in range(7):
        sl = ' e '.join(f'{h(a)} às {h(b)}' for a, b in sorted(hours.get(str(d)) or []))
        if not sl: continue
        if groups and groups[-1][2] == sl and groups[-1][1] == d - 1: groups[-1][1] = d
        else: groups.append([d, d, sl])
    parts = [(WEEK[a] if a == b else f'{WEEK[a]} a {WEEK[b]}') + ', ' + sl for a, b, sl in groups]
    t = ' · '.join(parts)
    return t[:1].upper() + t[1:]

async def hours_of(company):
    import agenda
    try: s = await agenda.settings_for(company)
    except Exception: return None
    hours = s.get('hours') if s.get('hours_set', True) else None
    return hours if hours and any(hours.values()) else None

@router.get('/automsg')
async def get_cfg(user=Depends(current_user)):
    from inbox import next_open
    co = user['company']; hours = await hours_of(co)
    return {**cfg_of(co), 'hours': hours_text(hours) if hours else '', 'next_open': (await next_open(co)) or '', 'defaults': TEXTS}

class Part(BaseModel):
    on: Optional[bool] = None
    text: Optional[str] = Field(default=None, max_length=700)

class CfgIn(BaseModel):
    welcome: Optional[Part] = None
    away: Optional[Part] = None
    bye: Optional[Part] = None
    sign: Optional[bool] = None

@router.put('/automsg')
async def put_cfg(body: CfgIn, request: Request, user=Depends(managers)):
    cur = cfg_of(user['company']); d = body.model_dump(exclude_none=True)
    for k in TEXTS:
        if k in d:
            if 'text' in d[k] and not d[k]['text'].strip(): raise HTTPException(400, 'A mensagem não pode ficar vazia.')
            cur[k] = {**cur[k], **{x: (v.strip() if isinstance(v, str) else v) for x, v in d[k].items()}}
    if 'sign' in d: cur['sign'] = d['sign']
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'automsg': cur}})
    await audit(request, user, 'Ajustou as mensagens automáticas', ', '.join(d))
    return await get_cfg({**user, 'company': {**user['company'], 'automsg': cur}})

async def send(company, contact, text, what):
    from whatsapp import deliver, note
    try:
        await deliver(company['id'], contact, text, None, WHO)
        return True
    except HTTPException as e: await note(company['id'], contact['id'], f'{what} não enviada: {e.detail}')
    except Exception:
        log.exception('falha na mensagem automática'); await note(company['id'], contact['id'], f'{what} não enviada.')
    return False

async def on_quiet(company_id, contact):
    """Mensagem nova e a Nanda não vai responder sozinha: fora do horário avisa quando volta; cliente novo recebe as boas-vindas."""
    co = await db.companies.find_one({'id': company_id}, {'_id': 0}) or {}
    cfg = cfg_of(co)
    if not (cfg['welcome']['on'] or cfg['away']['on']): return None
    c = await db.contacts.find_one({'id': contact['id']}, {'_id': 0}) or contact
    new = not c.get('automsg_welcome_at') and not await db.messages.find_one({'contact_id': c['id'], 'dir': 'out'}, {'_id': 1})
    if cfg['away']['on']:
        from inbox import next_open
        nx = await next_open(co)
        last = aware(c.get('automsg_away_at'))
        if nx and (not last or now() - last >= AWAY_EVERY):
            if await send(co, c, fill(cfg['away']['text'], c, co, nx), 'Mensagem de fora do horário'):
                await db.contacts.update_one({'id': c['id']}, {'$set': {'automsg_away_at': now(), **({'automsg_welcome_at': now()} if new else {})}})
                return 'away'
            return None
        if nx: return None  # fechado e o aviso já foi: não manda boas-vindas por cima
    if new and cfg['welcome']['on']:
        if await send(co, c, fill(cfg['welcome']['text'], c, co), 'Mensagem de boas-vindas'):
            await db.contacts.update_one({'id': c['id']}, {'$set': {'automsg_welcome_at': now()}})
            return 'welcome'
    return None

async def on_resolve(user, contact, survey_sent):
    """Conversa encerrada pelo time: agradece, a menos que a pesquisa de satisfação já tenha ido."""
    co = user['company']; cfg = cfg_of(co)
    if not cfg['bye']['on'] or survey_sent == 'enviada': return None
    return 'enviada' if await send(co, contact, fill(cfg['bye']['text'], contact, co), 'Mensagem de encerramento') else 'falhou'

def signed(company, user, contact, text):
    """Assinar com o nome: '*Júlia:*' no WhatsApp (negrito) e 'Júlia:' nos outros canais."""
    if not cfg_of(company)['sign']: return text
    first = ((user.get('name') or '').split(' ') or [''])[0]
    if not first or text.lstrip().startswith((first + ':', '*' + first)): return text
    from channels import view
    ch = view(contact).get('channel') or 'whatsapp'
    return (f'*{first}:*\n' if ch == 'whatsapp' else f'{first}:\n') + text
