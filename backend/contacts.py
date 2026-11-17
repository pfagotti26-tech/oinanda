"""Lista de contatos: a base inteira numa tela, com busca, visões, filtros de um clique, ordenação e paginação.

Visões são recortes prontos (todos, clientes, quem ainda não comprou, esperando resposta, sem responsável) mais os
segmentos salvos da conta. Os filtros de um clique somam em cima da visão escolhida. As ações em vários de uma vez
valem para os marcados ou para todos os que a lista está mostrando.
"""
import csv, io, unicodedata
from datetime import datetime, timezone
from typing import Optional, List, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from core import db, now, audit, current_user, managers
from whatsapp import aware
from inbox import visible_q, waiting
from origins import status_of
from wallet import Rule, match, seg_ctx, state_of

router = APIRouter()
VIEWS = [('all', 'Todos'), ('clientes', 'Clientes'), ('prospects', 'Ainda não compraram'), ('waiting', 'Esperando resposta'), ('noowner', 'Sem responsável')]
SEG_TABS = 8      # segmentos salvos que viram aba
BULK_MAX = 5000   # contatos por ação em massa
PROJ = {'_id': 0, 'id': 1, 'name': 1, 'wa_id': 1, 'ig_id': 1, 'ig_username': 1, 'channel': 1, 'email': 1, 'stage': 1, 'tags': 1, 'team_id': 1, 'assigned_to': 1,
        'origin': 1, 'ad': 1, 'sales_count': 1, 'value': 1, 'last_sale_at': 1, 'created_at': 1, 'holdings': 1, 'next_expiry': 1, 'opt_out': 1,
        'last_in_at': 1, 'last_at': 1, 'last_dir': 1, 'resolved_at': 1, 'noreply_at': 1}
OLD = datetime(1970, 1, 1, tzinfo=timezone.utc)

class TagsF(BaseModel):
    mode: Literal['any', 'all', 'none'] = 'any'
    values: List[str] = Field(default_factory=list, max_length=20)

class LastF(BaseModel):
    op: Literal['more', 'less'] = 'more'
    days: int = Field(ge=0, le=3650)

class Filters(BaseModel):
    stage: List[str] = Field(default_factory=list, max_length=40)
    tags: Optional[TagsF] = None
    owner: List[str] = Field(default_factory=list, max_length=60)  # '' = sem responsável
    last: Optional[LastF] = None
    product: Optional[str] = Field(default=None, max_length=60)    # tem este item ativo

class ListIn(BaseModel):
    view: str = Field(default='all', max_length=60)
    q: str = Field(default='', max_length=80)
    filters: Filters = Field(default_factory=Filters)
    sort: Literal['last', 'name', 'spent', 'created', 'expiry'] = 'last'
    dir: Literal['asc', 'desc'] = 'desc'
    page: int = Field(default=1, ge=1, le=100000)
    size: int = Field(default=25, ge=1, le=100)
    facets: bool = False

def norm(s):
    return ''.join(ch for ch in unicodedata.normalize('NFD', str(s or '').lower()) if unicodedata.category(ch) != 'Mn')

def spent_of(c): return (c.get('value') or 0) if c.get('sales_count') else 0

async def base(user, body: ListIn):
    """Contatos que o usuário pode ver, os segmentos que viram aba e o contexto para avaliar as regras deles."""
    segs = await db.segments.find({'company_id': user['company_id']}, {'_id': 0}).sort('created_at', -1).to_list(SEG_TABS)
    for s in segs: s['rules'] = [Rule(**r) for r in s['rules']]
    rules = [r for s in segs for r in s['rules']]
    if body.filters.product: rules.append(Rule(field='product', op='active', value=body.filters.product))
    ctx = await seg_ctx(user, rules)
    rows = await db.contacts.find(visible_q(user), PROJ).to_list(50000)
    return rows, segs, ctx

def in_view(c, view, segs, ctx):
    if view == 'all': return True
    if view == 'clientes': return status_of(c, ctx['inactive_days']) != 'prospect'
    if view == 'prospects': return status_of(c, ctx['inactive_days']) == 'prospect'
    if view == 'waiting': return waiting(c)
    if view == 'noowner': return not c.get('assigned_to')
    if view.startswith('seg:'):
        s = next((x for x in segs if x['id'] == view[4:]), None)
        if not s: raise HTTPException(404, 'Esse segmento não existe mais.')
        return all(match(c, r, ctx) for r in s['rules'])
    raise HTTPException(400, 'Visão desconhecida.')

def passes(c, f: Filters, ctx, t):
    if f.stage and (c.get('stage') or ctx['first_stage']) not in f.stage: return False
    if f.tags and f.tags.values:
        have = set(c.get('tags') or []); want = set(f.tags.values)
        if f.tags.mode == 'any' and not have & want: return False
        if f.tags.mode == 'all' and not want <= have: return False
        if f.tags.mode == 'none' and have & want: return False
    if f.owner and (c.get('assigned_to') or '') not in f.owner: return False
    if f.last:
        last = aware(c.get('last_at'))
        if not last: return f.last.op == 'more'   # nunca conversou conta como "há mais de"
        days = (t - last).days
        if (days > f.last.days) != (f.last.op == 'more'): return False
    if f.product and not any((x.get('product_id') or x['key']) == f.product and state_of(x, t) != 'vencido' for x in c.get('holdings') or []): return False
    return True

def found(c, q):
    if not q: return True
    digits = ''.join(ch for ch in q if ch.isdigit())
    if digits and len(digits) >= 3 and digits in (c.get('wa_id') or ''): return True
    hay = ' '.join([norm(c.get('name')), norm(c.get('ig_username')), norm(c.get('email')), ' '.join(norm(x) for x in c.get('tags') or [])])
    return all(w in hay for w in norm(q).lstrip('@').split())

def select(rows, segs, ctx, body: ListIn):
    t = now()
    return [c for c in rows if in_view(c, body.view, segs, ctx) and passes(c, body.filters, ctx, t) and found(c, body.q.strip())]

def order(rows, sort, direction):
    rev = direction == 'desc'
    if sort == 'name': return sorted(rows, key=lambda c: norm(c.get('name')), reverse=rev)
    if sort == 'spent': return sorted(rows, key=spent_of, reverse=rev)
    if sort == 'created': return sorted(rows, key=lambda c: aware(c.get('created_at')) or OLD, reverse=rev)
    if sort == 'expiry':  # quem não tem vencimento vai sempre para o fim
        has = sorted([c for c in rows if c.get('next_expiry')], key=lambda c: aware(c['next_expiry']), reverse=rev)
        return has + [c for c in rows if not c.get('next_expiry')]
    return sorted(rows, key=lambda c: aware(c.get('last_at')) or aware(c.get('created_at')) or OLD, reverse=rev)

def row_out(c, names, ctx):
    hs = c.get('holdings') or []
    return {'id': c['id'], 'name': c['name'], 'wa_id': c.get('wa_id'), 'ig_id': bool(c.get('ig_id')), 'ig_username': c.get('ig_username'), 'channel': c.get('channel'),
            'stage': c.get('stage') or ctx['first_stage'], 'tags': c.get('tags') or [], 'assigned_to': c.get('assigned_to'), 'owner': names.get(c.get('assigned_to')),
            'kind': status_of(c, ctx['inactive_days']), 'last_at': c.get('last_at'), 'last_dir': c.get('last_dir'), 'waiting': waiting(c),
            'spent': spent_of(c), 'purchases': c.get('sales_count') or 0, 'next_expiry': c.get('next_expiry'), 'opt_out': bool(c.get('opt_out')),
            'has': [{'name': x['name'], 'state': state_of(x), 'expires_at': x.get('expires_at')} for x in hs][:3]}

async def names_of(company_id):
    return {u['id']: u['name'] for u in await db.users.find({'company_id': company_id}, {'_id': 0, 'id': 1, 'name': 1}).to_list(500)}

@router.post('/contacts/list')
async def list_contacts(body: ListIn, user=Depends(current_user)):
    rows, segs, ctx = await base(user, body)
    sel = order(select(rows, segs, ctx, body), body.sort, body.dir)
    names = await names_of(user['company_id'])
    pages = max(1, -(-len(sel) // body.size)); page = min(body.page, pages)
    out = {'count': len(sel), 'total': len(rows), 'reachable': sum(1 for c in sel if not c.get('opt_out')), 'spent': round(sum(spent_of(c) for c in sel), 2),
           'page': page, 'pages': pages, 'size': body.size, 'items': [row_out(c, names, ctx) for c in sel[(page - 1) * body.size: page * body.size]]}
    if body.facets:
        from funnel import stages_for
        from wallet import products_map
        out['views'] = [{'id': k, 'name': n, 'count': sum(1 for c in rows if in_view(c, k, segs, ctx))} for k, n in VIEWS]
        out['views'] += [{'id': 'seg:' + s['id'], 'name': s['name'], 'count': sum(1 for c in rows if all(match(c, r, ctx) for r in s['rules'])), 'segment': True} for s in segs]
        tags = {}
        for c in rows:
            for x in c.get('tags') or []: tags[x] = tags.get(x, 0) + 1
        active = await db.users.find({'company_id': user['company_id'], 'active': True}, {'_id': 0, 'id': 1, 'name': 1}).sort('name', 1).to_list(500)
        prods = sorted((await products_map(user['company_id'])).values(), key=lambda p: norm(p.get('name')))
        out['facets'] = {'stages': [{'id': s['id'], 'name': s['name'], 'kind': s.get('kind')} for s in await stages_for(user['company'])],
                         'tags': [{'name': k, 'count': v} for k, v in sorted(tags.items(), key=lambda kv: (-kv[1], norm(kv[0])))][:200],
                         'owners': active, 'products': [{'id': p['id'], 'name': p['name']} for p in prods if p.get('active', True)][:200]}
    return out

class BulkIn(BaseModel):
    ids: List[str] = Field(default_factory=list, max_length=500)
    query: Optional[ListIn] = None   # no lugar de ids: todos os que a lista está mostrando
    action: Literal['tag', 'untag', 'stage', 'owner']
    value: str = Field(default='', max_length=60)

async def targets(user, ids, query):
    if query is not None:
        rows, segs, ctx = await base(user, query)
        sel = select(rows, segs, ctx, query)
    else:
        sel = await db.contacts.find({**visible_q(user), 'id': {'$in': ids}}, {'_id': 0}).to_list(len(ids) or 1) if ids else []
    if not sel: raise HTTPException(400, 'Nenhum contato selecionado.')
    if len(sel) > BULK_MAX: raise HTTPException(400, f'São {len(sel)} contatos. Filtre um pouco mais: o limite por vez é {BULK_MAX}.')
    return sel

@router.post('/contacts/bulk')
async def bulk(body: BulkIn, request: Request, user=Depends(current_user)):
    sel = await targets(user, body.ids, body.query)
    ids = [c['id'] for c in sel]; n = len(ids); v = body.value.strip()
    if body.action in ('tag', 'untag'):
        tag = v[:24]
        if not tag: raise HTTPException(400, 'Escreva a etiqueta.')
        await db.contacts.update_many({'id': {'$in': ids}}, {'$pull': {'tags': tag}} if body.action == 'untag' else {'$addToSet': {'tags': tag}})
        await audit(request, user, ('Tirou' if body.action == 'untag' else 'Colocou') + f' a etiqueta "{tag}" em vários contatos', f'{n} contatos')
        return {'ok': True, 'count': n}
    if body.action == 'stage':
        from funnel import move_stage, stages_for
        stages = await stages_for(user['company'])
        if v not in {s['id'] for s in stages}: raise HTTPException(400, 'Etapa não existe.')
        full = await db.contacts.find({'id': {'$in': ids}}, {'_id': 0}).to_list(n)
        by = user.get('impersonated_by') or user['name']; moved = 0
        for c in full: moved += bool(await move_stage(user['company'], c, v, by, stages))
        await audit(request, user, 'Mudou a etapa de vários contatos', f'{moved} contatos para {next(s["name"] for s in stages if s["id"] == v)}')
        return {'ok': True, 'count': moved}
    if user['role'] == 'agent' and v != user['id']: raise HTTPException(403, 'Só dono e gestor passam contatos para outra pessoa.')
    who = 'ninguém'
    if v:
        u = await db.users.find_one({'id': v, 'company_id': user['company_id'], 'active': True}, {'_id': 0, 'name': 1})
        if not u: raise HTTPException(404, 'Pessoa não encontrada.')
        who = u['name']
    await db.contacts.update_many({'id': {'$in': ids}}, {'$set': {'assigned_to': v or None}})
    await audit(request, user, f'Passou vários contatos para {who}', f'{n} contatos')
    return {'ok': True, 'count': n}

class ExportIn(BaseModel):
    ids: List[str] = Field(default_factory=list, max_length=500)
    query: Optional[ListIn] = None

@router.post('/contacts/export')
async def export(body: ExportIn, request: Request, user=Depends(managers)):
    from funnel import stages_for
    sel = await targets(user, body.ids, body.query)
    stages = {s['id']: s['name'] for s in await stages_for(user['company'])}
    names = await names_of(user['company_id']); first = next(iter(stages), None)
    d = lambda v: aware(v).strftime('%d/%m/%Y') if v else ''
    buf = io.StringIO(); w = csv.writer(buf, delimiter=';')
    w.writerow(['Nome', 'WhatsApp', 'Instagram', 'E-mail', 'Etapa', 'Etiquetas', 'Última conversa', 'Compras', 'Total gasto', 'Próximo vencimento', 'Tem', 'Responsável', 'Não quer mensagens'])
    for c in order(sel, 'name', 'asc'):
        w.writerow([c.get('name') or '', '+' + c['wa_id'] if c.get('wa_id') else '', '@' + c['ig_username'] if c.get('ig_username') else '', c.get('email') or '',
                    stages.get(c.get('stage') or first, ''), ', '.join(c.get('tags') or []), d(c.get('last_at')), c.get('sales_count') or 0, f'{spent_of(c):.2f}'.replace('.', ','),
                    d(c.get('next_expiry')), ', '.join(x['name'] for x in c.get('holdings') or []), names.get(c.get('assigned_to'), ''), 'sim' if c.get('opt_out') else ''])
    await audit(request, user, 'Exportou contatos', f'{len(sel)} contatos')
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="contatos.csv"'})
