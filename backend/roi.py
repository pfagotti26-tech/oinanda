"""Retorno por anúncio e por canal: quanto foi investido, quantos contatos e clientes vieram e quanto eles compraram.

O investimento é lançado à mão (ou importado em CSV, como o relatório do Gerenciador de Anúncios da Meta), por canal
(origem) e, se quiser, por anúncio, link rastreável ou campanha. Um investimento que pega só parte do período entra
proporcional aos dias. Os contatos contam pelo dia em que chegaram; o faturamento é tudo o que esses contatos compraram
até hoje (assim uma campanha de outubro recebe o crédito da venda fechada em novembro).
"""
import csv, io, re, unicodedata
from datetime import date, datetime, timedelta
from typing import Optional, Literal
from fastapi import APIRouter, Request, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field, field_validator
from core import db, now, uid, audit, managers

router = APIRouter()

def norm(s): return re.sub(r'\s+', ' ', unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode().strip().lower())

class SpendIn(BaseModel):
    origin: str = Field(min_length=1, max_length=24)
    ref_kind: Optional[Literal['ad', 'detail']] = None  # ad = anúncio da Meta (id); detail = link, campanha, evento...
    ref: Optional[str] = Field(default=None, max_length=80)
    label: str = Field(default='', max_length=80)
    start: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    amount: float = Field(gt=0, le=10_000_000)
    notes: str = Field(default='', max_length=200)

    @field_validator('end')
    @classmethod
    def ordem(cls, v, info):
        if info.data.get('start') and v < info.data['start']: raise ValueError('A data final é antes da inicial.')
        return v

def child_key(c):
    if c.get('ad') and c['ad'].get('source_id') and (c.get('origin') in (None, 'anuncio')): return 'ad:' + str(c['ad']['source_id'])
    d = norm(c.get('origin_detail'))
    return 'detail:' + d if d else None

def spend_key(s):
    if s.get('ref_kind') == 'ad' and s.get('ref'): return 'ad:' + str(s['ref'])
    if s.get('ref_kind') == 'detail' and s.get('ref'): return 'detail:' + norm(s['ref'])
    return None

def overlap(s, d0, d1):
    """Parte do investimento que cai dentro de [d0, d1] (datas, inclusive), proporcional aos dias."""
    a, b = date.fromisoformat(s['start']), date.fromisoformat(s['end'])
    lo, hi = max(a, d0), min(b, d1)
    if hi < lo: return 0.0
    return s['amount'] * ((hi - lo).days + 1) / ((b - a).days + 1)

@router.get('/spend')
async def list_spend(user=Depends(managers)):
    return await db.spend.find({'company_id': user['company_id']}, {'_id': 0}).sort('start', -1).to_list(2000)

@router.get('/spend/options')
async def options(user=Depends(managers)):
    from origins import origins_of
    ads = {}
    for c in await db.contacts.find({'company_id': user['company_id'], 'ad.source_id': {'$exists': True}}, {'_id': 0, 'ad': 1}).sort('created_at', -1).to_list(5000):
        a = c['ad']; ads.setdefault(str(a['source_id']), a.get('headline') or str(a['source_id']))
    links = [{'name': l['label'], 'origin': l['origin'], 'detail': l.get('detail') or l['label']} for l in await db.track_links.find({'company_id': user['company_id']}, {'_id': 0}).to_list(500)]
    details = {}
    for c in await db.contacts.find({'company_id': user['company_id'], 'origin_detail': {'$nin': [None, '']}}, {'_id': 0, 'origin': 1, 'origin_detail': 1}).to_list(20000):
        details.setdefault(c.get('origin') or '_none', set()).add(c['origin_detail'].strip())
    return {'origins': await origins_of(user['company']), 'ads': [{'id': k, 'name': v} for k, v in ads.items()], 'links': links,
            'details': {k: sorted(v)[:100] for k, v in details.items()}}

async def save(user, body, sid=None):
    from origins import origins_of
    if body.origin not in {o['id'] for o in await origins_of(user['company'])}: raise HTTPException(400, 'Origem não existe.')
    d = body.model_dump()
    d['ref'] = (d.get('ref') or '').strip() or None
    if not d['ref']: d['ref_kind'] = None
    d['label'] = d['label'].strip() or (d['ref'] or '')
    return d

@router.post('/spend')
async def create(body: SpendIn, request: Request, user=Depends(managers)):
    from billing import require; require(user, 'retorno')
    d = {'id': uid(), 'company_id': user['company_id'], **await save(user, body), 'by': user['name'], 'created_at': now()}
    await db.spend.insert_one(d.copy())
    await audit(request, user, 'Lançou investimento', f'{d["label"] or d["origin"]} · R$ {d["amount"]:.2f}')
    d.pop('_id', None)
    return d

@router.put('/spend/{sid}')
async def update(sid: str, body: SpendIn, user=Depends(managers)):
    if not await db.spend.find_one({'id': sid, 'company_id': user['company_id']}): raise HTTPException(404, 'Investimento não encontrado.')
    d = await save(user, body)
    await db.spend.update_one({'id': sid}, {'$set': d})
    return {'ok': True}

@router.delete('/spend/{sid}')
async def delete(sid: str, request: Request, user=Depends(managers)):
    s = await db.spend.find_one({'id': sid, 'company_id': user['company_id']}, {'_id': 0})
    if not s: raise HTTPException(404, 'Investimento não encontrado.')
    await db.spend.delete_one({'id': sid})
    await audit(request, user, 'Apagou investimento', f'{s.get("label") or s["origin"]} · R$ {s["amount"]:.2f}')
    return {'ok': True}

# ---------- importar CSV (ex.: relatório do Gerenciador de Anúncios) ----------
COLS = {
    'amount': ['valor usado', 'valor gasto', 'amount spent', 'gasto', 'investimento', 'valor', 'custo', 'spend', 'cost'],
    'start': ['inicio dos relatorios', 'reporting starts', 'data de inicio', 'inicio', 'start', 'data', 'dia', 'date', 'day'],
    'end': ['termino dos relatorios', 'reporting ends', 'data de termino', 'termino', 'fim', 'end'],
    'ad_id': ['id do anuncio', 'ad id', 'ad_id'],
    'name': ['nome do anuncio', 'ad name', 'nome da campanha', 'campaign name', 'campanha', 'campaign', 'anuncio', 'nome'],
}
def find_col(headers, key):
    hs = [norm(h) for h in headers]
    for want in COLS[key]:
        for i, h in enumerate(hs):
            if h == want or h.startswith(want + ' ') or h.startswith(want + '('): return i
    return None

def num(v):
    v = re.sub(r'[^\d,.\-]', '', str(v or ''))
    if not v: return None
    if ',' in v and '.' in v: v = v.replace('.', '').replace(',', '.') if v.rfind(',') > v.rfind('.') else v.replace(',', '')
    elif ',' in v: v = v.replace(',', '.')
    try: return float(v)
    except ValueError: return None

def day(v):
    v = str(v or '').strip()[:10]
    for f in ('%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y', '%m/%d/%Y'):
        try: return datetime.strptime(v, f).date().isoformat()
        except ValueError: pass
    return None

@router.post('/spend/import')
async def import_csv(request: Request, file: UploadFile = File(...), origin: str = Form(default='anuncio'), start: str = Form(default=''), end: str = Form(default=''),
                     user=Depends(managers)):
    from billing import require; require(user, 'retorno')
    from origins import origins_of
    if origin not in {o['id'] for o in await origins_of(user['company'])}: raise HTTPException(400, 'Origem não existe.')
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024: raise HTTPException(400, 'Arquivo grande demais (máximo 5 MB).')
    text = None
    for enc in ('utf-8-sig', 'latin-1'):
        try: text = raw.decode(enc); break
        except UnicodeDecodeError: pass
    try: dialect = csv.Sniffer().sniff(text[:4000], delimiters=';,\t')
    except csv.Error: dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if len(rows) < 2: raise HTTPException(400, 'O arquivo está vazio.')
    h = rows[0]
    ia, i0, i1, iid, inm = (find_col(h, k) for k in ('amount', 'start', 'end', 'ad_id', 'name'))
    if ia is None: raise HTTPException(400, 'Não achei a coluna do valor. Use uma coluna chamada "Valor usado", "Gasto" ou "Investimento".')
    d0, d1 = day(start), day(end)
    made = skipped = 0
    for r in rows[1:]:
        g = lambda i: r[i] if i is not None and i < len(r) else ''
        amount = num(g(ia))
        if not amount or amount <= 0: skipped += 1; continue
        s0 = day(g(i0)) or d0; s1 = day(g(i1)) or (day(g(i0)) if i0 is not None else None) or d1
        if not s0 or not s1: raise HTTPException(400, 'Sem data no arquivo: informe o período do investimento.')
        if s1 < s0: s0, s1 = s1, s0
        ad_id = re.sub(r'\D', '', g(iid)) or None; name = g(inm).strip()[:80]
        doc = {'origin': origin, 'ref_kind': 'ad' if ad_id else ('detail' if name else None), 'ref': ad_id or name or None, 'label': name or ad_id or '',
               'start': s0, 'end': s1, 'amount': round(amount, 2), 'notes': 'importado de ' + (file.filename or 'CSV')[:60]}
        if await db.spend.find_one({'company_id': user['company_id'], **{k: doc[k] for k in ('origin', 'ref', 'start', 'end', 'amount')}}): skipped += 1; continue
        await db.spend.insert_one({'id': uid(), 'company_id': user['company_id'], **doc, 'by': user['name'], 'created_at': now()})
        made += 1
    await audit(request, user, 'Importou investimentos', f'{made} linhas')
    return {'created': made, 'skipped': skipped}

# ---------- relatório ----------
def kpis(r):
    sp, ld, cl, rv = r['spend'], r['leads'], r['clients'], r['revenue']
    r.update(spend=round(sp, 2), revenue=round(rv, 2), cpl=round(sp / ld, 2) if sp and ld else None, cac=round(sp / cl, 2) if sp and cl else None,
             roas=round(rv / sp, 2) if sp else None, result=round(rv - sp, 2) if sp else None, conversion=round(100 * cl / ld, 1) if ld else None)
    return r

@router.get('/reports/roi')
async def report(start: Optional[str] = None, end: Optional[str] = None, user=Depends(managers)):
    from sales import period, Filters
    from origins import origins_of
    company = user['company']
    s0, s1, t0, t1 = period(company, Filters(start=start, end=end))
    names = {o['id']: o['name'] for o in await origins_of(company)}; names['_none'] = 'Não sabemos'
    cs = await db.contacts.find({'company_id': company['id'], 'created_at': {'$gte': t0, '$lt': t1}, 'source': {'$nin': ['historico', 'agenda do celular']}},
                                {'_id': 0, 'id': 1, 'origin': 1, 'origin_detail': 1, 'ad': 1}).to_list(50000)
    ids = [c['id'] for c in cs]
    rev, first = {}, {}
    for s in await db.sales.find({'company_id': company['id'], 'contact_id': {'$in': ids}, 'status': 'ok'}, {'_id': 0, 'contact_id': 1, 'total': 1}).to_list(100000):
        rev[s['contact_id']] = rev.get(s['contact_id'], 0) + s['total']
    blank = lambda k, n: {'key': k, 'name': n, 'spend': 0.0, 'leads': 0, 'clients': 0, 'revenue': 0.0}
    rows = {}
    for c in cs:
        o = c.get('origin') or ('anuncio' if c.get('ad') else '_none')
        r = rows.setdefault(o, {**blank(o, names.get(o, o)), 'children': {}})
        k = child_key(c)
        ch = r['children'].setdefault(k or '_', blank(k or '_', ((c.get('ad') or {}).get('headline') or (c.get('ad') or {}).get('source_id')) if k and k.startswith('ad:')
                                                            else (c.get('origin_detail') or '').strip() if k else 'Sem detalhe'))
        for x in (r, ch):
            x['leads'] += 1
            if c['id'] in rev: x['clients'] += 1; x['revenue'] += rev[c['id']]
    spend_total = 0.0
    for s in await db.spend.find({'company_id': company['id'], 'start': {'$lte': s1.isoformat()}, 'end': {'$gte': s0.isoformat()}}, {'_id': 0}).to_list(5000):
        v = overlap(s, s0, s1)
        if not v: continue
        spend_total += v
        r = rows.setdefault(s['origin'], {**blank(s['origin'], names.get(s['origin'], s['origin'])), 'children': {}})
        r['spend'] += v
        k = spend_key(s) or '_geral'
        ch = r['children'].setdefault(k, blank(k, s.get('label') or s.get('ref') or 'Investimento geral do canal' if k != '_geral' else 'Investimento geral do canal'))
        ch['spend'] += v
    out = []
    for r in rows.values():
        ch = [kpis(x) for x in r.pop('children').values()]
        ch.sort(key=lambda x: (x['key'] in ('_', '_geral'), -x['spend'], -x['revenue'], -x['leads']))
        r = kpis(r); r['children'] = ch if (len(ch) > 1 or (ch and ch[0]['key'] not in ('_', '_geral'))) else []
        out.append(r)
    out.sort(key=lambda r: (r['key'] == '_none', -r['spend'], -r['revenue'], -r['leads']))
    tot = kpis({'spend': spend_total, 'leads': len(cs), 'clients': sum(1 for c in cs if c['id'] in rev), 'revenue': sum(rev.get(c['id'], 0) for c in cs)})
    paid = [r for r in out if r['spend']]
    tot_paid = kpis({'spend': sum(r['spend'] for r in paid), 'leads': sum(r['leads'] for r in paid), 'clients': sum(r['clients'] for r in paid), 'revenue': sum(r['revenue'] for r in paid)})
    return {'start': s0.isoformat(), 'end': s1.isoformat(), 'rows': out, 'total': tot, 'paid': tot_paid, 'has_spend': bool(spend_total)}
