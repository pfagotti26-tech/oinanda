#!/usr/bin/env bash
# Relatório para decidir preços: quantos clientes por mês cada conta recebe de verdade, quanto a Nanda atendeu
# e quanto custou a IA. Só lê o banco, não muda nada. Uso: bash /opt/oinanda/deploy/relatorio-uso.sh
set -euo pipefail
cd "$(dirname "$0")"
docker compose exec -T api python - <<'PY'
import asyncio, statistics
from datetime import timedelta
from core import db, now
from adminapi import ai_cost

def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else 0

async def main():
    t = now(); m0 = t.strftime('%Y-%m'); m1 = (t.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    since = (t.replace(day=1) - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cos = {c['id']: c for c in await db.companies.find({}, {'_id': 0, 'id': 1, 'name': 1, 'plan': 1, 'test': 1, 'segment': 1, 'billing': 1}).to_list(5000)}
    # demanda real: clientes diferentes que escreveram em cada dia (1 cliente num dia = 1 "atendimento possível")
    dem = {}
    async for x in db.messages.aggregate([
        {'$match': {'dir': 'in', 'at': {'$gte': since}}},
        {'$project': {'company_id': 1, 'contact_id': 1, 'mes': {'$dateToString': {'format': '%Y-%m', 'date': '$at'}}, 'dia': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$at'}}}},
        {'$group': {'_id': {'c': '$company_id', 'm': '$mes', 'k': '$contact_id', 'd': '$dia'}}},
        {'$group': {'_id': {'c': '$_id.c', 'm': '$_id.m'}, 'n': {'$sum': 1}}}]):
        dem[(x['_id']['c'], x['_id']['m'])] = x['n']
    use = {(u['company_id'], u['month']): u for u in await db.ai_usage.find({'month': {'$in': [m0, m1]}}, {'_id': 0}).to_list(20000)}
    print(f'Meses: {m1} (fechado) e {m0} (até hoje, dia {t.day})\n')
    print(f'{"conta":28} {"plano":13} | {"clientes/mês":>12} {"Nanda":>6} {"IA R$":>7} {"R$/atend":>8} | mês atual: {"clientes":>8} {"Nanda":>6}')
    cpa, dm1, dm0 = [], [], []
    for cid, c in sorted(cos.items(), key=lambda kv: -dem.get((kv[0], m1), 0)):
        d1, d0 = dem.get((cid, m1), 0), dem.get((cid, m0), 0)
        u1, u0 = use.get((cid, m1), {}), use.get((cid, m0), {})
        if not (d1 or d0 or u1 or u0): continue
        cost = ai_cost(u1.get('input_tokens', 0), u1.get('output_tokens', 0), u1.get('cache_read_tokens', 0), u1.get('cache_write_tokens', 0)) or 0
        calls = sum(u1.get(k, 0) or 0 for k in ('replies', 'suggestions', 'tests', 'describe', 'rewrite', 'scores')) or 0
        at = u1.get('atend', 0) or 0
        per = (cost * (u1.get('replies', 0) or 0) / calls / at) if at and calls else None
        if per: cpa.append(per)
        tag = ' (teste)' if c.get('test') else ''
        if d1 and not c.get('test'): dm1.append(d1)
        if d0 and not c.get('test'): dm0.append(round(d0 * 30 / t.day))
        c0 = ai_cost(u0.get('input_tokens', 0), u0.get('output_tokens', 0), u0.get('cache_read_tokens', 0), u0.get('cache_write_tokens', 0)) or 0
        k0 = sum(u0.get(k, 0) or 0 for k in ('replies', 'suggestions', 'tests', 'describe', 'rewrite', 'scores')); a0 = u0.get('atend', 0) or 0
        if a0 and k0: cpa.append(c0 * (u0.get('replies', 0) or 0) / k0 / a0)
        print(f'{(c.get("name") or "?")[:26] + tag:28} {(c.get("plan") or "-")[:13]:13} | {d1:12} {at:6} {cost:7.2f} {(f"{per:.3f}" if per else "-"):>8} | {"":10} {d0:8} {u0.get("atend", 0) or 0:6}')
    print('\nResumo do mês fechado (sem contas de teste):')
    if dm1:
        print(f'  Clientes por mês que escreveram: mediana {statistics.median(dm1):.0f} · 75% das contas até {pct(dm1, .75)} · 90% até {pct(dm1, .9)} · maior {max(dm1)}')
        print(f'  Contas que passariam de 200 por mês: {sum(1 for x in dm1 if x > 200)} de {len(dm1)}')
    if dm0:
        print(f'  Mês atual projetado para 30 dias: mediana {statistics.median(dm0):.0f} · 75% até {pct(dm0, .75)} · 90% até {pct(dm0, .9)} · maior {max(dm0)}')
        print(f'  Contas que devem passar de 200 este mês: {sum(1 for x in dm0 if x > 200)} de {len(dm0)}')
    if cpa: print(f'  Custo de IA por atendimento da Nanda: média R$ {statistics.mean(cpa):.3f} · maior R$ {max(cpa):.3f}')
    print('\nNada foi alterado. Pode mandar o print deste resultado.')
asyncio.run(main())
PY
