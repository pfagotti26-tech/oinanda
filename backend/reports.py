"""Painel de vendas e nota de atendimento.

Painel: contatos novos, vendas, ticket médio, conversão, ciclo de venda, tempo de primeira resposta,
funil (quantos chegaram em cada etapa), origem (WhatsApp, anúncio, manual...), desempenho por pessoa e da Nanda.
Nota de atendimento: a IA lê a conversa e dá nota de 0 a 10 para o atendimento, com o que foi bem e o que melhorar.
"""
import re, json, asyncio
from datetime import timedelta
from statistics import median
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from core import db, now, uid, current_user, managers
from whatsapp import get_contact, aware
from funnel import stages_for
import ai

router = APIRouter()
SOURCES = {'whatsapp': 'Chamou no WhatsApp', 'anuncio': 'Anúncio', 'manual': 'Cadastro manual', 'historico': 'Conversa antiga do celular',
           'agenda do celular': 'Agenda do celular'}

def mins(td): return round(td.total_seconds() / 60, 1)

async def first_reply_minutes(company_id, contact_ids):
    """Minutos entre a primeira mensagem do cliente e a primeira resposta (time ou Nanda)."""
    out = {}
    for cid in contact_ids[:800]:
        msgs = await db.messages.find({'company_id': company_id, 'contact_id': cid, 'dir': {'$in': ['in', 'out']}, 'imported': {'$ne': True}},
                                      {'_id': 0, 'dir': 1, 'at': 1, 'by': 1, 'by_id': 1}).sort('at', 1).to_list(40)
        first_in = next((m for m in msgs if m['dir'] == 'in'), None)
        if not first_in: continue
        rep = next((m for m in msgs if m['dir'] == 'out' and aware(m['at']) >= aware(first_in['at'])), None)
        if rep: out[cid] = (mins(aware(rep['at']) - aware(first_in['at'])), rep.get('by_id'), '(IA)' in (rep.get('by') or ''))
    return out

async def period_totals(cid, kinds, won_ids, start, end):
    """Números de um período fechado, para comparar com o atual."""
    new = await db.contacts.find({'company_id': cid, 'created_at': {'$gte': start, '$lt': end}, 'source': {'$nin': ['historico', 'agenda do celular']}},
                                 {'_id': 0, 'id': 1, 'stage': 1}).to_list(10000)
    won = await db.contacts.find({'company_id': cid, 'stage': {'$in': won_ids}, 'won_at': {'$gte': start, '$lt': end}}, {'_id': 0, 'value': 1}).to_list(10000)
    fr = await first_reply_minutes(cid, [c['id'] for c in new])
    vals = [v[0] for v in fr.values()]
    conv = sum(1 for c in new if kinds.get(c.get('stage')) == 'won')
    return {'new_contacts': len(new), 'won': len(won), 'won_value': sum(c.get('value') or 0 for c in won),
            'conversion': round(100 * conv / len(new), 1) if new else None, 'first_reply_min': median(vals) if vals else None}

@router.get('/reports/sales')
async def sales(days: int = 30, user=Depends(managers)):
    days = max(1, min(days, 365))
    cid = user['company_id']; since = now() - timedelta(days=days)
    stages = await stages_for(user['company'])
    kinds = {s['id']: s['kind'] for s in stages}
    won_ids = [s['id'] for s in stages if s['kind'] == 'won']
    new = await db.contacts.find({'company_id': cid, 'created_at': {'$gte': since}, 'source': {'$nin': ['historico', 'agenda do celular']}},
                                 {'_id': 0, 'id': 1, 'source': 1, 'ad': 1, 'stage': 1, 'assigned_to': 1, 'created_at': 1}).to_list(10000)
    won = await db.contacts.find({'company_id': cid, 'stage': {'$in': won_ids}, 'won_at': {'$gte': since}},
                                 {'_id': 0, 'id': 1, 'value': 1, 'created_at': 1, 'won_at': 1, 'assigned_to': 1, 'source': 1, 'ad': 1}).to_list(10000)
    lost = await db.contacts.count_documents({'company_id': cid, 'lost_at': {'$gte': since}})
    value = sum(c.get('value') or 0 for c in won)
    with_value = [c['value'] for c in won if c.get('value')]
    cycle = [(aware(c['won_at']) - aware(c['created_at'])).total_seconds() / 86400 for c in won if c.get('created_at') and c.get('won_at')]
    new_ids = {c['id'] for c in new}
    converted = sum(1 for c in new if kinds.get(c.get('stage')) == 'won')

    # funil: quantos dos contatos novos chegaram em cada etapa (ou além)
    ev = await db.stage_events.find({'company_id': cid, 'contact_id': {'$in': list(new_ids)}}, {'_id': 0, 'contact_id': 1, 'to': 1}).to_list(50000)
    reached = {s['id']: set() for s in stages}
    for e in ev:
        if e['to'] in reached: reached[e['to']].add(e['contact_id'])
    for c in new:
        st = c.get('stage') if c.get('stage') in reached else stages[0]['id']
        reached[st].add(c['id'])
    order = [s['id'] for s in stages if s['kind'] == 'open'] + won_ids
    funnel, carry = [], set()
    for sid in reversed(order):  # quem está numa etapa mais adiante também passou pelas anteriores
        carry |= reached[sid]
        funnel.append({'id': sid, 'count': len(carry)})
    funnel.reverse()
    names = {s['id']: s['name'] for s in stages}
    for f in funnel: f['name'] = names[f['id']]; f['kind'] = kinds[f['id']]

    # origem
    origin = {}
    for c in new:
        k = ('Anúncio: ' + ((c.get('ad') or {}).get('headline') or (c.get('ad') or {}).get('source_id') or 'sem título')) if c.get('ad') else SOURCES.get(c.get('source'), c.get('source') or 'Outro')
        o = origin.setdefault(k, {'name': k, 'contacts': 0, 'won': 0, 'value': 0, 'ad': bool(c.get('ad'))})
        o['contacts'] += 1
    for c in won:
        if c['id'] not in new_ids: continue
        k = ('Anúncio: ' + ((c.get('ad') or {}).get('headline') or (c.get('ad') or {}).get('source_id') or 'sem título')) if c.get('ad') else SOURCES.get(c.get('source'), c.get('source') or 'Outro')
        if k in origin: origin[k]['won'] += 1; origin[k]['value'] += c.get('value') or 0

    # primeira resposta e desempenho por pessoa
    fr = await first_reply_minutes(cid, list(new_ids))
    team = await db.users.find({'company_id': cid}, {'_id': 0, 'id': 1, 'name': 1, 'active': 1}).to_list(200)
    sent = {}
    for m in await db.messages.find({'company_id': cid, 'dir': 'out', 'at': {'$gte': since}}, {'_id': 0, 'by_id': 1, 'by': 1}).to_list(100000):
        k = m.get('by_id') or ('nanda' if '(IA)' in (m.get('by') or '') else 'auto' if (m.get('by') or '').startswith('Automação') else 'celular')
        sent[k] = sent.get(k, 0) + 1
    scores = {}
    for q in await db.quality.find({'company_id': cid, 'at': {'$gte': since}}, {'_id': 0, 'agents': 1, 'score': 1}).to_list(5000):
        for a in q.get('agents') or []: scores.setdefault(a, []).append(q['score'])
    # quem passou oportunidades adiante (pré-vendas) e quantas viraram venda
    passed = {}
    for t in await db.transfers.find({'company_id': cid, 'at': {'$gte': since}, 'by_id': {'$ne': None}, 'auto': {'$ne': True}}, {'_id': 0, 'by_id': 1, 'contact_id': 1}).to_list(50000):
        passed.setdefault(t['by_id'], set()).add(t['contact_id'])
    won_by_id = {c['id']: c for c in won}
    origin_won = {}
    for c in await db.contacts.find({'company_id': cid, 'id': {'$in': list(won_by_id)}, 'origin_by_id': {'$ne': None}}, {'_id': 0, 'id': 1, 'origin_by_id': 1}).to_list(10000):
        if c['origin_by_id'] != won_by_id[c['id']].get('assigned_to'): origin_won.setdefault(c['origin_by_id'], []).append(won_by_id[c['id']])
    people = []
    for u in team:
        w = [c for c in won if c.get('assigned_to') == u['id']]
        mine = [v[0] for k, v in fr.items() if v[1] == u['id']]
        people.append({'id': u['id'], 'name': u['name'], 'active': u.get('active'), 'messages': sent.get(u['id'], 0), 'won': len(w),
                       'value': sum(c.get('value') or 0 for c in w), 'first_reply_min': median(mine) if mine else None,
                       'score': round(sum(scores[u['id']]) / len(scores[u['id']]), 1) if scores.get(u['id']) else None, 'scored': len(scores.get(u['id']) or []),
                       'passed': len(passed.get(u['id'], ())), 'assisted': len(origin_won.get(u['id'], [])),
                       'assisted_value': sum(c.get('value') or 0 for c in origin_won.get(u['id'], []))})
    people.sort(key=lambda p: (-p['value'], -p['won'], -p['messages']))
    month = now().strftime('%Y-%m')
    usage = await db.ai_usage.find_one({'company_id': cid, 'month': month}, {'_id': 0}) or {}
    nanda_books = await db.appointments.count_documents({'company_id': cid, 'created_at': {'$gte': since}, 'by': {'$regex': r'\(IA\)$'}})
    handoffs = await db.messages.count_documents({'company_id': cid, 'dir': 'sys', 'at': {'$gte': since}, 'text': {'$regex': '^A Nanda passou a conversa'}})
    autos = await db.automation_runs.count_documents({'company_id': cid, 'at': {'$gte': since}, 'status': 'sent'})
    all_fr = [v[0] for v in fr.values()]
    prev = await period_totals(cid, kinds, won_ids, since - timedelta(days=days), since)
    # série por dia (ou por semana em períodos longos), no fuso da empresa
    import agenda as _ag
    tz = _ag.tz_of(user['company']); bucket = 7 if days > 45 else 1
    start_local = (now() - timedelta(days=days - 1)).astimezone(tz).date()
    nb = (days + bucket - 1) // bucket
    series = [{'date': str(start_local + timedelta(days=i * bucket)), 'new': 0, 'won': 0, 'value': 0} for i in range(nb)]
    def idx(dt):
        d = aware(dt).astimezone(tz).date(); i = (d - start_local).days // bucket
        return i if 0 <= i < nb else None
    for c in new:
        i = idx(c['created_at'])
        if i is not None: series[i]['new'] += 1
    for c in won:
        i = idx(c['won_at'])
        if i is not None: series[i]['won'] += 1; series[i]['value'] += c.get('value') or 0
    # taxa de passagem entre etapas
    for i, f in enumerate(funnel):
        f['rate'] = round(100 * f['count'] / funnel[i - 1]['count']) if i and funnel[i - 1]['count'] else None
    # o que fazer agora
    alerts = []
    hour_ago = now() - timedelta(hours=1)
    waiting = await db.contacts.count_documents({'company_id': cid, 'last_dir': 'in', 'last_in_at': {'$lte': hour_ago}, 'needs_human': {'$ne': True}})
    human = await db.contacts.count_documents({'company_id': cid, 'needs_human': True})
    open_ids = [s['id'] for s in stages if s['kind'] == 'open']
    stale = await db.contacts.count_documents({'company_id': cid, 'stage': {'$in': open_ids + [None]}, 'last_at': {'$lte': now() - timedelta(days=3)}})
    if human: alerts.append({'kind': 'bad', 'text': f'{human} {"cliente pediu" if human == 1 else "clientes pediram"} para falar com alguém do time', 'to': 'conversas', 'filter': 'waiting'})
    if waiting: alerts.append({'kind': 'warn', 'text': f'{waiting} {"cliente esperando" if waiting == 1 else "clientes esperando"} resposta há mais de 1 hora', 'to': 'conversas', 'filter': 'waiting'})
    if stale: alerts.append({'kind': 'warn', 'text': f'{stale} {"negociação parada" if stale == 1 else "negociações paradas"} há 3 dias ou mais', 'to': 'funil', 'filter': 'stale'})
    timed = [p for p in people if p['first_reply_min'] is not None]
    if len(timed) >= 2:
        fast, slow = min(timed, key=lambda p: p['first_reply_min']), max(timed, key=lambda p: p['first_reply_min'])
        if slow['first_reply_min'] >= max(10, 3 * fast['first_reply_min']):
            alerts.append({'kind': 'info', 'text': f'{fast["name"].split(" ")[0]} responde em {fast["first_reply_min"]:.0f} min; {slow["name"].split(" ")[0]} em {slow["first_reply_min"]:.0f} min', 'to': 'relatorios'})
    drops = [f for f in funnel[1:] if f.get('rate') is not None and funnel[funnel.index(f) - 1]['count'] >= 5]
    if drops:
        worst = min(drops, key=lambda f: f['rate'])
        if worst['rate'] < 50:
            prv = funnel[funnel.index(worst) - 1]['name']
            alerts.append({'kind': 'info', 'text': f'Só {worst["rate"]}% de "{prv}" chegam em "{worst["name"]}": é onde mais se perde', 'to': 'funil'})
    # equipes
    from teams import teams_of
    from inbox import waiting as _waiting
    team_rows = []
    for tm in await teams_of(cid):
        cs = await db.contacts.find({'company_id': cid, 'team_id': tm['id']}, {'_id': 0, 'id': 1, 'assigned_to': 1, 'last_dir': 1, 'resolved_at': 1, 'last_in_at': 1}).to_list(5000)
        tw = [c for c in won if c['id'] in {x['id'] for x in cs}]
        tfr = [v[0] for k, v in fr.items() if k in {x['id'] for x in cs}]
        team_rows.append({'id': tm['id'], 'name': tm['name'], 'color': tm.get('color'), 'members': len(tm.get('member_ids') or []), 'contacts': len(cs),
                          'queue': sum(1 for c in cs if not c.get('assigned_to')), 'waiting': sum(1 for c in cs if _waiting(c)),
                          'won': len(tw), 'value': sum(c.get('value') or 0 for c in tw), 'first_reply_min': median(tfr) if tfr else None})
    late = await db.contacts.count_documents({'company_id': cid, 'sla_alert_at': {'$gte': now() - timedelta(hours=24)}, 'last_dir': 'in'})
    if late: alerts.insert(0, {'kind': 'bad', 'text': f'{late} {"cliente passou" if late == 1 else "clientes passaram"} do tempo limite na fila de uma equipe', 'to': 'conversas', 'filter': 'waiting'})
    return {'prev': prev, 'series': series, 'bucket': bucket, 'alerts': alerts[:4], 'teams': team_rows,
        'days': days,
        'totals': {'new_contacts': len(new), 'won': len(won), 'won_value': value, 'lost': lost,
                   'ticket': round(sum(with_value) / len(with_value), 2) if with_value else None,
                   'conversion': round(100 * converted / len(new), 1) if new else None,
                   'cycle_days': round(median(cycle), 1) if cycle else None,
                   'first_reply_min': median(all_fr) if all_fr else None},
        'funnel': funnel, 'origin': sorted(origin.values(), key=lambda o: -o['contacts']), 'people': people,
        'nanda': {'messages': sent.get('nanda', 0), 'bookings': nanda_books, 'handoffs': handoffs, 'replies_month': usage.get('replies', 0)},
        'automations': {'sent': autos, 'messages': sent.get('auto', 0)},
    }

# ---------- nota de atendimento ----------
RUBRIC = """Você avalia atendimentos de WhatsApp de pequenos negócios brasileiros, com olhar de gestor comercial justo e prático.
Leia a conversa entre o CLIENTE e o TIME do negócio "{company}" e dê uma nota de 0 a 10 para o atendimento do time.
Critérios: rapidez (o tempo entre as mensagens está indicado), cordialidade, se respondeu o que o cliente perguntou,
clareza, se entendeu a necessidade, e se conduziu para o próximo passo (agendar, enviar proposta, fechar) sem pressionar.
Mensagens marcadas como (IA) são da assistente virtual; avalie o atendimento como um todo.
Seja específico e curto, cite trechos quando ajudar. Responda SOMENTE com JSON:
{{"score": 0-10, "summary": "uma frase", "good": ["até 3 itens"], "improve": ["até 3 itens"], "lost_opportunity": true|false}}"""

def transcript(msgs):
    lines, prev = [], None
    for m in msgs:
        at = aware(m['at'])
        gap = f' (+{int((at - prev).total_seconds() // 60)} min)' if prev and (at - prev) > timedelta(minutes=5) else ''
        who = 'CLIENTE' if m['dir'] == 'in' else f"TIME · {m.get('by') or 'time'}"
        lines.append(f'[{who}{gap}] {(m.get("text") or "")[:600]}')
        prev = at
    return '\n'.join(lines)

async def score_contact(company, contact):
    msgs = await db.messages.find({'contact_id': contact['id'], 'dir': {'$in': ['in', 'out']}}, {'_id': 0}).sort('at', -1).to_list(60)
    msgs.reverse()
    if not any(m['dir'] == 'in' for m in msgs) or not any(m['dir'] == 'out' for m in msgs):
        raise HTTPException(400, 'A conversa precisa ter mensagem do cliente e resposta do time.')
    text, usage = await ai.llm(RUBRIC.format(company=company['name']), [{'role': 'user', 'content': transcript(msgs)}], max_tokens=600)
    await ai.track(company['id'], usage, 'scores')
    m = re.search(r'\{.*\}', text or '', re.S)
    try: d = json.loads(m.group(0)) if m else {}
    except ValueError: d = {}
    try: score = max(0.0, min(10.0, float(d.get('score'))))
    except (TypeError, ValueError): raise HTTPException(502, 'A IA não devolveu uma nota. Tente de novo.')
    agents = sorted({m['by_id'] for m in msgs if m['dir'] == 'out' and m.get('by_id')})
    q = {'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'contact_name': contact.get('name'), 'agents': agents, 'score': round(score, 1),
         'summary': str(d.get('summary') or '')[:300], 'good': [str(x)[:200] for x in (d.get('good') or [])][:3],
         'improve': [str(x)[:200] for x in (d.get('improve') or [])][:3], 'lost_opportunity': bool(d.get('lost_opportunity')),
         'upto': msgs[-1]['at'], 'at': now()}
    await db.quality.insert_one(q.copy())
    q.pop('_id', None)
    return q

@router.post('/quality/{contact_id}')
async def score_one(contact_id: str, user=Depends(managers)):
    c = await get_contact(user, contact_id)
    return await score_contact(user['company'], c)

@router.get('/quality/{contact_id}')
async def last_score(contact_id: str, user=Depends(managers)):
    await get_contact(user, contact_id)
    return (await db.quality.find({'contact_id': contact_id}, {'_id': 0}).sort('at', -1).to_list(1) or [None])[0]

@router.post('/quality-run')
async def score_batch(limit: int = 15, user=Depends(managers)):
    """Avalia as conversas da última semana que ainda não têm nota (ou tiveram mensagem nova depois da nota)."""
    if not ai.key_ok(): raise HTTPException(400, 'A chave de IA ainda não foi configurada no servidor.')
    since = now() - timedelta(days=7)
    rows = await db.contacts.find({'company_id': user['company_id'], 'last_at': {'$gte': since}}, {'_id': 0}).sort('last_at', -1).to_list(300)
    todo = []
    for c in rows:
        last = (await db.quality.find({'contact_id': c['id']}, {'_id': 0, 'upto': 1}).sort('at', -1).to_list(1) or [None])[0]
        if last and aware(last['upto']) >= aware(c['last_at']): continue
        if not await db.messages.find_one({'contact_id': c['id'], 'dir': 'out', 'by_id': {'$ne': None}}): continue  # só conversas com gente do time
        todo.append(c)
        if len(todo) >= min(limit, 30): break
    sem = asyncio.Semaphore(3)
    async def one(c):
        async with sem:
            try: return await score_contact(user['company'], c)
            except HTTPException: return None
    done = [q for q in await asyncio.gather(*[one(c) for c in todo]) if q]
    return {'scored': len(done)}

@router.get('/quality')
async def quality_list(days: int = 30, user=Depends(managers)):
    since = now() - timedelta(days=max(1, min(days, 365)))
    rows = await db.quality.find({'company_id': user['company_id'], 'at': {'$gte': since}}, {'_id': 0}).sort('at', -1).to_list(500)
    latest = {}
    for r in rows: latest.setdefault(r['contact_id'], r)  # a nota mais recente de cada conversa
    names = {u['id']: u['name'] for u in await db.users.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(200)}
    items = list(latest.values())
    for r in items: r['agent_names'] = [names.get(a, '—') for a in r.get('agents') or []]
    return {'items': items, 'avg': round(sum(r['score'] for r in items) / len(items), 1) if items else None,
            'lost': sum(1 for r in items if r.get('lost_opportunity'))}
