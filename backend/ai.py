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
from whatsapp import get_contact, deliver, note, aware
from datetime import datetime
import agenda

log = logging.getLogger('oinanda.ai')
router = APIRouter()
def debounce(): return float(os.environ.get('AI_DEBOUNCE', '4'))  # espera o cliente terminar de digitar (várias mensagens seguidas)
HUMAN_WORDS = re.compile(r'\b(atendente|humano|humana|pessoa de verdade|falar com (alg[uú]em|uma pessoa|o dono|a dona|gerente)|reclama|procon|absurdo)\b', re.I)

BLOCKS = ['about', 'offer', 'goal', 'qualify', 'rules', 'faq', 'knowledge', 'handoff']
DEFAULTS = {'name': 'Nanda', 'tone': 'proximo', 'default_mode': 'copilot', 'signature': True, **{b: '' for b in BLOCKS}}
TONES = {'proximo': 'próximo, simpático e direto, como uma boa atendente de WhatsApp', 'formal': 'educado e mais formal',
         'descontraido': 'descontraído e animado, com no máximo um emoji por mensagem'}

async def settings_for(company_id):
    s = await db.ai_settings.find_one({'company_id': company_id}, {'_id': 0}) or {}
    s = {**DEFAULTS, **s, 'company_id': company_id}
    s['taught'] = bool((s['about'] or s['offer'] or s['knowledge']).strip())
    return s

def key_ok(): return bool(os.environ.get('ANTHROPIC_API_KEY'))

def brl(v): return f'R$ {v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.').replace(',00', '')

async def catalog_items(company_id):
    """Itens ativos do Catálogo: a fonte oficial de preços que a Nanda usa."""
    rows = await db.products.find({'company_id': company_id, 'active': {'$ne': False}}, {'_id': 0, 'name': 1, 'category': 1, 'price': 1, 'duration_days': 1, 'service_id': 1, 'recurring': 1}).sort('name', 1).to_list(300)
    # serviço da agenda sem preço: já aparece na AGENDA; aqui só confundiria ("preço sob consulta" ao lado de "aula experimental grátis")
    return [{'name': r['name'], 'category': r.get('category') or '', 'price': r.get('price'), 'duration_days': r.get('duration_days'), 'recurring': bool(r.get('recurring'))} for r in rows
            if not (r.get('service_id') and r.get('price') is None)]

def catalog_text(items):
    out = []
    for r in items:
        bits = [brl(r['price']) if r.get('price') is not None else 'preço sob consulta (chame o time se perguntarem)']
        d = r.get('duration_days'); per = {30: 'por mês', 90: 'por trimestre', 180: 'por semestre', 365: 'por ano'}.get(d)
        if r.get('recurring'): bits[0] += ' ' + per if per and r.get('price') is not None else ''; bits.append('assinatura' + ('' if per else f', cobrada a cada {d or 30} dias'))
        elif d: bits.append({30: 'vale 1 mês', 90: 'vale 3 meses', 180: 'vale 6 meses', 365: 'vale 1 ano'}.get(d, f'vale {d} dias'))
        out.append(f"- {r['name']}" + (f" ({r['category']})" if r['category'] else '') + ': ' + ' · '.join(bits))
    return '\n'.join(out)

def cached(system):
    """Prompt da Nanda com cache: o começo (regras, conhecimento, catálogo) se repete a cada resposta e fica guardado por 5 minutos,
    custando 10% na leitura. Partes que mudam a cada conversa (agenda, ficha do cliente) vêm depois do marcador CACHE_SPLIT."""
    if not isinstance(system, str): return system
    head, sep, tail = system.partition(CACHE_SPLIT)
    blocks = [{'type': 'text', 'text': head, 'cache_control': {'type': 'ephemeral'}}]
    if tail.strip(): blocks.append({'type': 'text', 'text': tail})
    return blocks

CACHE_SPLIT = '\n<<<fim-da-parte-fixa>>>\n'

async def llm(system, messages, max_tokens=900):
    """Chamada ao Claude. Isolada para os testes substituírem. Devolve (texto, uso)."""
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key: raise HTTPException(400, 'A chave de IA ainda não foi configurada no servidor.')
    async with httpx.AsyncClient(timeout=45) as c:
        r = await c.post('https://api.anthropic.com/v1/messages', headers={'x-api-key': key, 'anthropic-version': '2023-06-01'},
                         json={'model': os.environ.get('AI_MODEL', 'claude-haiku-5-5'), 'max_tokens': max_tokens, 'system': cached(system), 'messages': messages})
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise HTTPException(502, 'A IA recusou: ' + ((data.get('error') or {}).get('message') or f'erro {r.status_code}'))
    text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
    return text, data.get('usage') or {}

def knowledge_block(s):
    parts = [('Sobre o negócio', s.get('about')),
             ('Catálogo com os preços oficiais (se algo abaixo disser outro valor, vale este)', s.get('_catalog')),
             ('Condições, pacotes e observações de preço', s.get('offer')), ('Perguntas frequentes', s.get('faq')), ('Outras informações', s.get('knowledge'))]
    text = '\n\n'.join(f'{t}:\n{v.strip()}' for t, v in parts if (v or '').strip())
    return text or '(o dono ainda não preencheu)'

def build_system(s, company, contact_name, agenda_text=None, upcoming=None, origin_opts=None, extra='', step=30, notes='', stages=None, stage_id=None):
    tz = agenda.tz_of(company); t = now().astimezone(tz)
    ag = ''
    if agenda_text:
        ag = f"""
AGENDA (horários livres de verdade, já descontando o que está ocupado):
{agenda_text}
- "09:00 às 12:30" é uma faixa: qualquer início de {step} em {step} min dentro dela está livre (09:00, 09:30, 10:00...). Se o cliente pedir um horário dentro de uma faixa, pode agendar.
- Ofereça no máximo 3 opções por vez, perguntando antes qual dia ou período fica melhor. Não liste a agenda inteira.
- Só agende quando o cliente escolher um serviço e um horário que esteja livre AQUI. Aí preencha "book" e confirme na reply (serviço, dia e hora).
- Nunca ofereça horário fora do que está livre. Se nenhum servir, chame o time.
- Para cancelar ou remarcar, chame o time.
- No WhatsApp, quando for oferecer horários (o cliente já disse o serviço), você pode preencher "slots" com o service_id: o sistema manda uma lista com os próximos horários livres para o cliente tocar, e o horário escolhido já fica marcado. Nesse caso a reply é só uma frase curta de introdução (ex.: "Separei os próximos horários livres, é só tocar no melhor pra você 😊"), sem listar horários.
"""
        if upcoming:
            ag += 'Este cliente já tem agendado: ' + '; '.join(f"{a['service_name']} {agenda.WEEK[agenda.aware(a['start']).astimezone(tz).weekday()]} {agenda.aware(a['start']).astimezone(tz):%d/%m %H:%M}" for a in upcoming) + '\n'
    # parte fixa primeiro (fica no cache da IA); o que muda a cada conversa vem depois do marcador
    fixed = f"""Você é {s['name']}, assistente de atendimento e vendas do negócio "{company['name']}" no WhatsApp.
Fale em português do Brasil, num tom {TONES.get(s['tone'], TONES['proximo'])}. Mensagens curtas (até 3 frases), como no WhatsApp.
Use o primeiro nome do cliente com naturalidade.

O QUE VOCÊ SABE SOBRE O NEGÓCIO (única fonte de verdade):
<conhecimento>
{knowledge_block(s)}
</conhecimento>
{('SEU OBJETIVO NA CONVERSA: ' + s['goal'].strip()) if s['goal'].strip() else 'SEU OBJETIVO NA CONVERSA: tirar as dúvidas e levar o cliente ao próximo passo.'}
{('PARA ENTENDER O CLIENTE (pergunte uma coisa de cada vez, só o que ainda não sabe, sem parecer formulário):' + chr(10) + s['qualify'].strip()) if s['qualify'].strip() else ''}

QUANDO CHAMAR O TIME:
{s['handoff'].strip() or 'Quando o cliente pedir para falar com uma pessoa, reclamar, ou quando a resposta não estiver no conhecimento.'}
{('REGRAS DESTE NEGÓCIO:' + chr(10) + s['rules'].strip()) if s['rules'].strip() else ''}

REGRAS QUE VOCÊ NUNCA QUEBRA:
- Nunca invente preço, horário, condição ou promessa que não esteja no conhecimento. Se não souber, chame o time.
- Se o cliente pedir uma pessoa, estiver irritado ou o assunto for sensível (saúde, reclamação, cancelamento), chame o time.
- Se perguntarem se você é robô, diga com naturalidade que é a assistente virtual do {company['name']} e que pode chamar alguém do time.
- Seu objetivo é ajudar e levar ao próximo passo (agendar, fechar, tirar a dúvida), sem pressionar.

Responda SOMENTE com um JSON, sem nada antes ou depois (comece direto com a chave de abertura). O texto da reply é mensagem de WhatsApp: sem markdown, sem asteriscos, curto.
{{"reply": "texto para o cliente (ou vazio)", "handoff": true|false, "reason": "motivo curto se handoff", "book": null ou {{"service_id": "id", "start": "AAAA-MM-DDTHH:MM", "pro_id": null}}, "slots": null ou {{"service_id": "id"}}, "origin": null ou "id da origem", "origin_detail": "", "marcos": [], "anotar": "", "valor": null, "valor_item": "", "etapa": null, "urgente": false, "faltou": ""}}
"faltou": quando o cliente perguntou algo que NÃO está no que você sabe e por isso você não respondeu ou chamou o time, escreva a dúvida como uma pergunta frequente curta e genérica, sem o nome do cliente (ex.: "Tem estacionamento?", "Atende convênio?"). Se você soube responder, ou o assunto é negociação, reclamação ou pedido de pessoa, deixe "".
Se handoff for true, a reply deve avisar com gentileza que alguém do time vai continuar (ex.: "Vou chamar a Camila, ela já te responde!").
"urgente" só com handoff true e só quando não pode esperar: problema de saúde ou reação a procedimento, cliente irritado ou reclamando, pedido de reembolso, ameaça (Procon, advogado), situação de segurança. Pedir desconto, preço ou vaga não é urgente."""
    now_part = f"""Hoje é {agenda.WEEK[t.weekday()]} {t:%d/%m/%Y}, {t:%H:%M}.
O cliente se chama {contact_name or 'cliente'}.
{ag}{extra}{origin_block(origin_opts, company)}{notes_block(notes)}{stage_block(stages, stage_id)}"""
    return fixed + CACHE_SPLIT + now_part

def stage_block(stages, stage_id):
    """Etapas abertas do funil, para a Nanda mover o cliente quando a conversa chega lá (ex.: link enviado, orçamento enviado)."""
    if not stages: return ''
    opened = [x for x in stages if x.get('kind') == 'open']
    if len(opened) < 2: return ''
    cur = next((x for x in stages if x['id'] == stage_id), None) or opened[0]
    return f"""
ETAPAS DO FUNIL, nesta ordem: {' → '.join(f"{x['id']} ({x['name']})" for x in opened)}. O cliente está em: {cur['name']}.
- "etapa": o id da etapa quando a conversa claramente chegou nela (ex.: você mandou o link ou o orçamento, o cliente respondeu o que qualifica). Só para frente, e nunca a etapa de fechado/perdido: isso é o time que marca. Senão null.
"""

def notes_block(notes):
    return f"""
ANOTAÇÕES DO CLIENTE (o que o time já sabe; não pergunte de novo o que está aqui):
{(notes or '').strip() or '(nada ainda)'}
- "anotar": uma frase curta, só se aprendeu algo novo e útil para quem vai atender (para quem é, nome do filho, prazo, orçamento, preferência, quem indicou). Não repita o que já está anotado. Senão, "".
- "valor": se o cliente mostrou interesse em um item do catálogo com preço, o valor estimado desse negócio em reais (número); senão null. "valor_item": o nome do item.
"""

def origin_block(opts, company):
    if not opts: return ''
    return f"""
COMO O CLIENTE CONHECEU O NEGÓCIO (ainda não sabemos):
- Depois de ajudar no que o cliente pediu (nunca logo na primeira resposta e nunca no meio de uma dúvida), pergunte uma vez, com leveza, como ele conheceu o {company['name']}. Se ele já disse na conversa, não pergunte.
- Quando souber, preencha "origin" com um destes ids: {', '.join(f'{o["id"]} ({o["name"]})' for o in opts)}.
- Em "origin_detail" coloque o detalhe se houver: nome de quem indicou, qual evento, qual campanha ou post. Senão deixe vazio.
- Se a resposta não se encaixar em nenhum, use "outro" com o que ele disse no detalhe.
"""

def clean_reply(t):
    """Texto que vai para o cliente: sem negrito de markdown (no WhatsApp aparece como asteriscos) e sem sobra de JSON."""
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', t or '', flags=re.S)
    return re.sub(r'\n{3,}', '\n\n', t).strip()

def salvage(text):
    """A resposta veio fora do formato (texto solto antes do JSON, ou JSON cortado no meio): aproveita o que dá, sem nunca mostrar JSON ao cliente."""
    text = text or ''
    cut = re.search(r'```|\{\s*"reply"', text)
    before = text[:cut.start()].strip() if cut else text.strip()
    m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)("?)', text, re.S)
    inside = ''
    if m:
        raw = m.group(1)
        if not m.group(2): raw = re.sub(r'\\$', '', raw)  # cortado no meio de um escape
        try: inside = json.loads('"' + raw + '"')
        except ValueError: inside = raw.replace('\\n', '\n').replace('\\"', '"')
        if not m.group(2): inside = re.sub(r'[^.!?…]*$', '', inside).strip() or inside  # cortado: fica até a última frase inteira
    h = re.search(r'"handoff"\s*:\s*true', text)
    reply = before if len(before) >= len(inside) else inside
    if not h and re.search(r'\bvou chamar\b|chamar (algu[eé]m|o time)', reply, re.I): h = True  # avisou que ia chamar e o JSON não chegou
    return reply, bool(h), (re.search(r'"reason"\s*:\s*"([^"]{0,200})', text) or [None, ''])[1]

def parse(text):
    m = re.search(r'\{.*\}', text or '', re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            d['reply'] = clean_reply(str(d.get('reply') or ''))
            b = d.get('book') if isinstance(d.get('book'), dict) and d['book'].get('service_id') and d['book'].get('start') else None
            if b: b = {'service_id': str(b['service_id'])[:24], 'start': str(b['start'])[:19], 'pro_id': (str(b['pro_id'])[:24] if b.get('pro_id') else None)}
            sl = d.get('slots') if isinstance(d.get('slots'), dict) and d['slots'].get('service_id') else None
            return {'reply': str(d.get('reply') or '').strip(), 'handoff': bool(d.get('handoff')), 'reason': str(d.get('reason') or '')[:200], 'book': b,
                    'slots': {'service_id': str(sl['service_id'])[:24]} if sl and not b else None,
                    'origin': str(d['origin'])[:24] if isinstance(d.get('origin'), str) and d.get('origin') else None, 'origin_detail': str(d.get('origin_detail') or '')[:80],
                    'marcos': [str(x)[:12] for x in d.get('marcos') or [] if isinstance(x, (str, int))][:5] if isinstance(d.get('marcos'), list) else [],
                    'anotar': str(d.get('anotar') or '').strip()[:200],
                    'valor': float(d['valor']) if isinstance(d.get('valor'), (int, float)) and 0 < d['valor'] <= 100_000_000 else None,
                    'valor_item': str(d.get('valor_item') or '').strip()[:60],
                    'etapa': str(d['etapa'])[:24] if isinstance(d.get('etapa'), str) and d.get('etapa') else None,
                    'urgente': bool(d.get('urgente')) and bool(d.get('handoff')),
                    'faltou': str(d.get('faltou') or '').strip()[:160]}
        except (ValueError, TypeError): pass
    reply, hand, reason = salvage(text)
    return {'reply': clean_reply(reply), 'handoff': hand, 'reason': reason, 'book': None, 'origin': None, 'origin_detail': '', 'marcos': [], 'anotar': '', 'valor': None, 'valor_item': '', 'etapa': None, 'urgente': False, 'faltou': ''}

def _img_block(r):
    """Foto recente do cliente vai como imagem para a Nanda enxergar (comprovante, print, foto de referência)."""
    import base64
    try:
        from media import path_for
        p = path_for(r)
        if r.get('mime') not in ('image/jpeg', 'image/png', 'image/webp') or not p.exists() or p.stat().st_size > 3_500_000: return None
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': r['mime'], 'data': base64.b64encode(p.read_bytes()).decode()}}
    except Exception: return None

def _msg_text(r):
    t = r.get('type'); txt = r.get('text') or ''
    if t == 'audio': return f'[áudio] {r["transcript"]}' if r.get('transcript') else '[mandou um áudio que não deu para ouvir]'
    if t == 'image': return '[foto]' + (f' {r["caption"]}' if r.get('caption') else '')
    if t == 'video': return '[vídeo]' + (f' {r["caption"]}' if r.get('caption') else '')
    if t == 'document': return f'[arquivo {r.get("filename") or ""}]'.replace(' ]', ']') + (f' {r["caption"]}' if r.get('caption') else '')
    if t == 'sticker': return '[figurinha]'
    return txt

async def history(contact_id, limit=20):
    rows = await db.messages.find({'contact_id': contact_id, 'dir': {'$in': ['in', 'out']}},
                                  {'_id': 0, 'id': 1, 'company_id': 1, 'dir': 1, 'text': 1, 'type': 1, 'transcript': 1, 'caption': 1, 'filename': 1, 'mime': 1}).sort('at', -1).to_list(limit)
    rows = list(reversed(rows))
    imgs = {r['id'] for r in rows[-6:] if r['dir'] == 'in' and r.get('type') == 'image'}
    msgs = []
    for r in rows:
        role = 'user' if r['dir'] == 'in' else 'assistant'
        blocks = [{'type': 'text', 'text': _msg_text(r) or '…'}]
        if r['id'] in imgs:
            b = _img_block(r)
            if b: blocks.insert(0, b)
        if msgs and msgs[-1]['role'] == role: msgs[-1]['content'] += blocks
        else: msgs.append({'role': role, 'content': blocks})
    while msgs and msgs[0]['role'] != 'user': msgs.pop(0)
    for m in msgs:  # só texto: junta numa string (formato mais simples)
        if all(b['type'] == 'text' for b in m['content']): m['content'] = '\n'.join(b['text'] for b in m['content'])
    return msgs

async def track(company_id, usage, kind, contact_id=None):
    month = now().strftime('%Y-%m')
    await db.ai_usage.update_one({'company_id': company_id, 'month': month},
        {'$inc': {'input_tokens': usage.get('input_tokens', 0), 'output_tokens': usage.get('output_tokens', 0),
                  'cache_read_tokens': usage.get('cache_read_input_tokens') or 0, 'cache_write_tokens': usage.get('cache_creation_input_tokens') or 0, kind: 1}}, upsert=True)
    try:
        import notices, billing
        if kind == 'replies' and await billing.count_atend(company_id, contact_id): await notices.ai_mark(company_id)
        elif kind == 'suggestions': await notices.ai_mark(company_id)
    except Exception: log.exception('falha no aviso de uso da Nanda')

async def origin_hint(company, contact):
    bits = []
    ad = contact.get('ad') or {}
    if ad.get('headline') or ad.get('body'): bits.append(f'clicou no anúncio "{(ad.get("headline") or ad.get("body"))[:80]}"')
    if contact.get('origin_link'):
        l = await db.track_links.find_one({'id': contact['origin_link'], 'company_id': company['id']}, {'_id': 0, 'label': 1})
        if l and l.get('label'): bits.append(f'chegou pelo link "{l["label"][:60]}"')
    if contact.get('checkout_src'): bits.append(f'veio do checkout com a origem "{str(contact["checkout_src"])[:40]}"')
    ev = await db.checkout_events.find_one({'company_id': company['id'], 'contact_id': contact['id']}, {'_id': 0, 'kind': 1, 'product': 1, 'provider': 1}, sort=[('at', -1)])
    if ev and ev.get('product'):
        what = {'pending': 'gerou o pagamento (boleto ou Pix) e ainda não pagou', 'abandoned': 'começou a compra e não terminou', 'approved': 'comprou',
                'refunded': 'pediu reembolso', 'chargeback': 'abriu chargeback'}.get(ev['kind'])
        if what: bits.append(f'{what} de "{ev["product"][:60]}"')
    if not bits: return ''
    return ('\nDE ONDE ESTE CLIENTE VEIO: ' + '; '.join(bits) + '.\nNa primeira resposta, mostre que sabe disso com naturalidade (ex.: "vi que você veio da aula 3"), '
            'sem soar como robô e sem inventar o que não está aqui. Se ele gerou pagamento ou abandonou a compra, ajude com a dúvida e ofereça o link, sem pressionar.\n')

async def system_for(s, company, contact=None):
    step = 30
    try: agenda_text, ag_s = await agenda.nanda_context(company); step = (ag_s or {}).get('step') or 30
    except Exception: log.exception('falha ao montar a agenda'); agenda_text = None
    upcoming = await agenda.upcoming_for(company['id'], contact['id']) if contact and agenda_text else None
    name = (contact.get('name') or '').split(' ')[0] if contact else 'Cliente'
    opts = None
    if contact and company.get('ask_origin', True) and not contact.get('origin') and not contact.get('ad'):
        from origins import origins_of
        opts = [o for o in await origins_of(company) if o['id'] != 'anuncio']
    extra = ''
    try:
        from promos import nanda_block
        last = (await db.messages.find({'contact_id': contact['id'], 'dir': 'in'}, {'_id': 0, 'text': 1}).sort('at', -1).to_list(1) or [{}])[0].get('text', '') if contact else ''
        extra = await nanda_block(company, contact or {'id': None}, last)
        if contact and contact.get('holdings'):
            from wallet import state_of, offers_for, products_map
            tz = agenda.tz_of(company)
            has = [f"{x['name']}" + (f" (até {aware(x['expires_at']).astimezone(tz):%d/%m}" + (', vence logo' if state_of(x) == 'vence_logo' else ', já venceu' if state_of(x) == 'vencido' else '') + ')' if x.get('expires_at') else '') for x in contact['holdings']]
            offs = offers_for(contact['holdings'], await products_map(company['id']))
            extra += '\nO QUE ESTE CLIENTE JÁ COMPROU: ' + '; '.join(has) + '\n'
            if offs:
                extra += 'PODE FAZER SENTIDO OFERECER (só quando combinar com a conversa, sem empurrar; preço só se estiver no conhecimento): ' + '; '.join(
                    ('renovação de ' if o['why'] == 'renovar' else 'voltar com ' if o['why'] == 'voltar' else '') + o['name'] for o in offs) + '\n'
    except Exception: log.exception('falha ao montar as promoções')
    try:  # de onde a pessoa veio (link, anúncio, plataforma de venda): a Nanda cita no começo para a conversa não parecer genérica
        if contact: extra += await origin_hint(company, contact)
    except Exception: log.exception('falha ao montar a origem')
    try:
        from journey import nanda_block as journey_block
        if contact: extra += journey_block(company)
    except Exception: log.exception('falha ao montar os marcos')
    try:
        from channels import nanda_block as wa_block, wa_link
        if contact and contact.get('channel') == 'instagram' and not contact.get('wa_id'): extra += wa_block(contact, await wa_link(company['id'], contact))
    except Exception: log.exception('falha ao montar o link do whatsapp')
    try: s = {**s, '_catalog': catalog_text(await catalog_items(company['id']))}
    except Exception: log.exception('falha ao montar o catálogo')
    stages = None
    try:
        from funnel import stages_for
        stages = await stages_for(company) if contact else None
    except Exception: log.exception('falha ao montar as etapas')
    return build_system(s, company, name, agenda_text, upcoming, opts, extra, step, (contact or {}).get('notes') or '', stages, (contact or {}).get('stage'))

async def think(company_id, contact):
    s = await settings_for(company_id)
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    msgs = await history(contact['id'])
    if not msgs: return parse(''), {}
    text, usage = await llm(await system_for(s, company, contact), msgs)
    d = parse(text)
    if d.get('faltou'):
        try: await record_gap(company_id, contact, d['faltou'])
        except Exception: log.exception('falha ao guardar a pergunta que a Nanda não soube')
    return d, usage

def gap_key(q):
    import unicodedata
    t = unicodedata.normalize('NFKD', q).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9 ]', '', re.sub(r'\s+', ' ', t)).strip()

async def record_gap(company_id, contact, q):
    """Pergunta de cliente que a Nanda não sabia responder: vira uma lista para o dono ensinar (a mesma pergunta soma)."""
    q = q.strip().rstrip('.')
    if len(q) < 6: return
    if not q.endswith('?'): q += '?'
    q = q[0].upper() + q[1:]
    key = gap_key(q)
    g = await db.ai_gaps.find_one({'company_id': company_id, 'key': key, 'status': {'$in': ['open', 'dismissed']}}, {'_id': 0, 'id': 1, 'contacts': 1, 'status': 1})
    if g:
        if g['status'] == 'dismissed': return
        upd = {'$set': {'last_at': now(), 'last_contact': contact.get('name')}, '$inc': {'count': 1}}
        if contact['id'] in (g.get('contacts') or []): upd.pop('$inc')
        else: upd['$push'] = {'contacts': {'$each': [contact['id']], '$slice': -20}}
        await db.ai_gaps.update_one({'id': g['id']}, upd)
    else:
        await db.ai_gaps.insert_one({'id': uid(), 'company_id': company_id, 'q': q[:160], 'key': key, 'count': 1, 'contacts': [contact['id']],
                                     'last_contact': contact.get('name'), 'first_at': now(), 'last_at': now(), 'status': 'open'})

def describe_book(company, b):
    try:
        t = datetime.fromisoformat(b['start'])
        return f"{agenda.WEEK[t.weekday()]} {t:%d/%m às %H:%M}"
    except Exception: return b.get('start')

# ---------- resposta automática ----------
async def handoff(company_id, contact, reason, urgent=False):
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'needs_human': True, 'ai_mode': 'off', 'ai_paused': 'handoff', 'ai_prev': contact.get('ai_mode'),
                                                                   'needs_human_at': now(), 'handoff_reason': reason, 'handoff_urgent': bool(urgent)}})
    await note(company_id, contact['id'], ('🚨 URGENTE · ' if urgent else '') + f'A Nanda passou a conversa para o time · {reason or "cliente quer falar com uma pessoa"}')
    await route(company_id, contact, reason)
    try:
        from push import later, on_handoff
        fresh = await db.contacts.find_one({'id': contact['id']}, {'_id': 0}) or contact
        later(on_handoff(company_id, fresh, reason, urgent))
        from notify import handoff_alert
        later(handoff_alert(company_id, fresh, reason, urgent))
    except Exception: log.exception('falha no aviso de repasse')

async def route(company_id, contact, reason):
    try:
        from teams import route_handoff
        company = await db.companies.find_one({'id': company_id}, {'_id': 0})
        fresh = await db.contacts.find_one({'id': contact['id']}, {'_id': 0}) or contact
        await route_handoff(company, fresh, reason, fresh.get('last_text') or '')
    except Exception: log.exception('falha ao direcionar o repasse para uma equipe')

async def suggest_marcos(company_id, contact, ids):
    try:
        from journey import suggest
        await suggest(await db.companies.find_one({'id': company_id}, {'_id': 0}), contact, ids)
    except Exception: log.exception('falha ao sugerir marcos')

def cover_delay(): return float(os.environ.get('AI_COVER_DELAY', '180'))  # com a conversa no time, dá esse tempo para uma pessoa responder antes de a Nanda cobrir

async def cover_block(company, contact):
    """Instrução extra quando a conversa já está com o time: a Nanda só cobre o simples, sem mexer no assunto repassado."""
    from inbox import next_open
    quando = await next_open(company)
    volta = f'a partir de {quando}' if quando else 'em breve'
    return f"""
ESTA CONVERSA JÁ ESTÁ COM O TIME{(' · motivo: ' + contact['handoff_reason']) if contact.get('handoff_reason') else ''}.
- Você só cobre o que for simples e estiver no conhecimento (endereço, horário de funcionamento, formas de pagamento, dúvida rápida).
- Não trate do motivo do repasse, não agende, não negocie, não chame o time de novo: deixe handoff false, book null e origin null.
- Diga em uma frase que alguém do time continua com ele {volta}.
- Se a mensagem for sobre o assunto repassado, for só um "ok/obrigado" ou precisar de uma pessoa, deixe reply vazia ("").
"""

async def auto_reply(company_id, contact_id, msg_id):
    await asyncio.sleep(debounce())
    try:
        contact = await db.contacts.find_one({'id': contact_id}, {'_id': 0})
        if not contact: return
        s = await settings_for(company_id)
        cover = bool(contact.get('needs_human') or contact.get('ai_paused') == 'handoff')
        if cover:
            if (contact.get('ai_prev') or s['default_mode']) != 'auto': return  # antes do repasse ela não respondia sozinha: segue assim
            await asyncio.sleep(max(0.0, cover_delay() - debounce()))
            contact = await db.contacts.find_one({'id': contact_id}, {'_id': 0})
            if not contact or not (contact.get('needs_human') or contact.get('ai_paused') == 'handoff'): return
        mode = 'auto' if cover else (contact.get('ai_mode') or s['default_mode'])
        if mode == 'auto':
            from billing import has, ai_allowed
            co = await db.companies.find_one({'id': company_id}, {'_id': 0}) or {}
            if not has(co, 'auto') or not await ai_allowed(co, 'auto', contact_id):
                mode = 'copilot'  # plano sem Nanda automática, só leitura ou atendimentos do mês esgotados: ela passa a sugerir
                month = now().strftime('%Y-%m')
                if has(co, 'auto') and co.get('ai_limit_noted') != month:
                    await db.companies.update_one({'id': company_id}, {'$set': {'ai_limit_noted': month}})
                    await note(company_id, contact_id, 'Os atendimentos da Nanda deste mês acabaram. Ela segue sugerindo as respostas para o time. Para ela voltar a atender sozinha, adicione um pacote em Empresa › Assinatura.')
        # o horário da Meta vem em segundos; a ordem real é a de chegada no servidor
        last = (await db.messages.find({'contact_id': contact_id, 'dir': 'in'}, {'_id': 0}).sort('received_at', -1).to_list(1) or [None])[0]
        if not last or last['id'] != msg_id: return  # chegou mensagem mais nova: a resposta sai na vez dela
        if await db.messages.find_one({'contact_id': contact_id, 'dir': 'out', 'at': {'$gt': last['received_at']}}): return  # alguém já respondeu
        if await db.messages.find_one({'contact_id': contact_id, 'dir': 'in', 'pending': True, 'id': {'$ne': msg_id}}): return  # áudio ainda sendo transcrito: a resposta sai quando terminar
        asks_human = bool(HUMAN_WORDS.search(last.get('text') or ''))
        if cover:  # conversa no time: só cobre o factual, uma mensagem curta, sem repassar de novo
            if mode != 'auto' or asks_human or not key_ok(): return
            company = await db.companies.find_one({'id': company_id}, {'_id': 0})
            msgs = await history(contact_id)
            if not msgs: return
            text, usage = await llm(await system_for(s, company, contact) + await cover_block(company, contact), msgs)
            await track(company_id, usage, 'replies', contact_id)
            d = parse(text)
            if d['reply']: await deliver(company_id, contact, d['reply'], None, s['name'] + ' (IA)')
            await apply_facts(company_id, contact, d)
            return
        if mode != 'auto' or not key_ok():
            if asks_human:  # mesmo sem a Nanda respondendo, o time fica sabendo
                await db.contacts.update_one({'id': contact_id}, {'$set': {'needs_human': True, 'needs_human_at': now(), 'handoff_reason': 'cliente pediu uma pessoa'}})
                await route(company_id, contact, 'cliente pediu uma pessoa')
                try:
                    from push import later
                    from notify import handoff_alert
                    later(handoff_alert(company_id, contact, 'cliente pediu uma pessoa'))
                except Exception: log.exception('falha no aviso de repasse')
            return
        if asks_human:
            await deliver(company_id, contact, 'Claro! Vou chamar alguém do time para continuar com você. 😊', None, s['name'] + ' (IA)')
            return await handoff(company_id, contact, 'cliente pediu uma pessoa')
        d, usage = await think(company_id, contact)
        await track(company_id, usage, 'replies', contact_id)
        if d['book']:
            company = await db.companies.find_one({'id': company_id}, {'_id': 0})
            try:
                a = await agenda.book(company, contact, d['book']['service_id'], datetime.fromisoformat(d['book']['start']), None, s['name'] + ' (IA)', pro_id=d['book'].get('pro_id'))
                if a.get('pro_name') and a['pro_name'].lower() not in (d['reply'] or '').lower():
                    d['reply'] = (d['reply'] or '').rstrip() + f' Seu horário fica com {a["pro_name"]}.'
            except (HTTPException, ValueError):
                d['reply'] = 'Opa, esse horário acabou de ser ocupado 😅 Quer que eu veja outra opção pra você?'
        sent_list = False
        if d.get('slots') and not d['book']:
            from channels import view
            if (view(contact).get('channel') or 'whatsapp') == 'whatsapp':
                import wachoice
                company = await db.companies.find_one({'id': company_id}, {'_id': 0})
                try: sent_list = await wachoice.offer_slots(company, contact, d['slots']['service_id'], d['reply'] or 'Separei os próximos horários livres, é só tocar no melhor pra você 😊', s['name'] + ' (IA)')
                except Exception: log.exception('falha ao mandar a lista de horários')
        if d['reply'] and not sent_list:
            import voice
            await voice.reply(company_id, contact, d['reply'], s['name'] + ' (IA)', msg_id)
        await apply_origin(company_id, contact, d)
        await apply_facts(company_id, contact, d)
        await apply_stage(company_id, contact, d, s['name'] + ' (IA)')
        if d.get('marcos'): await suggest_marcos(company_id, contact, d['marcos'])
        if d['handoff']: await handoff(company_id, contact, d['reason'], d.get('urgente'))
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
    try:
        from funnel import on_team_reply
        await on_team_reply(user['company_id'], contact)
    except Exception: log.exception('falha ao mover o card depois da resposta do time')
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
    about: Optional[str] = Field(default=None, max_length=3000)
    offer: Optional[str] = Field(default=None, max_length=8000)
    goal: Optional[str] = Field(default=None, max_length=1000)
    qualify: Optional[str] = Field(default=None, max_length=2000)
    rules: Optional[str] = Field(default=None, max_length=3000)
    faq: Optional[str] = Field(default=None, max_length=8000)
    default_mode: Optional[Literal['auto', 'copilot', 'off']] = None

@router.get('/ai/settings')
async def get_settings(user=Depends(current_user)):
    s = await settings_for(user['company_id'])
    month = now().strftime('%Y-%m')
    s['usage'] = await db.ai_usage.find_one({'company_id': user['company_id'], 'month': month}, {'_id': 0}) or {}
    s['key_configured'] = key_ok()
    try:
        from sales import sync_agenda; await sync_agenda(user['company'])  # serviços da Agenda entram no Catálogo
    except Exception: log.exception('falha ao sincronizar a agenda com o catálogo')
    s['catalog'] = await catalog_items(user['company_id'])
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

def to_llm(messages):
    msgs = []
    for m in messages:
        role = 'user' if m.get('dir') == 'in' else 'assistant'
        t = str(m.get('text') or '')[:2000]
        if msgs and msgs[-1]['role'] == role: msgs[-1]['content'] += '\n' + t
        else: msgs.append({'role': role, 'content': t})
    while msgs and msgs[0]['role'] != 'user': msgs.pop(0)
    return msgs

@router.get('/ai/example')
async def example(user=Depends(current_user)):
    """Exemplo preenchido do nicho da empresa, para o dono adaptar."""
    from niches import niche
    return niche(user['company'].get('segment'))['ai']

@router.post('/ai/test')
async def test(body: TestIn, user=Depends(managers)):
    """Conversa de teste no painel: nada vai para o WhatsApp."""
    from billing import ai_allowed
    if not await ai_allowed(user['company'], 'test'): raise HTTPException(402, 'As conversas de teste do modo demonstração acabaram este mês. Ative seus 14 dias grátis para continuar.')
    s = await settings_for(user['company_id'])
    if not s['taught']:  # ainda não ensinou: a Nanda conversa com o exemplo do ramo (modo demonstração e primeiros passos)
        from niches import niche
        ex = niche(user['company'].get('segment'))['ai']
        s = {**s, **{k: v for k, v in ex.items() if not (s.get(k) or '').strip()}}
    msgs = to_llm(body.messages)
    if not msgs: raise HTTPException(400, 'Escreva uma mensagem como cliente.')
    text, usage = await llm(await system_for(s, user['company']), msgs)
    await track(user['company_id'], usage, 'tests')
    d = parse(text)
    if d['book']: d['book_label'] = describe_book(user['company'], d['book'])
    return d

async def apply_facts(company_id, contact, d):
    """O que a Nanda aprendeu vai para as Anotações do cliente e para o valor previsto, para o time não ter que reler a conversa."""
    fresh = await db.contacts.find_one({'id': contact['id']}, {'_id': 0, 'notes': 1, 'value': 1, 'sales_count': 1}) or {}
    upd = {}
    fact = (d.get('anotar') or '').strip().rstrip('.')
    notes = (fresh.get('notes') or '').strip()
    if fact and fact.lower() not in notes.lower() and len(notes) < 2500:
        tz = agenda.tz_of(await db.companies.find_one({'id': company_id}, {'_id': 0, 'timezone': 1}) or {})
        upd['notes'] = (notes + '\n' if notes else '') + f'• {fact} (Nanda, {now().astimezone(tz):%d/%m})'
    if d.get('valor') and not fresh.get('value') and not fresh.get('sales_count'):
        upd['value'] = d['valor']
        await note(company_id, contact['id'], f'A Nanda estimou o valor previsto: {brl(d["valor"])}' + (f' · {d["valor_item"]}' if d.get('valor_item') else ''))
    if upd: await db.contacts.update_one({'id': contact['id']}, {'$set': upd})

async def apply_stage(company_id, contact, d, by_name):
    """Move só para frente, só entre etapas abertas, e nunca em cima de uma etapa mais avançada que o time já marcou."""
    if not d.get('etapa'): return
    try:
        from funnel import stages_for, move_stage
        company = await db.companies.find_one({'id': company_id}, {'_id': 0})
        stages = await stages_for(company)
        ids = [x['id'] for x in stages]
        fresh = await db.contacts.find_one({'id': contact['id']}, {'_id': 0}) or contact
        cur = fresh.get('stage') or ids[0]
        if d['etapa'] not in ids or cur not in ids: return
        target = next(x for x in stages if x['id'] == d['etapa'])
        if target.get('kind') != 'open' or ids.index(d['etapa']) <= ids.index(cur): return
        await move_stage(company, fresh, d['etapa'], by_name, stages)
    except Exception: log.exception('falha ao mover a etapa sugerida pela Nanda')

async def apply_origin(company_id, contact, d):
    """Grava a origem que a Nanda entendeu da conversa (no automático e nas sugestões)."""
    if not d.get('origin') or contact.get('origin'): return
    from origins import origins_of, set_origin
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    names = {o['id']: o['name'] for o in await origins_of(company)}
    if d['origin'] not in names: return
    ref = d['origin_detail'] if d['origin'] == 'indicacao' else None
    if await set_origin(company, contact, d['origin'], d['origin_detail'], 'nanda', None, ref):
        await note(company_id, contact['id'], f'A Nanda descobriu a origem: {names[d["origin"]]}' + (f' · {d["origin_detail"]}' if d['origin_detail'] else ''))

# ---------- na conversa ----------
@router.post('/conversations/{contact_id}/suggest')
async def suggest(contact_id: str, cached: bool = False, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    last = (await db.messages.find({'contact_id': c['id'], 'dir': 'in'}, {'_id': 0, 'id': 1}).sort('at', -1).to_list(1) or [{}])[0].get('id')
    if cached:  # sugestão automática ao abrir a conversa: uma por mensagem do cliente
        if c.get('sug_for') == last and c.get('sug'): return c['sug']
        if not key_ok(): return {'reply': '', 'handoff': False, 'reason': '', 'book': None}
    from billing import ai_allowed
    if not await ai_allowed(user['company'], 'sug'): raise HTTPException(402, 'As sugestões da Nanda deste mês acabaram. Mude de plano em Empresa › Assinatura para continuar.')
    d, usage = await think(user['company_id'], c)
    await track(user['company_id'], usage, 'suggestions')
    if d.get('marcos'): await suggest_marcos(user['company_id'], c, d['marcos'])
    await apply_origin(user['company_id'], c, d)
    if last: await db.contacts.update_one({'id': c['id']}, {'$set': {'sug_for': last, 'sug': {k: d.get(k) for k in ('reply', 'handoff', 'reason', 'book')}}})
    if d['book']: d['book_label'] = describe_book(user['company'], d['book'])
    return d

class ModeIn(BaseModel):
    mode: Optional[Literal['auto', 'copilot', 'off']] = None

@router.post('/conversations/{contact_id}/mode')
async def set_mode(contact_id: str, body: ModeIn, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    upd = {'ai_mode': body.mode}
    if body.mode == 'auto': upd['needs_human'] = False
    await db.contacts.update_one({'id': contact_id}, {'$set': upd, '$unset': {'ai_paused': '', 'ai_prev': ''}})
    label = {'auto': 'automática', 'copilot': 'copiloto', 'off': 'pausada', None: 'padrão da empresa'}[body.mode]
    await note(user['company_id'], contact_id, f'{user.get("impersonated_by") or user["name"]} deixou a Nanda em modo {label}')
    return {'ok': True}

@router.post('/conversations/{contact_id}/resolved')
async def resolved(contact_id: str, user=Depends(current_user)):
    await get_contact(user, contact_id)
    await db.contacts.update_one({'id': contact_id}, {'$set': {'needs_human': False}})
    return {'ok': True}

async def resume(company_id, contact, who='o time'):
    """A Nanda volta a atuar como antes do repasse (só quando foi ela mesma que pausou)."""
    if contact.get('ai_paused') != 'handoff': return False
    await db.contacts.update_one({'id': contact['id']}, {'$set': {'ai_mode': contact.get('ai_prev'), 'needs_human': False, 'handoff_urgent': False}, '$unset': {'ai_paused': '', 'ai_prev': ''}})
    await note(company_id, contact['id'], f'{who} devolveu a conversa para a Nanda')
    return True

@router.post('/conversations/{contact_id}/resume')
async def resume_route(contact_id: str, request: Request, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    if c.get('ai_paused') == 'handoff': await resume(user['company_id'], c, user.get('impersonated_by') or user['name'])
    else:
        await db.contacts.update_one({'id': contact_id}, {'$set': {'ai_mode': None, 'needs_human': False}})
        await note(user['company_id'], contact_id, f'{user.get("impersonated_by") or user["name"]} religou a Nanda nesta conversa')
    return {'ok': True}

# ---------- testes salvos ----------
class CaseIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    messages: List[dict] = Field(min_length=1, max_length=20)
    expect: Literal['reply', 'handoff', 'book'] = 'reply'
    contains: str = Field(default='', max_length=300)
    not_contains: str = Field(default='', max_length=300)

def terms(v): return [t.strip().lower() for t in re.split(r'[;\n]', v or '') if t.strip()]

def judge(case, d):
    """Confere a resposta com o que o dono espera. Devolve (passou, motivo)."""
    reply = (d.get('reply') or '').lower()
    if case['expect'] == 'handoff' and not d['handoff']: return False, 'Era para chamar o time e ela respondeu sozinha.'
    if case['expect'] == 'book' and not d['book']: return False, 'Era para agendar e ela não agendou.'
    if case['expect'] == 'reply' and d['handoff']: return False, 'Era para responder e ela chamou o time (' + (d.get('reason') or '') + ').'
    miss = [t for t in terms(case.get('contains')) if t not in reply]
    if miss: return False, 'Faltou mencionar: ' + ', '.join(miss)
    bad = [t for t in terms(case.get('not_contains')) if t in reply]
    if bad: return False, 'Falou o que não devia: ' + ', '.join(bad)
    return True, 'ok'

@router.get('/ai/tests')
async def list_tests(user=Depends(managers)):
    return await db.ai_tests.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', 1).to_list(50)

@router.post('/ai/tests')
async def add_test(body: CaseIn, user=Depends(managers)):
    if await db.ai_tests.count_documents({'company_id': user['company_id']}) >= 30: raise HTTPException(400, 'Limite de 30 testes.')
    if not to_llm(body.messages): raise HTTPException(400, 'O teste precisa de pelo menos uma mensagem do cliente.')
    t = {'id': uid(), 'company_id': user['company_id'], **body.model_dump(), 'created_at': now(), 'last': None}
    await db.ai_tests.insert_one(t.copy())
    t.pop('_id', None)
    return t

@router.delete('/ai/tests/{tid}')
async def del_test(tid: str, user=Depends(managers)):
    await db.ai_tests.delete_one({'id': tid, 'company_id': user['company_id']})
    return {'ok': True}

@router.post('/ai/tests/run')
async def run_tests(user=Depends(managers)):
    """Roda todos os testes salvos com o que a Nanda sabe agora. Nada vai para o WhatsApp."""
    cases = await db.ai_tests.find({'company_id': user['company_id']}, {'_id': 0}).to_list(30)
    if not cases: raise HTTPException(400, 'Salve pelo menos um teste antes.')
    s = await settings_for(user['company_id'])
    system = await system_for(s, user['company'])
    sem = asyncio.Semaphore(4)
    async def one(case):
        async with sem:
            try:
                text, usage = await llm(system, to_llm(case['messages']))
                await track(user['company_id'], usage, 'tests')
                d = parse(text); ok, why = judge(case, d)
                res = {'ok': ok, 'why': why, 'reply': d['reply'], 'handoff': d['handoff'], 'book': bool(d['book']), 'at': now()}
            except HTTPException as e:
                res = {'ok': False, 'why': str(e.detail), 'reply': '', 'at': now()}
            await db.ai_tests.update_one({'id': case['id']}, {'$set': {'last': res}})
            return {**case, 'last': res}
    out = await asyncio.gather(*[one(c) for c in cases])
    return {'passed': sum(1 for c in out if c['last']['ok']), 'total': len(out), 'tests': out}


# ---------- o que ela não soube ----------
@router.get('/ai/gaps')
async def gaps(user=Depends(managers)):
    rows = await db.ai_gaps.find({'company_id': user['company_id'], 'status': 'open'}, {'_id': 0, 'key': 0, 'contacts': 0}).sort([('count', -1), ('last_at', -1)]).to_list(30)
    return rows

class GapAnswer(BaseModel):
    answer: str = Field(min_length=2, max_length=600)

@router.post('/ai/gaps/{gid}/answer')
async def gap_answer(gid: str, body: GapAnswer, request: Request, user=Depends(managers)):
    """O dono responde: a pergunta e a resposta entram nas Perguntas frequentes e a Nanda passa a saber."""
    g = await db.ai_gaps.find_one({'id': gid, 'company_id': user['company_id']}, {'_id': 0})
    if not g: raise HTTPException(404, 'Pergunta não encontrada.')
    s = await settings_for(user['company_id'])
    ans = body.answer.strip()
    if not re.search(r'[.!?]$', ans): ans += '.'
    faq = ((s.get('faq') or '').rstrip() + '\n' + f'{g["q"]} {ans}').strip()
    if len(faq) > 8000: raise HTTPException(400, 'As Perguntas frequentes já estão cheias. Resuma alguma antes de acrescentar.')
    await db.ai_settings.update_one({'company_id': user['company_id']}, {'$set': {'faq': faq}}, upsert=True)
    await db.ai_gaps.update_one({'id': gid}, {'$set': {'status': 'answered', 'answer': ans, 'answered_at': now(), 'answered_by': user['name']}})
    await audit(request, user, 'Ensinou a Nanda', g['q'])
    return {'faq': faq}

@router.post('/ai/gaps/{gid}/dismiss')
async def gap_dismiss(gid: str, user=Depends(managers)):
    """Não é para ela responder isso (o time cuida): some da lista e não volta."""
    r = await db.ai_gaps.update_one({'id': gid, 'company_id': user['company_id']}, {'$set': {'status': 'dismissed', 'dismissed_at': now()}})
    if not r.matched_count: raise HTTPException(404, 'Pergunta não encontrada.')
    return {'ok': True}
