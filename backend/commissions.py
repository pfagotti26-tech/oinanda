"""Comissão: regras num lugar só, quanto cada um tem a receber, extrato e acerto.

Regras (a primeira que existir vale):
1. exceção da pessoa no item  (ex.: Bianca 15% no Plano mensal)
2. regra do item              (percentual ou valor fixo por unidade)
3. padrão da pessoa           (users.commission_pct, o que já existia)

Base: o que o cliente pagou pelo item (depois do desconto e das promoções); em venda dividida, só a parte da pessoa.
Plano que renova: cada cobrança é uma venda nova do mesmo item para o mesmo cliente. A regra diz em quais paga:
'first' só a primeira, 'n' as N primeiras (a primeira conta), 'all' todas.
Só conta venda confirmada (status ok). Cancelada sai sozinha.
"""
from typing import Dict, List, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
import agenda
from sales import Filters, period, aware, MGR

router = APIRouter()

class Exc(BaseModel):
    user_id: str
    pct: float = Field(ge=0, le=100)

class Rule(BaseModel):
    mode: Literal['pct', 'fixed'] = 'pct'
    value: float = Field(ge=0, le=1_000_000)
    renew: Literal['first', 'n', 'all'] = 'all'
    n: int = Field(default=3, ge=1, le=120)
    exceptions: List[Exc] = Field(default_factory=list, max_length=50)

def _rule_label(r, who=None):
    v = (f"R$ {r['value']:.2f}".replace('.', ',') + ' fixo') if r['mode'] == 'fixed' else (f"{r['value']:g}%".replace('.', ','))
    return v

async def _calc(cid, s0, s1, t0, t1, only_user=None):
    users = {u['id']: u for u in await db.users.find({'company_id': cid}, {'_id': 0, 'id': 1, 'name': 1, 'commission_pct': 1, 'active': 1, 'role': 1}).to_list(500)}
    prods = {p['id']: p for p in await db.products.find({'company_id': cid}, {'_id': 0, 'id': 1, 'name': 1, 'kind': 1, 'commission': 1}).to_list(None)}
    sales = await db.sales.find({'company_id': cid, 'status': 'ok', 'date': {'$gte': t0, '$lt': t1}}, {'_id': 0}).sort('date', -1).to_list(50000)
    # posição de cada cobrança do plano: quantas vezes este cliente já comprou este item (contando esta)
    ruled = {pid for pid, p in prods.items() if (p.get('commission') or {}).get('renew') in ('first', 'n')}
    seq = {}
    if ruled:
        cids = list({s['contact_id'] for s in sales if any(i.get('product_id') in ruled for i in s.get('items') or [])})
        for i in range(0, len(cids), 1000):
            hist = await db.sales.find({'company_id': cid, 'status': 'ok', 'contact_id': {'$in': cids[i:i + 1000]}, 'items.product_id': {'$in': list(ruled)}}, {'_id': 0, 'id': 1, 'contact_id': 1, 'date': 1, 'items.product_id': 1}).sort('date', 1).to_list(None)
            cnt = {}
            for h in hist:
                for it in h.get('items') or []:
                    if it.get('product_id') in ruled:
                        k = (h['contact_id'], it['product_id']); cnt[k] = cnt.get(k, 0) + 1; seq[(h['id'], it['product_id'])] = cnt[k]
    lines, nobody = [], 0.0
    for s in sales:
        sellers = [x for x in s.get('sellers') or [] if x.get('user_id') in users]
        items = s.get('items') or []
        gross = sum((i.get('total') if i.get('total') is not None else i.get('qty', 1) * i.get('price', 0) - (i.get('discount') or 0)) for i in items)
        factor = (s.get('total') or 0) / gross if gross else 0
        if not sellers: nobody += s.get('total') or 0; continue
        for x in sellers:
            if only_user and x['user_id'] != only_user: continue
            u = users[x['user_id']]; share = (x.get('pct') or 0) / 100
            for it in items:
                paid = (it.get('total') if it.get('total') is not None else it.get('qty', 1) * it.get('price', 0) - (it.get('discount') or 0)) * factor
                base = round(paid * share, 2)
                p = prods.get(it.get('product_id')) or {}
                r = p.get('commission')
                upct = u.get('commission_pct')
                if r:
                    ex = next((e for e in r.get('exceptions') or [] if e['user_id'] == u['id']), None)
                    k = seq.get((s['id'], it.get('product_id')), 1)
                    limit = 1 if r.get('renew') == 'first' else (r.get('n') or 1) if r.get('renew') == 'n' else None
                    plano = p.get('kind') == 'plano'
                    tag = (f' · {k}ª cobrança' if plano and k > 1 else '')
                    if limit is not None and k > limit:
                        val, label, src = 0.0, 'plano · ' + ('só paga a primeira' if limit == 1 else f'já passou das {limit} primeiras'), 'item'
                    elif ex:
                        val, label, src = base * ex['pct'] / 100, f"exceção de {u['name'].split()[0]} · {ex['pct']:g}%".replace('.', ','), 'exc'
                    elif r['mode'] == 'fixed':
                        val, label, src = r['value'] * (it.get('qty') or 1) * share, 'regra do item · ' + _rule_label(r), 'item'
                    else:
                        val, label, src = base * r['value'] / 100, 'regra do item · ' + _rule_label(r), 'item'
                    if limit and limit > 1 and k <= limit: label += f' ({k} de {limit})'
                    iname = it.get('name', '') + tag
                else:
                    val = base * (upct or 0) / 100
                    label = f"padrão de {u['name'].split()[0]} · {upct:g}%".replace('.', ',') if upct else 'sem comissão'
                    src, iname = 'user', it.get('name', '')
                lines.append({'sale_id': s['id'], 'user_id': u['id'], 'date': aware(s['date']).isoformat(), 'client': s.get('contact_name') or '', 'item': iname,
                              'split': round(share * 100) if share < 1 else None, 'discount': round((it.get('discount') or 0), 2), 'base': base, 'rule': label, 'src': src, 'commission': round(val, 2)})
    return users, lines, round(nobody, 2)

async def _paid(cid, s0, s1, uid_=None):
    q = {'company_id': cid, 'start': s0.isoformat(), 'end': s1.isoformat()}
    if uid_: q['user_id'] = uid_
    return await db.commission_payouts.find(q, {'_id': 0}).sort('at', -1).to_list(500)

@router.get('/commission/summary')
async def summary(start: Optional[str] = None, end: Optional[str] = None, user=Depends(current_user)):
    s0, s1, t0, t1 = period(user['company'], Filters(start=start, end=end))
    me = None if user['role'] in MGR else user['id']
    users, lines, nobody = await _calc(user['company_id'], s0, s1, t0, t1, me)
    pays = await _paid(user['company_id'], s0, s1, me)
    rows = {}
    for l in lines:
        r = rows.setdefault(l['user_id'], {'user_id': l['user_id'], 'name': users[l['user_id']]['name'], 'pct': users[l['user_id']].get('commission_pct'), 'sales': set(), 'sold': 0.0, 'commission': 0.0, 'paid': 0.0})
        r['sales'].add(l['sale_id']); r['sold'] += l['base']; r['commission'] += l['commission']
    for p in pays:
        if p['user_id'] in rows: rows[p['user_id']]['paid'] += p['amount']
        elif p['user_id'] in users: rows[p['user_id']] = {'user_id': p['user_id'], 'name': users[p['user_id']]['name'], 'pct': users[p['user_id']].get('commission_pct'), 'sales': set(), 'sold': 0.0, 'commission': 0.0, 'paid': p['amount']}
    out = []
    for r in rows.values():
        r['sales'] = len(r['sales']); r['sold'] = round(r['sold'], 2); r['commission'] = round(r['commission'], 2); r['paid'] = round(r['paid'], 2)
        r['due'] = round(max(0.0, r['commission'] - r['paid']), 2)
        r['last_paid'] = next((p['at'] for p in pays if p['user_id'] == r['user_id']), None)
        out.append(r)
    out.sort(key=lambda r: (-r['due'], -r['commission'], -r['sold']))
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'rows': out, 'due': round(sum(r['due'] for r in out), 2), 'paid': round(sum(r['paid'] for r in out), 2),
            'sold': round(sum(r['sold'] for r in out), 2), 'commission': round(sum(r['commission'] for r in out), 2), 'without_seller': nobody if not me else None,
            'payouts': [{**p, 'at': aware(p['at']).isoformat()} for p in pays]}

@router.get('/commission/statement')
async def statement(user_id: str, start: Optional[str] = None, end: Optional[str] = None, fmt: str = '', user=Depends(current_user)):
    if user['role'] not in MGR: user_id = user['id']
    s0, s1, t0, t1 = period(user['company'], Filters(start=start, end=end))
    users, lines, _ = await _calc(user['company_id'], s0, s1, t0, t1, user_id)
    if user_id not in users: raise HTTPException(404, 'Pessoa não encontrada.')
    pays = await _paid(user['company_id'], s0, s1, user_id)
    tot = round(sum(l['commission'] for l in lines), 2); paid = round(sum(p['amount'] for p in pays), 2)
    if fmt == 'csv':
        import csv, io
        from fastapi.responses import Response
        tz = agenda.tz_of(user['company'])
        from datetime import datetime
        buf = io.StringIO(); w = csv.writer(buf, delimiter=';'); br = lambda v: f'{v:.2f}'.replace('.', ',')
        w.writerow(['Data', 'Cliente', 'Item', 'Base', 'Regra', 'Comissão'])
        for l in lines: w.writerow([datetime.fromisoformat(l['date']).astimezone(tz).strftime('%d/%m/%Y'), l['client'], l['item'], br(l['base']), l['rule'], br(l['commission'])])
        w.writerow([]); w.writerow(['', '', '', '', 'Total', br(tot)]); w.writerow(['', '', '', '', 'Já pago', br(paid)]); w.writerow(['', '', '', '', 'A receber', br(max(0, tot - paid))])
        name = users[user_id]['name'].split()[0].lower()
        return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="comissao-{name}-{s0}-a-{s1}.csv"'})
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'user': {'id': user_id, 'name': users[user_id]['name'], 'pct': users[user_id].get('commission_pct')},
            'lines': lines, 'commission': tot, 'paid': paid, 'due': round(max(0.0, tot - paid), 2), 'payouts': [{**p, 'at': aware(p['at']).isoformat()} for p in pays]}

class PayIn(BaseModel):
    user_id: str
    start: str
    end: str
    amount: float = Field(gt=0, le=10_000_000)
    note: str = Field(default='', max_length=200)

@router.post('/commission/pay')
async def pay(body: PayIn, request: Request, user=Depends(managers)):
    u = await db.users.find_one({'id': body.user_id, 'company_id': user['company_id']}, {'_id': 0, 'name': 1})
    if not u: raise HTTPException(404, 'Pessoa não encontrada.')
    s0, s1, _, _ = period(user['company'], Filters(start=body.start, end=body.end))
    p = {'id': uid(), 'company_id': user['company_id'], 'user_id': body.user_id, 'start': s0.isoformat(), 'end': s1.isoformat(), 'amount': round(body.amount, 2), 'note': body.note, 'at': now(), 'by': user['name']}
    await db.commission_payouts.insert_one(p.copy())
    await audit(request, user, 'Marcou comissão como paga', f"{u['name']}: R$ {p['amount']:.2f} ({s0:%d/%m} a {s1:%d/%m})")
    return {'ok': True, 'id': p['id']}

@router.delete('/commission/pay/{pid}')
async def unpay(pid: str, request: Request, user=Depends(managers)):
    p = await db.commission_payouts.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0})
    if not p: raise HTTPException(404, 'Pagamento não encontrado.')
    await db.commission_payouts.delete_one({'id': pid})
    await audit(request, user, 'Desfez pagamento de comissão', f"R$ {p['amount']:.2f}")
    return {'ok': True}

@router.get('/commission/rules')
async def rules(user=Depends(managers)):
    us = await db.users.find({'company_id': user['company_id'], 'active': {'$ne': False}}, {'_id': 0, 'id': 1, 'name': 1, 'role': 1, 'commission_pct': 1}).sort('name', 1).to_list(500)
    ps = await db.products.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1, 'name': 1, 'code': 1, 'price': 1, 'kind': 1, 'active': 1, 'duration_days': 1, 'commission': 1}).sort('name', 1).to_list(None)
    return {'people': us, 'items': ps}

class PeopleIn(BaseModel):
    pct: Dict[str, Optional[float]]

@router.put('/commission/people')
async def set_people(body: PeopleIn, request: Request, user=Depends(managers)):
    for k, v in body.pct.items():
        if v is not None and not (0 <= v <= 100): raise HTTPException(400, 'O percentual vai de 0 a 100.')
        await db.users.update_one({'id': k, 'company_id': user['company_id']}, {'$set': {'commission_pct': round(v, 2) if v is not None else None}})
    await audit(request, user, 'Mudou a comissão padrão da equipe', ', '.join(f'{v}%' for v in body.pct.values() if v is not None))
    return {'ok': True}

@router.put('/commission/item/{pid}')
async def set_item(pid: str, body: Optional[Rule] = None, request: Request = None, user=Depends(managers)):
    p = await db.products.find_one({'id': pid, 'company_id': user['company_id']}, {'_id': 0, 'name': 1})
    if not p: raise HTTPException(404, 'Item não encontrado.')
    await db.products.update_one({'id': pid}, {'$set': {'commission': body.model_dump() if body else None}})
    await audit(request, user, 'Mudou a comissão do item' if body else 'Tirou a regra de comissão do item', p['name'])
    return {'ok': True}
