"""Jornada do cliente: tudo o que aconteceu com ele, em ordem, num lugar só.

A linha do tempo é montada na hora a partir do que o sistema já guarda (avisos da conversa, vendas, mensagens,
pós-venda). Por isso clientes antigos já aparecem com histórico. O que não existe em outro lugar (registros manuais
e marcos) fica na coleção `journey`. Nada é apagado: desfazer deixa o registro riscado, com quem desfez.
"""
import re
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Literal
from core import db, now, uid, current_user, managers, audit
from whatsapp import get_contact, aware

router = APIRouter()

CATS = {'entrada': 'Entrada', 'conversa': 'Conversa', 'etapa': 'Etapa', 'agenda': 'Agenda', 'venda': 'Venda', 'equipe': 'Equipe',
        'nanda': 'Nanda', 'origem': 'Origem', 'envio': 'Automações e campanhas', 'posvenda': 'Pós-venda', 'manual': 'Registro do time',
        'marco': 'Marco', 'sistema': 'Sistema'}

# avisos antigos da conversa viram categoria pelo texto
NOTE_RULES = [
    (r'registrou uma venda|aprovou a venda', None),  # vendas vêm da coleção de vendas, com os itens
    (r'moveu (renovação|próximo produto|reativação)', 'posvenda'),
    (r' moveu para ', 'etapa'),
    (r'agendou|remarcou|marcou .+ como ', 'agenda'),
    (r'transferiu|pegou a conversa|assumiu a conversa|^Alerta: esperando', 'equipe'),
    (r'origem|^Veio pelo link', 'origem'),
    (r'^A Nanda|deixou a Nanda', 'nanda'),
    (r'marcou a conversa como resolvida|reabriu a conversa', 'equipe'),
]

def cat_of(text):
    for rx, cat in NOTE_RULES:
        if re.search(rx, text or ''): return cat
    return 'sistema'

def ev(at, cat, title, detail='', by=None, **extra):
    return {'at': aware(at).isoformat(), 'cat': cat, 'title': title, 'detail': detail, 'by': by, **extra}

def sale_at(x):
    """A venda guarda só o dia; se foi lançada no mesmo dia, usa a hora do lançamento."""
    d, cr = aware(x['date']), aware(x.get('created_at'))
    return cr if cr and abs((cr - d).total_seconds()) < 86400 and cr >= d else d

def brl(v): return f'R$ {v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.').replace(',00', '')

def who_of(by):
    b = by or ''
    if b.startswith('Automação'): return 'auto'
    if b.startswith('Campanha'): return 'campanha'
    if '(IA)' in b: return 'nanda'
    return 'pessoa' if b else 'celular'

async def timeline(company, c):
    cid, kid = company['id'], c['id']
    out = []
    from origins import origins_of
    on = {o['id']: o['name'] for o in await origins_of(company)}
    src = (f'Anúncio "{c["ad"].get("headline") or c["ad"].get("source_id") or ""}"' if c.get('ad') else
           {'manual': 'Cadastrado à mão', 'historico': 'Conversa antiga do celular', 'agenda do celular': 'Agenda do celular'}.get(c.get('source'), 'Chamou no WhatsApp'))
    det = src + (f' · origem: {on.get(c["origin"], c["origin"])}' if c.get('origin') else '') + (f' · {c["origin_detail"]}' if c.get('origin_detail') else '')
    out.append(ev(c.get('created_at') or now(), 'entrada', 'Entrou como contato', det))

    msgs = await db.messages.find({'company_id': cid, 'contact_id': kid}, {'_id': 0, 'dir': 1, 'text': 1, 'at': 1, 'by': 1, 'type': 1}).sort('at', 1).to_list(20000)
    first_in = None; replied = False; days = {}
    for m in msgs:
        at = aware(m['at'])
        if m['dir'] == 'sys':
            cat = cat_of(m.get('text'))
            if cat: out.append(ev(at, cat, m.get('text') or ''))
            continue
        d = at.date().isoformat()
        day = days.setdefault(d, {'in': 0, 'out': 0, 'at': at, 'last': at})
        day['last'] = at
        if m['dir'] == 'in':
            day['in'] += 1
            if not first_in:
                first_in = at; out.append(ev(at, 'conversa', 'Mandou a primeira mensagem', (m.get('text') or '')[:120]))
        else:
            w = who_of(m.get('by'))
            if w in ('auto', 'campanha'):
                out.append(ev(at, 'envio', ('Recebeu automação' if w == 'auto' else 'Recebeu campanha') + f' "{(m.get("by") or "").split("·", 1)[-1].strip()}"', (m.get('text') or '')[:160]))
                continue
            day['out'] += 1
            if first_in and not replied:
                replied = True
                mins = (at - first_in).total_seconds() / 60
                t = '< 1 min' if mins < 1 else f'{round(mins)} min' if mins < 60 else f'{mins / 60:.1f} h'.replace('.', ',') if mins < 1440 else f'{mins / 1440:.1f} dias'.replace('.', ',')
                out.append(ev(at, 'conversa', f'Primeira resposta em {t}', '', (m.get('by') or 'Pelo celular').replace(' (IA)', ''), mins=round(mins, 1)))
    for d, x in days.items():
        if x['in'] + x['out'] > 1:
            out.append(ev(x['last'], 'conversa', 'Conversou', f'{x["in"]} {"mensagem" if x["in"] == 1 else "mensagens"} do cliente · {x["out"]} {"resposta" if x["out"] == 1 else "respostas"}', day=True))

    for s in await db.sales.find({'company_id': cid, 'contact_id': kid}, {'_id': 0}).to_list(500):
        items = ', '.join(f'{(str(round(i.get("qty") or 1, 2)).rstrip("0").rstrip(".") + "× ") if (i.get("qty") or 1) != 1 else ""}{i.get("name")}' for i in s.get('items') or [])
        sellers = ', '.join(x.get('name') or '' for x in s.get('sellers') or [] if x.get('name'))
        extra = []
        if s.get('discount'): extra.append(f'desconto {brl(s["discount"])}')
        if s.get('coupon'): extra.append(f'cupom {s["coupon"]}')
        st = s.get('status') or 'ok'
        title = {'ok': f'Comprou {brl(s["total"])}', 'pending': f'Venda de {brl(s["total"])} aguardando aprovação', 'cancelled': f'Venda de {brl(s["total"])} cancelada'}[st]
        out.append(ev(sale_at(s), 'venda', title, items + (' · ' + ' · '.join(extra) if extra else ''), sellers or s.get('by'), sale_id=s['id'], status=st))

    for o in await db.opps.find({'company_id': cid, 'contact_id': kid}, {'_id': 0}).to_list(200):
        from opps import KINDS
        out.append(ev(o['created_at'], 'posvenda', f'Entrou em {KINDS.get(o["kind"], o["kind"]).lower()}' + (f' de {o["product_name"]}' if o.get('product_name') else ''), o.get('note') or '', o.get('by')))

    ms = {m['id']: m for m in milestones_of(company)}
    SRC = {'created': ('entrada',), 'stage': ('etapa',), 'appt_booked': ('agenda',), 'appt_done': ('agenda',), 'sale': ('venda', 'etapa')}
    for h in await hits_for(company, [kid], derived_only=True):
        m = ms.get(h['milestone_id'])
        if not m: continue
        cats = SRC.get(m['rule']['type'], ())
        twin = next((e for e in out if e['cat'] in cats and abs((datetime.fromisoformat(e['at']) - h['at']).total_seconds()) < 5), None)
        if twin: twin.setdefault('marcos', []).append({'id': m['id'], 'name': m['name'], 'icon': m.get('icon') or '•'})  # o marco aparece no próprio evento
        else: out.append(ev(h['at'], 'marco', m['name'], RULE_TXT.get(m['rule']['type'], ''), None, milestone_id=m['id'], source='auto'))
    for j in await db.journey.find({'company_id': cid, 'contact_id': kid, 'kind': {'$in': ['manual', 'milestone']}}, {'_id': 0}).to_list(2000):
        out.append(ev(j['at'], j.get('cat') or 'manual', j['title'], j.get('note') or '', j.get('by'), id=j['id'], kind=j.get('kind'),
                      undone=bool(j.get('undone_at')), undone_by=j.get('undone_by'), milestone_id=j.get('milestone_id'), source=j.get('source')))
    out.sort(key=lambda e: e['at'], reverse=True)
    return out

@router.get('/contacts/{contact_id}/journey')
async def journey(contact_id: str, cats: str = '', limit: int = 300, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    items = await timeline(user['company'], c)
    counts = {}
    for e in items: counts[e['cat']] = counts.get(e['cat'], 0) + 1
    if cats:
        want = set(cats.split(','))
        items = [e for e in items if e['cat'] in want]
    return {'items': items[:max(1, min(limit, 1000))], 'total': len(items), 'counts': counts, 'cats': CATS}

class EntryIn(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    note: str = Field(default='', max_length=500)
    at: Optional[datetime] = None

@router.post('/contacts/{contact_id}/journey')
async def add_entry(contact_id: str, body: EntryIn, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    at = aware(body.at) if body.at else now()
    if at > now() + timedelta(minutes=5): raise HTTPException(400, 'A data não pode ser no futuro.')
    j = {'id': uid(), 'company_id': user['company_id'], 'contact_id': c['id'], 'kind': 'manual', 'cat': 'manual', 'title': body.title.strip(),
         'note': body.note.strip(), 'at': at, 'by': user.get('impersonated_by') or user['name'], 'by_id': user['id'], 'created_at': now()}
    await db.journey.insert_one(j.copy())
    return {'ok': True, 'id': j['id']}

@router.post('/journey/{jid}/undo')
async def undo(jid: str, user=Depends(current_user)):
    j = await db.journey.find_one({'id': jid, 'company_id': user['company_id']}, {'_id': 0})
    if not j: raise HTTPException(404, 'Registro não encontrado.')
    await get_contact(user, j['contact_id'])
    if j.get('undone_at'): return {'ok': True}
    if user['role'] == 'agent' and j.get('by_id') not in (None, user['id']): raise HTTPException(403, 'Só quem registrou, o dono ou o gestor podem desfazer.')
    await db.journey.update_one({'id': jid}, {'$set': {'undone_at': now(), 'undone_by': user.get('impersonated_by') or user['name']}})
    return {'ok': True}


# ---------- marcos ----------
RULE_TXT = {'created': 'registrado quando o contato entrou', 'stage': 'registrado ao entrar na etapa', 'appt_booked': 'registrado ao agendar',
            'appt_done': 'registrado quando compareceu', 'sale': 'registrado pela venda', 'manual': 'marcado pelo time'}
RULE_TYPES = {'manual': 'Só manual (time ou Nanda)', 'created': 'Quando o contato entra', 'stage': 'Quando entra numa etapa do funil',
              'appt_booked': 'Quando agenda um serviço', 'appt_done': 'Quando comparece a um serviço', 'sale': 'Quando compra'}

class RuleIn(BaseModel):
    type: Literal['manual', 'created', 'stage', 'appt_booked', 'appt_done', 'sale'] = 'manual'
    stage_id: Optional[str] = None
    service_id: Optional[str] = None
    product_id: Optional[str] = None
    won_stage: bool = True  # em "quando compra": conta também quem foi para a etapa de ganho sem venda registrada

class MilestoneIn(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=2, max_length=40)
    icon: str = Field(default='•', max_length=4)
    active: bool = True
    rule: RuleIn = Field(default_factory=RuleIn)
    nanda: bool = False
    hint: str = Field(default='', max_length=200)

def _m(key, name, icon, rule, nanda=False, hint=''):
    return {'id': key, 'name': name, 'icon': icon, 'active': True, 'rule': {'type': 'manual', 'stage_id': None, 'service_id': None, 'product_id': None, 'won_stage': True, **rule},
            'nanda': nanda, 'hint': hint}

def default_milestones(company):
    from niches import niche
    nx = niche(company.get('segment'))
    sv = (nx.get('service') or ('Atendimento',))[0]
    agenda_first = company.get('segment') in ('studio', 'clinica', 'salao', 'imobiliaria', 'escola', 'servicos')
    out = [_m('entrou', 'Entrou', '👋', {'type': 'created'})]
    if agenda_first:
        out += [_m('agendou', f'Agendou {sv.lower()}', '📅', {'type': 'appt_booked'}), _m('compareceu', f'Fez {sv.lower()}', '✅', {'type': 'appt_done'})]
    out += [_m('orcamento', 'Pediu orçamento', '💬', {}, True, 'o cliente pergunta preço, valores, planos ou pede um orçamento'),
            _m('proposta', 'Recebeu proposta', '📄', {}, True, 'o time ou a Nanda manda valores, proposta ou orçamento para o cliente'),
            _m('aceitou', 'Aceitou proposta', '🤝', {}, True, 'o cliente diz que topa, aceita, quer fechar ou pede como pagar'),
            _m('comprou', 'Comprou', '🏆', {'type': 'sale'})]
    return out

def milestones_of(company):
    return company.get('milestones') if company.get('milestones') is not None else default_milestones(company)

def active_ms(company): return [m for m in milestones_of(company) if m.get('active', True)]

async def hits_for(company, contact_ids=None, start=None, end=None, derived_only=False):
    """Toda vez que cada cliente atingiu cada marco: [{contact_id, milestone_id, at, source}]."""
    cid = company['id']; out = []
    base = {'company_id': cid}
    if contact_ids is not None: base['contact_id'] = {'$in': list(contact_ids)}
    def rng(field):
        q = {}
        if start: q['$gte'] = start
        if end: q['$lt'] = end
        return {field: q} if q else {}
    ms = active_ms(company)
    types = {m['rule']['type'] for m in ms}
    cache = {}
    async def get(key, coll, q, proj):
        if key not in cache: cache[key] = await db[coll].find(q, proj).to_list(200000)
        return cache[key]
    for m in ms:
        r = m['rule']; t = r['type']; rows = []
        if t == 'created':
            q = {'company_id': cid, **rng('created_at')}
            if contact_ids is not None: q['id'] = {'$in': list(contact_ids)}
            rows = [(c['id'], c.get('created_at')) for c in await get('contacts', 'contacts', q, {'_id': 0, 'id': 1, 'created_at': 1})]
        elif t == 'stage' and r.get('stage_id'):
            rows = [(e['contact_id'], e['at']) for e in await get('stage', 'stage_events', {**base, **rng('at')}, {'_id': 0, 'contact_id': 1, 'to': 1, 'at': 1}) if e['to'] == r['stage_id']]
        elif t in ('appt_booked', 'appt_done'):
            aps = await get('appts', 'appointments', base, {'_id': 0, 'contact_id': 1, 'service_id': 1, 'status': 1, 'start': 1, 'created_at': 1})
            for a in aps:
                if r.get('service_id') and a.get('service_id') != r['service_id']: continue
                if t == 'appt_done' and a.get('status') != 'done': continue
                at = aware(a['created_at'] if t == 'appt_booked' else a['start'])
                if (start and at < start) or (end and at >= end): continue
                rows.append((a['contact_id'], at))
        elif t == 'sale':
            sales = await get('sales', 'sales', {**base, 'status': 'ok', **rng('date')}, {'_id': 0, 'contact_id': 1, 'date': 1, 'items': 1, 'created_at': 1})
            got = set()
            for x in sales:
                if r.get('product_id') and not any(i.get('product_id') == r['product_id'] for i in x.get('items') or []): continue
                rows.append((x['contact_id'], sale_at(x))); got.add(x['contact_id'])
            if r.get('won_stage', True):
                for e in await get('stage', 'stage_events', {**base, **rng('at')}, {'_id': 0, 'contact_id': 1, 'to': 1, 'at': 1, 'kind': 1}):
                    if e.get('kind') == 'won' and e['contact_id'] not in got: rows.append((e['contact_id'], e['at']))
        out += [{'contact_id': k, 'milestone_id': m['id'], 'at': aware(a), 'source': 'auto'} for k, a in rows if a]
    if not derived_only:
        ids = {m['id'] for m in ms}
        for j in await db.journey.find({**base, 'kind': 'milestone', 'undone_at': None, **rng('at')}, {'_id': 0, 'contact_id': 1, 'milestone_id': 1, 'at': 1, 'source': 1}).to_list(200000):
            if j['milestone_id'] in ids: out.append({'contact_id': j['contact_id'], 'milestone_id': j['milestone_id'], 'at': aware(j['at']), 'source': j.get('source') or 'manual'})
    return out

@router.get('/journey/settings')
async def get_settings(user=Depends(current_user)):
    c = user['company']
    from agenda import settings_for
    try: services = [{'id': s['id'], 'name': s['name']} for s in (await settings_for(c)).get('services') or []]
    except Exception: services = []
    from funnel import stages_for
    return {'milestones': milestones_of(c), 'custom': c.get('milestones') is not None, 'rule_types': RULE_TYPES, 'services': services,
            'stages': [{'id': s['id'], 'name': s['name']} for s in await stages_for(c)],
            'products': await db.products.find({'company_id': c['id'], 'active': True}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}

class SettingsIn(BaseModel):
    milestones: List[MilestoneIn] = Field(max_length=15)

@router.put('/journey/settings')
async def put_settings(body: SettingsIn, request: Request, user=Depends(managers)):
    out, seen = [], set()
    for m in body.milestones:
        d = m.model_dump()
        d['id'] = d['id'] or uid()[:8]
        if d['id'] in seen: raise HTTPException(400, 'Marco repetido.')
        seen.add(d['id']); d['name'] = d['name'].strip(); d['hint'] = d['hint'].strip()
        if d['rule']['type'] == 'stage' and not d['rule'].get('stage_id'): raise HTTPException(400, f'Escolha a etapa do marco "{d["name"]}".')
        if d['nanda'] and not d['hint']: raise HTTPException(400, f'Explique para a Nanda quando "{d["name"]}" acontece.')
        out.append(d)
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'milestones': out}})
    await audit(request, user, 'Alterou os marcos da jornada', ', '.join(m['name'] for m in out))
    return {'ok': True, 'milestones': out}

@router.post('/journey/settings/reset')
async def reset_settings(request: Request, user=Depends(managers)):
    await db.companies.update_one({'id': user['company_id']}, {'$unset': {'milestones': 1}})
    await audit(request, user, 'Voltou os marcos da jornada ao padrão', '')
    return {'ok': True}

class MarkIn(BaseModel):
    milestone_id: str
    note: str = Field(default='', max_length=300)
    at: Optional[datetime] = None

@router.post('/contacts/{contact_id}/milestones')
async def mark(contact_id: str, body: MarkIn, user=Depends(current_user)):
    c = await get_contact(user, contact_id)
    m = next((x for x in active_ms(user['company']) if x['id'] == body.milestone_id), None)
    if not m: raise HTTPException(404, 'Marco não encontrado.')
    at = aware(body.at) if body.at else now()
    if at > now() + timedelta(minutes=5): raise HTTPException(400, 'A data não pode ser no futuro.')
    await add_hit(user['company_id'], c['id'], m, at, 'manual', user.get('impersonated_by') or user['name'], user['id'], body.note.strip())
    return {'ok': True}

async def add_hit(company_id, contact_id, m, at, source, by, by_id=None, note_txt=''):
    j = {'id': uid(), 'company_id': company_id, 'contact_id': contact_id, 'kind': 'milestone', 'cat': 'marco', 'milestone_id': m['id'], 'title': m['name'],
         'note': note_txt, 'at': at, 'source': source, 'by': by, 'by_id': by_id, 'created_at': now(), 'undone_at': None}
    await db.journey.insert_one(j.copy())
    return j

# ---------- sugestões da Nanda ----------
def nanda_block(company):
    ms = [m for m in active_ms(company) if m.get('nanda') and m.get('hint')]
    if not ms: return ''
    return '\nMARCOS DA JORNADA (para o time acompanhar):\n- Inclua no JSON o campo "marcos": lista com o id de cada um destes que aconteceu nas últimas mensagens. Se nenhum, lista vazia.\n' + \
        ''.join(f'- {m["id"]}: {m["name"]} (quando {m["hint"]})\n' for m in ms)

async def suggest(company, contact, ids):
    ms = {m['id']: m for m in active_ms(company) if m.get('nanda')}
    for i in ids or []:
        m = ms.get(i)
        if not m: continue
        recent = await db.journey.find_one({'company_id': company['id'], 'contact_id': contact['id'], 'milestone_id': i, 'kind': {'$in': ['milestone', 'suggestion']},
                                            'undone_at': None, 'at': {'$gte': now() - timedelta(hours=12)}})
        if recent: continue  # já marcado ou sugerido há pouco
        await db.journey.insert_one({'id': uid(), 'company_id': company['id'], 'contact_id': contact['id'], 'kind': 'suggestion', 'milestone_id': i, 'title': m['name'],
                                     'at': now(), 'status': 'pending', 'undone_at': None, 'created_at': now()})

@router.get('/contacts/{contact_id}/milestones')
async def contact_milestones(contact_id: str, user=Depends(current_user)):
    """Para o painel: cada marco com quantas vezes e a última data, mais as sugestões da Nanda."""
    c = await get_contact(user, contact_id)
    hits = await hits_for(user['company'], [c['id']])
    out = []
    for m in active_ms(user['company']):
        hs = sorted(h['at'] for h in hits if h['milestone_id'] == m['id'])
        out.append({'id': m['id'], 'name': m['name'], 'icon': m.get('icon') or '•', 'count': len(hs), 'first': hs[0].isoformat() if hs else None,
                    'last': hs[-1].isoformat() if hs else None, 'rule': m['rule']['type']})
    sug = await db.journey.find({'company_id': user['company_id'], 'contact_id': c['id'], 'kind': 'suggestion', 'status': 'pending'}, {'_id': 0}).sort('at', -1).to_list(10)
    return {'milestones': out, 'suggestions': sug}

@router.post('/journey/suggestions/{sid}/{action}')
async def decide(sid: str, action: Literal['confirm', 'dismiss'], user=Depends(current_user)):
    s = await db.journey.find_one({'id': sid, 'company_id': user['company_id'], 'kind': 'suggestion'}, {'_id': 0})
    if not s: raise HTTPException(404, 'Sugestão não encontrada.')
    await get_contact(user, s['contact_id'])
    if s.get('status') != 'pending': return {'ok': True}
    who = user.get('impersonated_by') or user['name']
    await db.journey.update_one({'id': sid}, {'$set': {'status': 'confirmed' if action == 'confirm' else 'dismissed', 'decided_by': who, 'decided_at': now()}})
    if action == 'confirm':
        m = next((x for x in active_ms(user['company']) if x['id'] == s['milestone_id']), None)
        if m: await add_hit(user['company_id'], s['contact_id'], m, aware(s['at']), 'nanda', f'Nanda · confirmado por {who}', user['id'])
    return {'ok': True}

# ---------- relatório ----------
def _med(xs):
    xs = sorted(xs)
    if not xs: return None
    n = len(xs); return round((xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2), 1)

@router.get('/reports/journey')
async def report(start: Optional[str] = None, end: Optional[str] = None, origin: str = '', owner: str = '', team: str = '', user=Depends(managers)):
    from sales import period, Filters
    from origins import origins_of
    company = user['company']
    s0, s1, t0, t1 = period(company, Filters(start=start, end=end))
    q = {'company_id': company['id'], 'created_at': {'$gte': t0, '$lt': t1}}
    if owner: q['assigned_to'] = owner
    if team: q['team_id'] = team
    cs = await db.contacts.find(q, {'_id': 0, 'id': 1, 'origin': 1, 'ad': 1, 'assigned_to': 1, 'created_at': 1}).to_list(50000)
    if origin: cs = [c for c in cs if (c.get('origin') or ('anuncio' if c.get('ad') else '_none')) == origin]
    ids = [c['id'] for c in cs]
    ms = active_ms(company)
    first = {}  # (contato, marco) -> primeira vez
    for h in await hits_for(company, ids):
        k = (h['contact_id'], h['milestone_id'])
        if k not in first or h['at'] < first[k]: first[k] = h['at']
    steps, prev = [], None
    for m in ms:
        got = [c['id'] for c in cs if (c['id'], m['id']) in first]
        st = {'id': m['id'], 'name': m['name'], 'icon': m.get('icon') or '•', 'reached': len(got), 'pct': round(100 * len(got) / len(cs), 1) if cs else None}
        if prev:
            pg = [c['id'] for c in cs if (c['id'], prev['id']) in first]
            both = [k for k in pg if (k, m['id']) in first]
            st['from_prev'] = round(100 * len(both) / len(pg), 1) if pg else None
            st['days'] = _med([d for d in ((first[(k, m['id'])] - first[(k, prev['id'])]).total_seconds() / 86400 for k in both) if d >= 0])
            st['stuck'] = len(pg) - len(both); st['base'] = len(pg)
        steps.append(st); prev = m
    goal = next((m for m in ms if m['rule']['type'] == 'sale'), ms[-1] if ms else None)  # conversão = chegar na compra
    last = goal['id'] if goal else None
    names = {o['id']: o['name'] for o in await origins_of(company)}; names['_none'] = 'Não sabemos'
    users = {u['id']: u['name'] for u in await db.users.find({'company_id': company['id']}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    def group(keyf, labels):
        g = {}
        for c in cs:
            k = keyf(c); b = g.setdefault(k, {'key': k, 'name': labels.get(k, 'Sem responsável' if k is None else k), 'total': 0, 'done': 0, 'days': []})
            b['total'] += 1
            if last and (c['id'], last) in first:
                b['done'] += 1
                cr = aware(c.get('created_at'))
                if cr: b['days'].append((first[(c['id'], last)] - cr).total_seconds() / 86400)
        out = []
        for b in g.values():
            out.append({'key': b['key'], 'name': b['name'], 'total': b['total'], 'done': b['done'], 'pct': round(100 * b['done'] / b['total'], 1), 'days': _med([d for d in b['days'] if d >= 0])})
        return sorted(out, key=lambda b: -b['total'])
    acts = {}
    for h in await hits_for(company, start=t0, end=t1):
        acts[h['milestone_id']] = acts.get(h['milestone_id'], 0) + 1
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'total': len(cs), 'steps': steps, 'last': goal['name'] if goal else None,
            'by_origin': group(lambda c: c.get('origin') or ('anuncio' if c.get('ad') else '_none'), names),
            'by_owner': group(lambda c: c.get('assigned_to'), users),
            'activity': [{'id': m['id'], 'name': m['name'], 'icon': m.get('icon') or '•', 'count': acts.get(m['id'], 0)} for m in ms]}
