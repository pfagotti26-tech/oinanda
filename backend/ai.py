"""Etapa 3 — a Nanda (IA) responde no WhatsApp.

Modos por conversa: 'auto' (responde sozinha), 'copilot' (só sugere para o time), 'off' (pausada).
Cada empresa ensina a Nanda: o que ela sabe (preços, horários, regras), tom e quando chamar o time.
Quando o cliente pede uma pessoa, reclama ou pergunta algo fora do que ela sabe, a Nanda passa a conversa
para o time (needs_human) e pausa naquela conversa.

.env: ANTHROPIC_API_KEY, AI_MODEL (padrão claude-haiku-5-5).
"""
import os, re, json, asyncio, logging
from typing import Optional, Literal, List
import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
import whatsapp
from whatsapp import get_contact, deliver, note
from datetime import datetime
import agenda

log = logging.getLogger('oinanda.ai')
router = APIRouter()
def debounce(): return float(os.environ.get('AI_DEBOUNCE', '4'))  # espera o cliente terminar de digitar (várias mensagens seguidas)
HUMAN_WORDS = re.compile(r'\b(atendente|humano|humana|pessoa de verdade|falar com (alg[uú]em|uma pessoa|o dono|a dona|gerente)|reclama|procon|absurdo)\b', re.I)

DEFAULTS = {'name': 'Nanda', 'tone': 'proximo', 'knowledge': '', 'handoff': '', 'default_mode': 'copilot', 'signature': True}
TONES = {'proximo': 'próximo, simpático e direto, como uma boa atendente de WhatsApp', 'formal': 'educado e mais formal',
         'descontraido': 'descontraído e animado, com no máximo um emoji por mensagem'}

async def settings_for(company_id):
    s = await db.ai_settings.find_one({'company_id': company_id}, {'_id': 0}) or {}
    return {**DEFAULTS, **s, 'company_id': company_id}

def key_ok(): return bool(os.environ.get('ANTHROPIC_API_KEY'))

async def llm(system, messages, max_tokens=500):
    """Chamada ao Claude. Isolada para os testes substituírem. Devolve (texto, uso)."""
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key: raise HTTPException(400, 'A chave de IA ainda não foi configurada no servidor.')
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post('https://api.anthropic.com/v1/messages', headers={'x-api-key': key, 'anthropic-version': '2023-06-01'},
                         json={'model': os.environ.get('AI_MODEL', 'claude-haiku-5-5'), 'max_tokens': max_tokens, 'system': system, 'messages': messages})
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise HTTPException(502, 'A IA recusou: ' + ((data.get('error') or {}).get('message') or f'erro {r.status_code}'))
    text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
    return text, data.get('usage') or {}

def build_system(s, company, contact_name, agenda_text=None, upcoming=None):
    tz = agenda.tz_of(company); t = now().astimezone(tz)
    ag = ''
    if agenda_text:
        ag = f"""
AGENDA (horários livres de verdade, já descontando o que está ocupado):
{agenda_text}
- Ofereça no máximo 3 opções por vez, perguntando antes qual dia ou período fica melhor.
- Só agende quando o cliente escolher um serviço e um horário DESTA lista. Aí preencha "book" e confirme na reply (serviço, dia e hora).
- Nunca ofereça horário fora da lista. Se nenhum servir, chame o time.
- Para cancelar ou remarcar, chame o time.
"""
        if upcoming:
            ag += 'Este cliente já tem agendado: ' + '; '.join(f"{a['service_name']} {agenda.WEEK[agenda.aware(a['start']).astimezone(tz).weekday()]} {agenda.aware(a['start']).astimezone(tz):%d/%m %H:%M}" for a in upcoming) + '\n'
    return f"""Hoje é {agenda.WEEK[t.weekday()]} {t:%d/%m/%Y}, {t:%H:%M}.
Você é {s['name']}, assistente de atendimento e vendas do negócio "{company['name']}" no WhatsApp.
Fale em português do Brasil, num tom {TONES.get(s['tone'], TONES['proximo'])}. Mensagens curtas (até 3 frases), como no WhatsApp.
O cliente se chama {contact_name or 'cliente'}; use o primeiro nome com naturalidade.

O QUE VOCÊ SABE SOBRE O NEGÓCIO (única fonte de verdade):
<conhecimento>
{s['knowledge'].strip() or '(o dono ainda não preencheu)'}
</conhecimento>

QUANDO CHAMAR O TIME:
{s['handoff'].strip() or 'Quando o cliente pedir para falar com uma pessoa, reclamar, ou quando a resposta não estiver no conhecimento.'}

REGRAS QUE VOCÊ NUNCA QUEBRA:
- Nunca invente preço, horário, condição ou promessa que não esteja no conhecimento. Se não souber, chame o time.
- Se o cliente pedir uma pessoa, estiver irritado ou o assunto for sensível (saúde, reclamação, cancelamento), chame o time.
- Se perguntarem se você é robô, diga com naturalidade que é a assistente virtual do {company['name']} e que pode chamar alguém do time.
- Seu objetivo é ajudar e levar ao próximo passo (agendar, fechar, tirar a dúvida), sem pressionar.

{ag}
Responda SOMENTE com um JSON, sem nada antes ou depois:
{{"reply": "texto para o cliente (ou vazio)", "handoff": true|false, "reason": "motivo curto se handoff", "book": null ou {{"service_id": "id", "start": "AAAA-MM-DDTHH:MM"}}}}
Se handoff for true, a reply deve avisar com gentileza que alguém do time vai continuar (ex.: "Vou chamar a Camila, ela já te responde!")."""

def parse(text):
    m = re.search(r'\{.*\}', text or '', re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            b = d.get('book') if isinstance(d.get('book'), dict) and d['book'].get('service_id') and d['book'].get('start') else None
            return {'reply': str(d.get('reply') or '').strip(), 'handoff': bool(d.get('handoff')), 'reason': str(d.get('reason') or '')[:200], 'book': b}
        except ValueError: pass
    return {'reply': (text or '').strip(), 'handoff': False, 'reason': '', 'book': None}

async def history(contact_id, limit=20):
    rows = await db.messages.find({'contact_id': contact_id, 'dir': {'$in': ['in', 'out']}}, {'_id': 0, 'dir': 1, 'text': 1}).sort('at', -1).to_list(limit)
    msgs = []
    for r in reversed(rows):
        role = 'user' if r['dir'] == 'in' else 'assistant'
        if msgs and msgs[-1]['role'] == role: msgs[-1]['content'] += '\n' + (r.get('text') or '')
        else: msgs.append({'role': role, 'content': r.get('text') or ''})
    while msgs and msgs[0]['role'] != 'user': msgs.pop(0)
    return msgs

async def track(company_id, usage, kind):
    month = now().strftime('%Y-%m')
    await db.ai_usage.update_one({'company_id': company_id, 'month': month},
        {'$inc': {'input_tokens': usage.get('input_tokens', 0), 'output_tokens': usage.get('output_tokens', 0), kind: 1}}, upsert=True)

async def system_for(s, company, contact=None):
    try: agenda_text, _ = await agenda.nanda_context(company)
    except Exception: log.exception('falha ao montar a agenda'); agenda_text = None
    upcoming = await agenda.upcoming_for(company['id'], contact['id']) if contact and agenda_text else None
    name = (contact.get('name') or '').split(' ')[0] if contact else 'Cliente'
    return build_system(s, company, name, agenda_text, upcoming)

async def think(company_id, contact):
    s = await settings_for(company_id)
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    msgs = await history(contact['id'])
    if not msgs: return parse(''), {}
    text, usage = await llm(await system_for(s, company, contact), msgs)
    return parse(text), usage

def describe_book(company, b):
    try:
        t = datetime.fromisoformat(b['start'])
        return f"{agenda.WEEK[t.weekday()]} {t:%d/%m às %H:%M}"
    except Exception: return b.get('start')

# ---------- resposta automática ----------
async def handoff(company_id, contact, reason):
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'needs_human': True, 'ai_mode': 'off', 'needs_human_at': now(), 'handoff_reason': reason}})
    await note(company_id, contact['id'], f'A Nanda passou a conversa para o time · {reason or "cliente quer falar com uma pessoa"}')

async def auto_reply(company_id, contact_id, msg_id):
    await asyncio.sleep(debounce())
    try:
        contact = await db.contacts.find_one({'id': contact_id}, {'_id': 0})
        if not contact or contact.get('needs_human'): return
        s = await settings_for(company_id)
        mode = contact.get('ai_mode') or s['default_mode']
        # o horário da Meta vem em segundos; a ordem real é a de chegada no servidor
        last = (await db.messages.find({'contact_id': contact_id, 'dir': 'in'}, {'_id': 0}).sort('received_at', -1).to_list(1) or [None])[0]
        if not last or last['id'] != msg_id: return  # chegou mensagem mais nova: a resposta sai na vez dela
        if await db.messages.find_one({'contact_id': contact_id, 'dir': 'out', 'at': {'$gt': last['received_at']}}): return  # alguém já respondeu
        asks_human = bool(HUMAN_WORDS.search(last.get('text') or ''))
        if mode != 'auto' or not key_ok():
            if asks_human:  # mesmo sem a Nanda respondendo, o time fica sabendo
                await db.contacts.update_one({'id': contact_id}, {'$set': {'needs_human': True, 'needs_human_at': now(), 'handoff_reason': 'cliente pediu uma pessoa'}})
            return
        if asks_human:
            await deliver(company_id, contact, 'Claro! Vou chamar alguém do time para continuar com você. 😊', None, s['name'] + ' (IA)')
            return await handoff(company_id, contact, 'cliente pediu uma pessoa')
        d, usage = await think(company_id, contact)
        await track(company_id, usage, 'replies')
        if d['book']:
            company = await db.companies.find_one({'id': company_id}, {'_id': 0})
            try:
                await agenda.book(company, contact, d['book']['service_id'], datetime.fromisoformat(d['book']['start']), None, s['name'] + ' (IA)')
            except (HTTPException, ValueError):
                d['reply'] = 'Opa, esse horário acabou de ser ocupado 😅 Quer que eu veja outra opção pra você?'
        if d['reply']: await deliver(company_id, contact, d['reply'], None, s['name'] + ' (IA)')
        if d['handoff']: await handoff(company_id, contact, d['reason'])
    except Exception:
        log.exception('falha na resposta automática')
        try: await note(company_id, contact_id, 'A Nanda não conseguiu responder agora. Alguém do time precisa assumir.')
        except Exception: pass

async def on_human_send(user, contact):
    """Alguém do time respondeu: a Nanda para de responder sozinha nessa conversa."""
    s = await settings_for(user['company_id'])
    if (contact.get('ai_mode') or s['default_mode']) == 'auto':
        await db.contacts.update_one({'id': contact['id']}, {'$set': {'ai_mode': 'copilot'}})
        await note(user['company_id'], contact['id'], f'{user.get("impersonated_by") or user["name"]} assumiu a conversa · a Nanda passou para copiloto')
whatsapp.ON_HUMAN_SEND = on_human_send

_tasks = set()
def on_inbound(company_id, contact_id, msg_id):
    t = asyncio.get_running_loop().create_task(auto_reply(company_id, contact_id, msg_id))
    _tasks.add(t); t.add_done_callback(_tasks.discard)
whatsapp.ON_INBOUND = on_inbound

# ---------- configuração ----------
class SettingsIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=30)
    tone: Optional[Literal['proximo', 'formal', 'descontraido']] = None
    knowledge: Optional[str] = Field(default=None, max_length=12000)
    handoff: Optional[str] = Field(default=None, max_length=3000)
    default_mode: Optional[Literal['auto', 'copilot', 'off']] = None

@router.get('/ai/settings')
async def get_settings(user=Depends(current_user)):
    s = await settings_for(user['company_id'])
    month = now().strftime('%Y-%m')
    s['usage'] = await db.ai_usage.find_one({'company_id': user['company_id'], 'month': month}, {'_id': 0}) or {}
    s['key_configured'] = key_ok()
    return s

@router.put('/ai/settings')
async def put_settings(body: SettingsIn, request: Request, user=Depends(managers)):
    changes = body.model_dump(exclude_none=True)
    if changes:
        await db.ai_settings.update_one({'company_id': user['company_id']}, {'$set': changes}, upsert=True)
        await audit(request, user, 'Ajustou a Nanda (IA)', ', '.join(changes))
    return await get_settings(user)

class TestIn(BaseModel):
    messages: List[dict] = Field(min_length=1, max_length=30)

@router.post('/ai/test')
async def test(body: TestIn, user=Depends(managers)):
    """Conversa de teste no painel: nada vai para o WhatsApp."""
    s = await settings_for(user['company_id'])
    msgs = []
    for m in body.messages:
        role = 'user' if m.get('dir') == 'in' else 'assistant'
        t = str(m.get('text') or '')[:2000]
        if msgs and msgs[-1]['role'] == role: msgs[-1]['content'] += '\n' + t
        else: msgs.append({'role': role, 'content': t})
    while msgs and msgs[0]['role'] != 'user': msgs.pop(0)
    if not msgs: raise HTTPException(400, 'Escreva uma mensagem como cliente.')
    text, usage = await llm(await system_for(s, user['company']), msgs)
    await track(user['company_id'], usage, 'tests')
    d = parse(text)
    if d['book']: d['book_label'] = describe_book(user['company'], d['book'])
    return d

# ---------- na conversa ----------
@router.post('/conversations/{contact_id}/suggest')
async def suggest(contact_id: str, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    d, usage = await think(user['company_id'], c)
    await track(user['company_id'], usage, 'suggestions')
    if d['book']: d['book_label'] = describe_book(user['company'], d['book'])
    return d

class ModeIn(BaseModel):
    mode: Optional[Literal['auto', 'copilot', 'off']] = None

@router.post('/conversations/{contact_id}/mode')
async def set_mode(contact_id: str, body: ModeIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    upd = {'ai_mode': body.mode}
    if body.mode == 'auto': upd['needs_human'] = False
    await db.contacts.update_one({'id': contact_id}, {'$set': upd})
    label = {'auto': 'automática', 'copilot': 'copiloto', 'off': 'pausada', None: 'padrão da empresa'}[body.mode]
    await note(user['company_id'], contact_id, f'{user.get("impersonated_by") or user["name"]} deixou a Nanda em modo {label}')
    return {'ok': True}

@router.post('/conversations/{contact_id}/resolved')
async def resolved(contact_id: str, user=Depends(current_user)):
    await get_contact(user, contact_id)
    await db.contacts.update_one({'id': contact_id}, {'$set': {'needs_human': False}})
    return {'ok': True}
