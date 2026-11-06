"""Metas, premiações e gamificação. A empresa liga o que quiser (metas, prêmios, pontos/ranking).

Metas: para a empresa, uma equipe ou uma pessoa, por semana, mês, trimestre ou datas próprias, com degraus
(mínimo, meta, supermeta), ritmo ("faltam R$ X por dia") e projeção. A meta da equipe pode ser dividida entre as pessoas.
Na venda dividida, cada vendedor soma a parte do seu percentual.
Prêmios: ao bater a meta (ou a supermeta) o prêmio fica conquistado; o gestor marca quando entregar. O Oi Nanda só registra.
Desafios: competição com prazo ("quem vender mais planos anuais em novembro"), com prêmio para os primeiros.
Pontos: por ações reais (venda, renovação, resposta rápida, agendamento, nota alta...). Venda cancelada não conta.
Níveis e conquistas saem dos pontos e do histórico. Ranking com privacidade: todos, só o pódio ou só a própria posição.
"""
import calendar, logging
from datetime import datetime, timedelta, time as dtime
from statistics import median
from typing import Optional, List, Literal, Dict
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import aware, note
import agenda

router = APIRouter()
log = logging.getLogger('oinanda.goals')
METRICS = {
    'revenue': ('Faturamento', 'brl'), 'sales': ('Número de vendas', 'num'), 'ticket': ('Ticket médio', 'brl'),
    'new_clients': ('Clientes novos (primeira compra)', 'num'), 'renewals': ('Renovações', 'num'), 'upsell': ('Vendas de próximo produto', 'num'),
    'reactivations': ('Clientes reativados', 'num'), 'product_qty': ('Unidades de um item', 'num'), 'appointments': ('Agendamentos', 'num'),
    'attended': ('Comparecimentos', 'num'), 'assisted': ('Oportunidades passadas que viraram venda', 'num'),
    'response': ('Tempo da 1ª resposta (min)', 'min'), 'quality': ('Nota do atendimento', 'score'),
}
ADDITIVE = {'revenue', 'sales', 'new_clients', 'renewals', 'upsell', 'reactivations', 'product_qty', 'appointments', 'attended', 'assisted'}
LOWER_BETTER = {'response'}
DEFAULT_POINTS = {'sale': 10, 'renewal': 5, 'upsell': 5, 'reactivation': 5, 'fast_reply': 2, 'appointment': 3, 'quality': 5, 'assisted': 5, 'goal': 20, 'super': 40}
POINT_LABELS = {'sale': 'Venda', 'renewal': 'Renovação (a mais)', 'upsell': 'Próximo produto (a mais)', 'reactivation': 'Cliente reativado (a mais)',
                'fast_reply': '1ª resposta em até 5 min', 'appointment': 'Agendamento', 'quality': 'Nota de atendimento 9 ou mais',
                'assisted': 'Oportunidade passada que virou venda', 'goal': 'Bateu a meta', 'super': 'Bateu a supermeta'}
LEVELS = [(0, 'Bronze'), (300, 'Prata'), (900, 'Ouro'), (2000, 'Diamante')]
ACHIEVEMENTS = {
    'first_sale': ('Primeira venda', 'Fez a primeira venda'), 'ten_sales': ('Dez vendas', 'Chegou a 10 vendas'),
    'fifty_sales': ('Cinquenta vendas', 'Chegou a 50 vendas'), 'big_month': ('Mês de ouro', 'Faturou R$ 50 mil num mês'),
    'rescuer': ('Rei do resgate', 'Reativou 5 clientes'), 'renewer': ('Renovador', 'Fez 10 renovações'),
    'lightning': ('Relâmpago', '50 primeiras respostas em até 5 minutos'), 'loved': ('Querido pelos clientes', '10 atendimentos com nota 9 ou mais'),
    'team_player': ('Jogador de equipe', '10 oportunidades passadas viraram venda'), 'goal_hitter': ('Bate-meta', 'Bateu 3 metas'),
}

def settings_of(company):
    s = company.get('gamify') or {}
    return {'goals': bool(s.get('goals')), 'awards': bool(s.get('awards')), 'game': bool(s.get('game')), 'ranking': s.get('ranking') or 'all',
            'points': {**DEFAULT_POINTS, **(s.get('points') or {})}}

# ---------- períodos ----------
def period_range(company, kind, start=None, end=None, at=None):
    tz = agenda.tz_of(company)
    d = (at or now()).astimezone(tz).date()
    if kind == 'week': a = d - timedelta(days=d.weekday()); b = a + timedelta(days=6); key = f'{a.isocalendar()[0]}-S{a.isocalendar()[1]:02d}'
    elif kind == 'quarter':
        q = (d.month - 1) // 3; a = d.replace(month=q * 3 + 1, day=1); m = q * 3 + 3
        b = d.replace(month=m, day=calendar.monthrange(d.year, m)[1]); key = f'{d.year}-T{q + 1}'
    elif kind == 'custom':
        a = datetime.strptime(start, '%Y-%m-%d').date(); b = datetime.strptime(end, '%Y-%m-%d').date(); key = f'{start}_{end}'
    else: a = d.replace(day=1); b = d.replace(day=calendar.monthrange(d.year, d.month)[1]); key = d.strftime('%Y-%m')
    return a, b, datetime.combine(a, dtime(0), tz), datetime.combine(b + timedelta(days=1), dtime(0), tz), key

# ---------- o que cada um fez no período ----------
class Agg:
    def __init__(self):
        self.revenue = 0.0; self.sales = 0.0; self.count = 0; self.new_clients = 0.0; self.renewals = 0.0; self.upsell = 0.0; self.reactivations = 0.0
        self.qty = {}; self.appointments = 0; self.attended = 0; self.assisted = 0; self.replies = []; self.quality = []; self.points = 0.0
        self.fast = 0; self.q9 = 0

    def value(self, metric, product_id=None):
        if metric == 'ticket': return round(self.revenue / self.sales, 2) if self.sales else 0
        if metric == 'product_qty': return round(self.qty.get(product_id, 0), 2)
        if metric == 'response': return round(median(self.replies), 1) if self.replies else None
        if metric == 'quality': return round(sum(self.quality) / len(self.quality), 1) if self.quality else None
        return round(getattr(self, metric), 2)

async def compute(company, t0, t1):
    """Agrega por pessoa e para a empresa ('_all') tudo o que as metas, desafios e pontos usam."""
    cid = company['id']; pts = settings_of(company)['points']
    inactive = timedelta(days=company.get('inactive_days') or 90)
    aggs: Dict[str, Agg] = {}
    A = lambda k: aggs.setdefault(k, Agg())
    products = {p['id']: p for p in await db.products.find({'company_id': cid}, {'_id': 0, 'id': 1, 'duration_days': 1, 'next_ids': 1}).to_list(2000)}
    history = {}
    for s in await db.sales.find({'company_id': cid, 'status': 'ok', 'date': {'$lt': t1}}, {'_id': 0, 'id': 1, 'contact_id': 1, 'date': 1, 'items': 1, 'total': 1, 'sellers': 1}).sort('date', 1).to_list(100000):
        h = history.setdefault(s['contact_id'], {'products': set(), 'last': None, 'n': 0})
        d = aware(s['date'])
        if d >= t0:
            first = h['n'] == 0
            react = bool(h['last'] and d - h['last'] > inactive)
            ren = sum(1 for i in s['items'] if i.get('product_id') in h['products'] and (products.get(i['product_id']) or {}).get('duration_days'))
            ups = sum(1 for i in s['items'] if i.get('product_id') and i['product_id'] not in h['products'] and any(i['product_id'] in ((products.get(p) or {}).get('next_ids') or []) for p in h['products']))
            sellers = s.get('sellers') or []
            for k, share in [('_all', 1.0)] + [(x['user_id'], x['pct'] / 100) for x in sellers]:
                a = A(k)
                a.revenue += s['total'] * share; a.sales += share; a.count += 1
                if first: a.new_clients += share
                if react: a.reactivations += share
                a.renewals += min(ren, 1) * share; a.upsell += min(ups, 1) * share
                for i in s['items']:
                    if i.get('product_id'): a.qty[i['product_id']] = a.qty.get(i['product_id'], 0) + (i.get('qty') or 1) * share
                if k != '_all':
                    a.points += share * (pts['sale'] + (pts['renewal'] if ren else 0) + (pts['upsell'] if ups else 0) + (pts['reactivation'] if react else 0))
        h['n'] += 1; h['last'] = d
        for i in s['items']:
            if i.get('product_id'): h['products'].add(i['product_id'])
    for ap in await db.appointments.find({'company_id': cid, 'created_at': {'$gte': t0, '$lt': t1}}, {'_id': 0, 'by_id': 1, 'status': 1}).to_list(20000):
        for k in ['_all'] + ([ap['by_id']] if ap.get('by_id') else []):
            A(k).appointments += 1
            if ap.get('status') == 'done': A(k).attended += 1
        if ap.get('by_id'): A(ap['by_id']).points += pts['appointment']
    from reports import first_reply_minutes
    cids = [c['id'] for c in await db.contacts.find({'company_id': cid, 'created_at': {'$gte': t0, '$lt': t1}}, {'_id': 0, 'id': 1}).to_list(5000)]
    for _, (m, by_id, is_ai) in (await first_reply_minutes(cid, cids)).items():
        if is_ai: continue
        A('_all').replies.append(m)
        if by_id:
            A(by_id).replies.append(m)
            if m <= 5: A(by_id).fast += 1; A(by_id).points += pts['fast_reply']
    for q in await db.quality.find({'company_id': cid, 'at': {'$gte': t0, '$lt': t1}}, {'_id': 0, 'agents': 1, 'score': 1}).to_list(5000):
        A('_all').quality.append(q['score'])
        for u in q.get('agents') or []:
            A(u).quality.append(q['score'])
            if q['score'] >= 9: A(u).q9 += 1; A(u).points += pts['quality']
    sold = {s['contact_id'] for s in await db.sales.find({'company_id': cid, 'status': 'ok', 'date': {'$gte': t0}}, {'_id': 0, 'contact_id': 1}).to_list(50000)}
    seen = set()
    for t in await db.transfers.find({'company_id': cid, 'at': {'$gte': t0, '$lt': t1}, 'by_id': {'$ne': None}, 'auto': {'$ne': True}}, {'_id': 0, 'by_id': 1, 'contact_id': 1}).to_list(50000):
        k = (t['by_id'], t['contact_id'])
        if t['contact_id'] in sold and k not in seen:
            seen.add(k); A(t['by_id']).assisted += 1; A(t['by_id']).points += pts['assisted']; A('_all').assisted += 1
    for g in await db.goal_hits.find({'company_id': cid, 'at': {'$gte': t0, '$lt': t1}, 'owner_kind': 'user'}, {'_id': 0, 'owner_id': 1, 'level': 1}).to_list(5000):
        A(g['owner_id']).points += pts['super'] if g['level'] == 'super' else pts['goal']
    return aggs

async def team_agg(aggs, team):
    a = Agg()
    for m in team.get('member_ids') or []:
        x = aggs.get(m)
        if not x: continue
        for f in ('revenue', 'sales', 'new_clients', 'renewals', 'upsell', 'reactivations', 'appointments', 'attended', 'assisted', 'points', 'fast', 'q9'):
            setattr(a, f, getattr(a, f) + getattr(x, f))
        a.replies += x.replies; a.quality += x.quality
        for k, v in x.qty.items(): a.qty[k] = a.qty.get(k, 0) + v
    return a

# ---------- metas ----------
class GoalIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    metric: Literal['revenue', 'sales', 'ticket', 'new_clients', 'renewals', 'upsell', 'reactivations', 'product_qty', 'appointments', 'attended', 'assisted', 'response', 'quality']
    scope: Literal['company', 'team', 'user'] = 'company'
    target_id: Optional[str] = None
    period: Literal['week', 'month', 'quarter', 'custom'] = 'month'
    start: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: Optional[str] = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    target: float = Field(gt=0, le=1_000_000_000)
    floor_pct: int = Field(default=80, ge=10, le=100)
    super_pct: int = Field(default=120, ge=100, le=500)
    product_id: Optional[str] = None
    award_meta: str = Field(default='', max_length=120)
    award_super: str = Field(default='', max_length=120)
    active: bool = True

async def check_goal_in(user, b: GoalIn):
    if b.scope == 'team' and not await db.teams.find_one({'id': b.target_id, 'company_id': user['company_id']}): raise HTTPException(400, 'Escolha a equipe.')
    if b.scope == 'user' and not await db.users.find_one({'id': b.target_id, 'company_id': user['company_id']}): raise HTTPException(400, 'Escolha a pessoa.')
    if b.metric == 'product_qty' and not await db.products.find_one({'id': b.product_id, 'company_id': user['company_id']}): raise HTTPException(400, 'Escolha o item.')
    if b.period == 'custom' and not (b.start and b.end and b.end >= b.start): raise HTTPException(400, 'Informe início e fim.')

def progress(g, agg_val, a, b, t0, t1):
    t = now()
    total = (t1 - t0).total_seconds(); el = min(1.0, max(0.0, (t - t0).total_seconds() / total)) if total else 1
    v = agg_val
    if v is None: pct = 0
    elif g['metric'] in LOWER_BETTER: pct = round(100 * g['target'] / v, 1) if v else 100.0
    else: pct = round(100 * v / g['target'], 1)
    level = 'super' if pct >= g['super_pct'] else 'meta' if pct >= 100 else 'minimo' if pct >= g['floor_pct'] else 'abaixo'
    out = {'value': v, 'pct': pct, 'level': level, 'elapsed': round(100 * el, 1), 'days_left': max(0, (t1 - max(t, t0)).days), 'start': a.isoformat(), 'end': b.isoformat()}
    if g['metric'] in ADDITIVE and v is not None:
        out['projection'] = round(v / el, 2) if el > 0.02 else None
        out['projection_pct'] = round(100 * out['projection'] / g['target'], 1) if out['projection'] is not None else None
        days = max(1, (t1 - max(t, t0)).days)
        out['per_day'] = round(max(0, g['target'] - v) / days, 2) if t < t1 else 0
        out['on_track'] = pct >= 100 * el - 5
    return out

async def evaluate(company, goals, at=None):
    """Progresso de cada meta, calculando o período de cada uma (sem repetir contas iguais)."""
    cache = {}; teams = {t['id']: t for t in await db.teams.find({'company_id': company['id']}, {'_id': 0}).to_list(50)}
    out = []
    for g in goals:
        a, b, t0, t1, key = period_range(company, g['period'], g.get('start'), g.get('end'), at)
        if (t0, t1) not in cache: cache[(t0, t1)] = await compute(company, t0, t1)
        aggs = cache[(t0, t1)]
        agg = aggs.get('_all', Agg()) if g['scope'] == 'company' else aggs.get(g['target_id'], Agg()) if g['scope'] == 'user' else await team_agg(aggs, teams.get(g['target_id'], {}))
        out.append({**g, 'key': key, **progress(g, agg.value(g['metric'], g.get('product_id')), a, b, t0, t1)})
    return out

async def record_hits(company, rows):
    """Bateu a meta ou a supermeta: registra (uma vez por período), dá o prêmio e comemora."""
    s = settings_of(company)
    for r in rows:
        for level in ('meta', 'super'):
            if not ((level == 'meta' and r['pct'] >= 100) or (level == 'super' and r['pct'] >= r['super_pct'])): continue
            if r['metric'] not in ADDITIVE and r['days_left'] > 0: continue  # média/tempo só vale no fim do período
            k = f"{r['id']}:{r['key']}:{level}"
            if await db.goal_hits.find_one({'key': k}): continue
            try: await db.goal_hits.insert_one({'key': k, 'company_id': company['id'], 'goal_id': r['id'], 'goal': r['name'], 'owner_kind': r['scope'], 'owner_id': r.get('target_id'),
                                                'owner_name': r.get('owner_name'), 'level': level, 'at': now()})
            except Exception: continue
            award = r.get('award_super' if level == 'super' else 'award_meta')
            if s['awards'] and award:
                await db.awards.insert_one({'id': uid(), 'company_id': company['id'], 'goal_id': r['id'], 'goal': r['name'], 'level': level, 'text': award,
                                            'owner_kind': r['scope'], 'owner_id': r.get('target_id'), 'owner_name': r.get('owner_name'), 'period': r['key'],
                                            'status': 'won', 'at': now()})

async def owner_names(company_id):
    users = {u['id']: u['name'] for u in await db.users.find({'company_id': company_id}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}
    teams = {t['id']: t['name'] for t in await db.teams.find({'company_id': company_id}, {'_id': 0, 'id': 1, 'name': 1}).to_list(50)}
    return users, teams

def owner_name(g, users, teams):
    return 'Empresa' if g['scope'] == 'company' else teams.get(g.get('target_id'), 'Equipe') if g['scope'] == 'team' else users.get(g.get('target_id'), 'Pessoa')

@router.get('/goals/settings')
async def get_settings(user=Depends(current_user)):
    return {**settings_of(user['company']), 'labels': POINT_LABELS, 'metrics': {k: v[0] for k, v in METRICS.items()}}

class SettingsIn(BaseModel):
    goals: bool = False
    awards: bool = False
    game: bool = False
    ranking: Literal['all', 'podium', 'self'] = 'all'
    points: Dict[str, int] = Field(default_factory=dict)

@router.put('/goals/settings')
async def put_settings(body: SettingsIn, request: Request, user=Depends(managers)):
    pts = {k: max(0, min(1000, int(v))) for k, v in body.points.items() if k in DEFAULT_POINTS}
    g = {'goals': body.goals, 'awards': body.awards, 'game': body.game, 'ranking': body.ranking, 'points': {**DEFAULT_POINTS, **pts}}
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'gamify': g}})
    await audit(request, user, 'Alterou metas e gamificação', ', '.join(k for k in ('goals', 'awards', 'game') if g[k]) or 'tudo desligado')
    return g

@router.get('/goals')
async def list_goals(user=Depends(managers)):
    gs = await db.goals.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(500)
    users, teams = await owner_names(user['company_id'])
    for g in gs: g['owner_name'] = owner_name(g, users, teams)
    rows = await evaluate(user['company'], [g for g in gs if g.get('active', True)])
    by = {r['id']: r for r in rows}
    return [by.get(g['id'], g) for g in gs]

@router.post('/goals')
async def add_goal(body: GoalIn, request: Request, user=Depends(managers)):
    await check_goal_in(user, body)
    if await db.goals.count_documents({'company_id': user['company_id']}) >= 300: raise HTTPException(400, 'Limite de 300 metas.')
    g = {'id': uid(), 'company_id': user['company_id'], **body.model_dump(), 'name': body.name.strip(), 'parent_id': None, 'created_at': now(), 'by': user['name']}
    if body.scope == 'company': g['target_id'] = None
    await db.goals.insert_one(g.copy())
    await audit(request, user, 'Criou meta', g['name'])
    g.pop('_id', None)
    return g

@router.put('/goals/{gid}')
async def edit_goal(gid: str, body: GoalIn, request: Request, user=Depends(managers)):
    g = await db.goals.find_one({'id': gid, 'company_id': user['company_id']}, {'_id': 0})
    if not g: raise HTTPException(404, 'Meta não encontrada.')
    await check_goal_in(user, body)
    upd = {**body.model_dump(), 'name': body.name.strip(), 'updated_at': now()}
    if body.scope == 'company': upd['target_id'] = None
    await db.goals.update_one({'id': gid}, {'$set': upd})
    return {**g, **upd}

@router.delete('/goals/{gid}')
async def del_goal(gid: str, user=Depends(managers)):
    r = await db.goals.delete_one({'id': gid, 'company_id': user['company_id']})
    if not r.deleted_count: raise HTTPException(404, 'Meta não encontrada.')
    await db.goals.delete_many({'parent_id': gid})
    return {'ok': True}

class SplitIn(BaseModel):
    mode: Literal['equal', 'weights'] = 'equal'
    weights: Dict[str, float] = Field(default_factory=dict)

@router.post('/goals/{gid}/split')
async def split(gid: str, body: SplitIn, request: Request, user=Depends(managers)):
    g = await db.goals.find_one({'id': gid, 'company_id': user['company_id']}, {'_id': 0})
    if not g or g['scope'] != 'team': raise HTTPException(400, 'Só a meta de uma equipe pode ser dividida.')
    if g['metric'] not in ADDITIVE: raise HTTPException(400, 'Esse tipo de meta não se divide (é média ou tempo). Crie uma meta igual para cada pessoa.')
    team = await db.teams.find_one({'id': g['target_id']}, {'_id': 0})
    ids = [m for m in team.get('member_ids') or []]
    if not ids: raise HTTPException(400, 'A equipe não tem pessoas.')
    w = {i: (body.weights.get(i, 0) if body.mode == 'weights' else 1) for i in ids}
    tot = sum(w.values())
    if tot <= 0: raise HTTPException(400, 'Informe os pesos.')
    await db.goals.delete_many({'parent_id': gid})
    users, _ = await owner_names(user['company_id'])
    made = []
    for i in ids:
        if not w[i]: continue
        c = {**{k: g[k] for k in ('metric', 'period', 'start', 'end', 'floor_pct', 'super_pct', 'product_id', 'award_meta', 'award_super', 'active')},
             'id': uid(), 'company_id': user['company_id'], 'name': f'{g["name"]} · {users.get(i, "")}'.strip(), 'scope': 'user', 'target_id': i,
             'target': round(g['target'] * w[i] / tot, 2), 'parent_id': gid, 'created_at': now(), 'by': user['name']}
        await db.goals.insert_one(c.copy()); made.append({k: v for k, v in c.items() if k != '_id'})
    await audit(request, user, 'Dividiu meta da equipe', g['name'])
    return made

# ---------- desafios ----------
class ChallengeIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    metric: Literal['revenue', 'sales', 'new_clients', 'renewals', 'upsell', 'reactivations', 'product_qty', 'appointments', 'attended', 'assisted', 'points']
    product_id: Optional[str] = None
    among: Literal['users', 'teams'] = 'users'
    start: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    award: str = Field(default='', max_length=120)
    winners: int = Field(default=1, ge=1, le=3)

async def leaderboard(company, ch):
    a, b, t0, t1, key = period_range(company, 'custom', ch['start'], ch['end'])
    aggs = await compute(company, t0, t1)
    users, teams = await owner_names(company['id'])
    val = lambda x: round(x.points, 1) if ch['metric'] == 'points' else x.value(ch['metric'], ch.get('product_id'))
    if ch['among'] == 'teams':
        rows = [{'id': t['id'], 'name': t['name'], 'value': val(await team_agg(aggs, t))} for t in await db.teams.find({'company_id': company['id']}, {'_id': 0}).to_list(50)]
    else:
        active = {u['id'] for u in await db.users.find({'company_id': company['id'], 'active': True}, {'_id': 0, 'id': 1}).to_list(500)}
        rows = [{'id': k, 'name': users.get(k, '?'), 'value': val(x)} for k, x in aggs.items() if k != '_all' and k in users and (k in active or val(x))]
        rows += [{'id': u, 'name': users[u], 'value': 0} for u in active if u not in aggs]
    rows.sort(key=lambda r: -(r['value'] or 0))
    t = now()
    return {**ch, 'state': 'agendado' if t < t0 else 'encerrado' if t >= t1 else 'valendo', 'days_left': max(0, (t1 - max(t, t0)).days), 'rows': rows}

@router.get('/challenges')
async def list_challenges(user=Depends(current_user)):
    out = []
    for ch in await db.challenges.find({'company_id': user['company_id']}, {'_id': 0}).sort('start', -1).to_list(100):
        out.append(await leaderboard(user['company'], ch))
    return out

@router.post('/challenges')
async def add_challenge(body: ChallengeIn, request: Request, user=Depends(managers)):
    if body.end < body.start: raise HTTPException(400, 'O fim precisa ser depois do início.')
    if body.metric == 'product_qty' and not await db.products.find_one({'id': body.product_id, 'company_id': user['company_id']}): raise HTTPException(400, 'Escolha o item.')
    ch = {'id': uid(), 'company_id': user['company_id'], **body.model_dump(), 'name': body.name.strip(), 'created_at': now(), 'by': user['name'], 'closed': False}
    await db.challenges.insert_one(ch.copy())
    await audit(request, user, 'Criou desafio', ch['name'])
    ch.pop('_id', None)
    return ch

@router.delete('/challenges/{cid}')
async def del_challenge(cid: str, user=Depends(managers)):
    r = await db.challenges.delete_one({'id': cid, 'company_id': user['company_id']})
    if not r.deleted_count: raise HTTPException(404, 'Desafio não encontrado.')
    return {'ok': True}

async def close_challenges(company):
    """Desafio que terminou: os primeiros ganham o prêmio (uma vez)."""
    s = settings_of(company)
    for ch in await db.challenges.find({'company_id': company['id'], 'closed': {'$ne': True}, 'end': {'$lt': now().astimezone(agenda.tz_of(company)).date().isoformat()}}, {'_id': 0}).to_list(50):
        lb = await leaderboard(company, ch)
        for i, r in enumerate([r for r in lb['rows'] if r['value']][:ch['winners']]):
            await db.goal_hits.insert_one({'key': f"ch:{ch['id']}:{i}", 'company_id': company['id'], 'goal_id': ch['id'], 'goal': ch['name'], 'owner_kind': 'team' if ch['among'] == 'teams' else 'user',
                                           'owner_id': r['id'], 'owner_name': r['name'], 'level': f'{i + 1}º lugar', 'at': now()})
            if s['awards'] and ch.get('award'):
                await db.awards.insert_one({'id': uid(), 'company_id': company['id'], 'goal_id': ch['id'], 'goal': ch['name'], 'level': f'{i + 1}º lugar', 'text': ch['award'],
                                            'owner_kind': 'team' if ch['among'] == 'teams' else 'user', 'owner_id': r['id'], 'owner_name': r['name'], 'period': ch['end'], 'status': 'won', 'at': now()})
        await db.challenges.update_one({'id': ch['id']}, {'$set': {'closed': True}})

# ---------- prêmios ----------
@router.get('/awards')
async def awards(user=Depends(current_user)):
    q = {'company_id': user['company_id']}
    if user['role'] == 'agent':
        from teams import my_team_ids
        q['$or'] = [{'owner_id': user['id']}, {'owner_id': {'$in': await my_team_ids(user)}}, {'owner_kind': 'company'}]
    return await db.awards.find(q, {'_id': 0}).sort('at', -1).to_list(200)

@router.post('/awards/{aid}/deliver')
async def deliver_award(aid: str, request: Request, user=Depends(managers)):
    a = await db.awards.find_one({'id': aid, 'company_id': user['company_id']}, {'_id': 0})
    if not a: raise HTTPException(404, 'Prêmio não encontrado.')
    await db.awards.update_one({'id': aid}, {'$set': {'status': 'delivered', 'delivered_at': now(), 'delivered_by': user['name']}})
    await audit(request, user, 'Entregou prêmio', f'{a["text"]} · {a.get("owner_name")}')
    return {'ok': True}

# ---------- painel ----------
def level_of(points):
    cur = LEVELS[0]; nxt = None
    for i, l in enumerate(LEVELS):
        if points >= l[0]: cur = l; nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    return {'name': cur[1], 'points': round(points), 'next': nxt[1] if nxt else None, 'to_next': round(nxt[0] - points) if nxt else 0,
            'pct': round(100 * (points - cur[0]) / (nxt[0] - cur[0])) if nxt else 100}

async def achievements_of(company, uid_):
    t1 = now(); t0 = t1 - timedelta(days=730)
    x = (await compute(company, t0, t1)).get(uid_, Agg())
    hits = await db.goal_hits.count_documents({'company_id': company['id'], 'owner_kind': 'user', 'owner_id': uid_})
    best = 0.0
    for m in range(12):  # melhor mês nos últimos 12
        at = now() - timedelta(days=30 * m)
        a, b, s0, s1, _ = period_range(company, 'month', at=at)
        sales = await db.sales.find({'company_id': company['id'], 'status': 'ok', 'date': {'$gte': s0, '$lt': s1}, 'sellers.user_id': uid_}, {'_id': 0, 'total': 1, 'sellers': 1}).to_list(10000)
        best = max(best, sum(s['total'] * next(y['pct'] for y in s['sellers'] if y['user_id'] == uid_) / 100 for s in sales))
    have = {'first_sale': x.count >= 1, 'ten_sales': x.count >= 10, 'fifty_sales': x.count >= 50, 'big_month': best >= 50000, 'rescuer': x.reactivations >= 5,
            'renewer': x.renewals >= 10, 'lightning': x.fast >= 50, 'loved': x.q9 >= 10, 'team_player': x.assisted >= 10, 'goal_hitter': hits >= 3}
    return [{'key': k, 'name': v[0], 'desc': v[1], 'done': have[k]} for k, v in ACHIEVEMENTS.items()]

@router.get('/goals/board')
async def board(user=Depends(current_user)):
    company = user['company']; s = settings_of(company)
    if not (s['goals'] or s['game'] or s['awards']): return {'settings': s, 'enabled': False}
    users, teams_n = await owner_names(user['company_id'])
    from teams import my_team_ids
    mine_teams = set(await my_team_ids(user))
    out = {'settings': s, 'enabled': True, 'me': {'id': user['id'], 'name': user['name']}}
    if s['goals']:
        gs = [g for g in await db.goals.find({'company_id': user['company_id'], 'active': True}, {'_id': 0}).to_list(500)]
        for g in gs: g['owner_name'] = owner_name(g, users, teams_n)
        rows = await evaluate(company, gs)
        await record_hits(company, rows)
        mgr = user['role'] in ('owner', 'manager')
        vis = lambda r: mgr or r['scope'] == 'company' or (r['scope'] == 'team' and r['target_id'] in mine_teams) or (r['scope'] == 'user' and r['target_id'] == user['id'])
        rows = [r for r in rows if vis(r)]
        out['goals'] = rows
        out['my_goals'] = [r for r in rows if r['scope'] == 'user' and r['target_id'] == user['id']]
    if s['game']:
        a, b, t0, t1, _ = period_range(company, 'month')
        aggs = await compute(company, t0, t1)
        year = await compute(company, now() - timedelta(days=365), now())
        active = {u['id'] for u in await db.users.find({'company_id': user['company_id'], 'active': True}, {'_id': 0, 'id': 1}).to_list(500)}
        rank = sorted([{'id': u, 'name': users[u], 'points': round(aggs.get(u, Agg()).points), 'revenue': round(aggs.get(u, Agg()).revenue, 2), 'sales': aggs.get(u, Agg()).count,
                        'level': level_of(year.get(u, Agg()).points)['name']} for u in active], key=lambda r: (-r['points'], -r['revenue']))
        for i, r in enumerate(rank): r['pos'] = i + 1
        me = next((r for r in rank if r['id'] == user['id']), None)
        if s['ranking'] == 'podium' and user['role'] == 'agent': rank = rank[:3]
        elif s['ranking'] == 'self' and user['role'] == 'agent': rank = [me] if me else []
        tr = []
        for t in await db.teams.find({'company_id': user['company_id']}, {'_id': 0}).to_list(50):
            x = await team_agg(aggs, t); tr.append({'id': t['id'], 'name': t['name'], 'color': t.get('color'), 'points': round(x.points), 'revenue': round(x.revenue, 2)})
        tr.sort(key=lambda r: -r['points'])
        out.update(ranking=rank, team_ranking=tr if s['ranking'] != 'self' or user['role'] != 'agent' else [], my_rank=me, total_people=len(active),
                   level=level_of(year.get(user['id'], Agg()).points), period={'start': a.isoformat(), 'end': b.isoformat()},
                   achievements=await achievements_of(company, user['id']))
    if s['game'] or s['awards']:
        out['challenges'] = [await leaderboard(company, ch) for ch in await db.challenges.find({'company_id': user['company_id']}, {'_id': 0}).sort('start', -1).to_list(20)]
        out['challenges'] = [c for c in out['challenges'] if c['state'] != 'encerrado' or c['days_left'] == 0][:6]
    if s['awards']:
        out['awards'] = (await awards(user))[:20]
    return out

_last = {}
async def tick():
    """No ciclo das automações (de 10 em 10 minutos): registra metas batidas e fecha desafios."""
    t = now()
    for c in await db.companies.find({'status': 'active', '$or': [{'gamify.goals': True}, {'gamify.awards': True}, {'gamify.game': True}]}, {'_id': 0}).to_list(2000):
        if _last.get(c['id']) and t - _last[c['id']] < timedelta(minutes=10): continue
        _last[c['id']] = t
        try:
            if settings_of(c)['goals']:
                users, teams = await owner_names(c['id'])
                gs = await db.goals.find({'company_id': c['id'], 'active': True}, {'_id': 0}).to_list(500)
                for g in gs: g['owner_name'] = owner_name(g, users, teams)
                await record_hits(c, await evaluate(c, gs))
            await close_challenges(c)
        except Exception: log.exception('falha nas metas')
