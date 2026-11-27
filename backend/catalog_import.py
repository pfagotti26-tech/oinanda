"""Importar o catálogo (produtos, serviços, planos e pacotes) de uma planilha.

1. GET  /catalog/import/template.xlsx  -> modelo pronto para preencher.
2. POST /catalog/import/preview        -> lê o arquivo e mostra o que vai entrar: novo, atualiza ou com problema (nada é gravado).
3. POST /catalog/import                -> grava. Junta com o que já existe pelo código; sem código, pelo nome.
4. POST /catalog/import/{id}/undo      -> desfaz: apaga os itens criados (os já vendidos só ficam inativos) e volta os atualizados.

A leitura do arquivo é a mesma da importação de clientes (Excel .xlsx ou CSV).
"""
import io, json
from typing import Dict, List, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from core import db, now, uid, audit, managers
from importer import read_file, to_money, _n

router = APIRouter()
LIMIT = 1000  # mesmo limite do cadastro manual

FIELDS = {  # chave -> (rótulo, palavras do cabeçalho)
    'name': ('Nome', ['nome', 'produto', 'servico', 'item', 'descricao', 'nome do produto', 'nome do servico', 'plano', 'pacote', 'name']),
    'code': ('Código', ['codigo', 'cod', 'sku', 'referencia', 'ref', 'code', 'id']),
    'category': ('Categoria', ['categoria', 'grupo', 'linha', 'departamento', 'secao', 'category']),
    'kind': ('Tipo', ['tipo', 'kind', 'natureza']),
    'price': ('Preço', ['preco', 'valor', 'preco de venda', 'valor de venda', 'price', 'preco unitario']),
    'duration': ('Validade (dias)', ['validade', 'duracao', 'dias', 'vigencia', 'validade em dias', 'prazo']),
    'active': ('Ativo', ['ativo', 'status', 'situacao', 'disponivel', 'active']),
}
KIND_WORDS = {'servico': 'servico', 'serviço': 'servico', 'produto': 'produto', 'plano': 'plano', 'pacote': 'plano', 'assinatura': 'plano', 'mensalidade': 'plano'}

def guess(headers):
    out, used = {}, set()
    for exact in (True, False):
        for i, h in enumerate(headers):
            if str(i) in out: continue
            hn = _n(h)
            for k, (_, words) in FIELDS.items():
                if k in used: continue
                if (exact and hn in words) or (not exact and any(w in hn for w in words if len(w) > 3)):
                    out[str(i)] = k; used.add(k); break
    return out

def _kind(v, dur):
    t = _n(v)
    for w, k in KIND_WORDS.items():
        if _n(w) in t: return k
    return 'plano' if dur else 'servico'

def _active(v):
    t = _n(v)
    if not t: return True
    return not (t.startswith('nao') or t.startswith('n ') or t in ('n', '0', 'false', 'falso', 'off') or 'inativ' in t or 'desativ' in t)

def _dur(v):
    if v in ('', None): return None
    try: d = int(float(str(v).replace(',', '.').split()[0]))
    except (ValueError, IndexError): return 'bad'
    return d if 1 <= d <= 3650 else 'bad'

def parse(rows, mapping):
    col = {k: int(i) for i, k in mapping.items() if k in FIELDS}
    if 'name' not in col: raise HTTPException(400, 'Diga qual coluna tem o nome do item.')
    get = lambda r, k: (r[col[k]] if k in col and col[k] < len(r) else '')
    items, seen = [], set()
    for n, r in enumerate(rows, start=2):
        name = str(get(r, 'name') or '').strip()[:80]
        if not name: continue
        it = {'line': n, 'name': name, 'code': str(get(r, 'code') or '').strip()[:30], 'category': str(get(r, 'category') or '').strip()[:40], 'problems': []}
        if it['code'].endswith('.0') and it['code'][:-2].isdigit(): it['code'] = it['code'][:-2]  # Excel guarda 123 como 123.0
        pr = to_money(get(r, 'price'))
        if pr == 'bad': it['problems'].append('preço ilegível (fica "varia por cliente")'); pr = None
        if isinstance(pr, float) and pr < 0: it['problems'].append('preço negativo (fica "varia por cliente")'); pr = None
        it['price'] = pr
        d = _dur(get(r, 'duration'))
        if d == 'bad': it['problems'].append('validade ilegível (ignorada)'); d = None
        it['kind'] = _kind(get(r, 'kind'), d)
        it['duration_days'] = d if it['kind'] == 'plano' else None
        if it['kind'] == 'plano' and not it['duration_days']: it['duration_days'] = 30; it['problems'].append('plano sem validade: ficou 30 dias')
        it['active'] = _active(get(r, 'active')) if 'active' in col else True
        if len(name) < 2: it['error'] = 'nome muito curto'
        key = it['code'].lower() or _n(name)
        if key in seen: it['error'] = 'repetido na planilha (fica a primeira linha)'
        seen.add(key)
        items.append(it)
    return items

async def _match(cid, items):
    ex = await db.products.find({'company_id': cid}, {'_id': 0}).to_list(None)
    by_code = {(p.get('code') or '').lower(): p for p in ex if p.get('code')}
    by_name = {_n(p['name']): p for p in ex}
    for it in items:
        p = (by_code.get(it['code'].lower()) if it['code'] else None) or by_name.get(_n(it['name']))
        it['match'] = p['id'] if p else None
        if p and p.get('service_id'): it['error'] = it.get('error') or 'serviço da Agenda: mude o preço por lá'
    return ex

def _summary(items, total_now, mode):
    new = [i for i in items if not i.get('error') and not i['match']]
    upd = [i for i in items if not i.get('error') and i['match']]
    bad = [i for i in items if i.get('error')]
    over = max(0, total_now + len(new) - LIMIT)
    return {'new': len(new), 'update': len(upd) if mode == 'update' else 0, 'skip': len(upd) if mode == 'skip' else 0, 'errors': len(bad), 'over': over}

@router.get('/catalog/import/template.xlsx')
async def template(user=Depends(managers)):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = 'Catálogo'
    ws.append(['Nome', 'Código', 'Categoria', 'Tipo', 'Preço', 'Validade (dias)', 'Ativo'])
    ws.append(['Limpeza de pele', 'SV-01', 'Facial', 'Serviço', '180,00', '', 'Sim'])
    ws.append(['Protetor solar FPS 50', 'PR-10', 'Produtos', 'Produto', '89,90', '', 'Sim'])
    ws.append(['Pacote 10 sessões de drenagem', 'PC-03', 'Corporal', 'Pacote', '1.200,00', '90', 'Sim'])
    ws.append(['Plano mensal', 'PL-01', 'Planos', 'Plano', '280', '30', 'Sim'])
    ws.append(['Avaliação', '', '', 'Serviço', '', '', 'Sim'])
    for c in ws[1]: c.font = Font(bold=True, color='FFFFFF'); c.fill = PatternFill('solid', fgColor='E8603C')
    for col, w in zip('ABCDEFG', (32, 12, 16, 12, 12, 16, 8)): ws.column_dimensions[col].width = w
    h = wb.create_sheet('Como preencher')
    for line in (['Coluna', 'Como preencher'], ['Nome', 'Obrigatório. Do jeito que o cliente conhece.'], ['Código', 'Opcional. Se tiver, é por ele que o item é encontrado de novo ao reimportar.'],
                 ['Categoria', 'Opcional. Agrupa nos relatórios e no filtro da venda.'], ['Tipo', 'Serviço, Produto, Plano ou Pacote. Em branco: Serviço (ou Plano, se tiver validade).'],
                 ['Preço', 'Opcional. Em branco = varia por cliente (preenche na hora da venda).'], ['Validade (dias)', 'Só para plano ou pacote: em quantos dias vence. Ex.: 30, 90, 365.'],
                 ['Ativo', 'Sim ou Não. Em branco: Sim.']):
        h.append(line)
    h['A1'].font = h['B1'].font = Font(bold=True); h.column_dimensions['A'].width = 18; h.column_dimensions['B'].width = 80
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             headers={'Content-Disposition': 'attachment; filename="modelo-catalogo-oi-nanda.xlsx"'})

@router.post('/catalog/import/preview')
async def preview(file: UploadFile = File(...), mapping: str = Form(''), mode: str = Form('update'), user=Depends(managers)):
    head, rows = read_file(file.filename, await file.read())
    if len(rows) > LIMIT * 2: raise HTTPException(400, f'A planilha tem mais de {LIMIT * 2} linhas. O catálogo aceita até {LIMIT} itens.')
    mp = json.loads(mapping) if mapping else guess(head)
    items = parse(rows, mp) if 'name' in mp.values() else []
    ex = await _match(user['company_id'], items) if items else await db.products.find({'company_id': user['company_id']}, {'_id': 0, 'id': 1}).to_list(None)
    return {'file': (file.filename or 'planilha')[:120], 'headers': head, 'mapping': mp, 'fields': {k: v[0] for k, v in FIELDS.items()},
            'samples': [[('' if v is None else str(v))[:40] for v in r] for r in rows[:3]],
            'items': items, 'summary': _summary(items, len(ex), mode), 'total_now': len(ex)}

class Item(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    code: str = Field(default='', max_length=30)
    category: str = Field(default='', max_length=40)
    kind: Literal['produto', 'servico', 'plano'] = 'servico'
    price: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    duration_days: Optional[int] = Field(default=None, ge=1, le=3650)
    active: bool = True

class RunIn(BaseModel):
    items: List[Item] = Field(min_length=1, max_length=LIMIT * 2)
    mode: Literal['update', 'skip'] = 'update'
    file: str = Field(default='planilha', max_length=120)

@router.post('/catalog/import')
async def run(body: RunIn, request: Request, user=Depends(managers)):
    cid = user['company_id']
    items = [{**i.model_dump(), 'problems': []} for i in body.items]
    ex = await _match(cid, items)
    s = _summary(items, len(ex), body.mode)
    if s['over']: raise HTTPException(400, f'Passaria do limite de {LIMIT} itens no catálogo. Tire {s["over"]} da planilha ou apague itens antigos.')
    jid, created, before = uid(), [], []
    t = now()
    for it in items:
        if it.get('error'): continue
        data = {k: it[k] for k in ('name', 'code', 'category', 'kind', 'price', 'duration_days', 'active')}
        if it['match']:
            if body.mode == 'skip': continue
            old = next(p for p in ex if p['id'] == it['match'])
            before.append({k: old.get(k) for k in ('id', 'name', 'code', 'category', 'kind', 'price', 'duration_days', 'active')})
            await db.products.update_one({'id': it['match']}, {'$set': data})
        else:
            p = {'id': uid(), 'company_id': cid, **data, 'next_ids': [], 'recurring': False, 'created_at': t, 'import_id': jid}
            await db.products.insert_one(p); created.append(p['id'])
    await db.catalog_imports.insert_one({'id': jid, 'company_id': cid, 'file': body.file, 'created': created, 'before': before, 'at': t, 'by': user['name'], 'undone': False})
    await audit(request, user, 'Importou o catálogo', f'{body.file}: {len(created)} novos, {len(before)} atualizados')
    return {'id': jid, 'created': len(created), 'updated': len(before)}

@router.post('/catalog/import/{jid}/undo')
async def undo(jid: str, request: Request, user=Depends(managers)):
    j = await db.catalog_imports.find_one({'id': jid, 'company_id': user['company_id']}, {'_id': 0})
    if not j: raise HTTPException(404, 'Importação não encontrada.')
    if j.get('undone'): raise HTTPException(400, 'Essa importação já foi desfeita.')
    removed = kept = 0
    for pid in j['created']:
        if await db.sales.find_one({'company_id': user['company_id'], 'items.product_id': pid}):
            await db.products.update_one({'id': pid}, {'$set': {'active': False}}); kept += 1
        else:
            await db.products.delete_one({'id': pid, 'company_id': user['company_id']}); removed += 1
    for b in j['before']:
        pid = b.pop('id'); await db.products.update_one({'id': pid, 'company_id': user['company_id']}, {'$set': b})
    await db.catalog_imports.update_one({'id': jid}, {'$set': {'undone': True}})
    await audit(request, user, 'Desfez a importação do catálogo', j['file'])
    return {'removed': removed, 'deactivated': kept, 'restored': len(j['before'])}
