"""Importar clientes e prospects de uma planilha (Excel ou CSV).

Fluxo em três passos, todos pela tela de Clientes:
1. POST /import/upload      -> lê o arquivo, guarda as linhas num rascunho e adivinha o que é cada coluna.
2. POST /import/{id}/check  -> com as colunas confirmadas, conta quem é novo, quem já existe, as compras e as linhas com problema.
3. POST /import/{id}/run    -> cria ou junta os contatos e registra as compras (marcadas como importadas).
   GET  /import/{id}        -> andamento: a importação roda em segundo plano e a tela acompanha por aqui.
   GET  /import/{id}/problems.xlsx -> as linhas que não entram ou entram com ajuste, para corrigir e importar de novo.
   POST /import/{id}/undo   -> desfaz: apaga as compras importadas e os contatos criados (se ainda não conversaram).

As linhas ficam guardadas em blocos (import_rows), não dentro do rascunho: uma planilha grande passaria do limite de um documento.

Regras:
- Telefone é a chave: o mesmo número na planilha e no Oi Nanda vira um contato só (os dados são juntados).
- Várias linhas com o mesmo telefone = várias compras da mesma pessoa.
- Prospect importado (sem compra) só entra em campanha se a pessoa autorizou receber mensagens.
- Importados não contam como "contatos novos" nos relatórios: não chegaram pelo atendimento.
"""
import asyncio, csv, io, re, unicodedata
from datetime import datetime, timedelta, date, time as dtime
from typing import Dict, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from core import db, now, uid, audit, managers

router = APIRouter()
MAX_ROWS, MAX_BYTES, CHUNK = 20000, 15 * 1024 * 1024, 1000

FIELDS = {  # chave -> (rótulo, palavras que aparecem no cabeçalho)
    'name': ('Nome', ['nome', 'cliente', 'nome completo', 'contato', 'aluno', 'paciente', 'name']),
    'phone': ('Telefone', ['telefone', 'celular', 'whatsapp', 'whats', 'fone', 'tel', 'contato telefone', 'phone', 'numero']),
    'email': ('E-mail', ['email', 'e-mail', 'mail']),
    'product': ('Produto ou plano', ['produto', 'plano', 'servico', 'item', 'curso', 'pacote', 'procedimento']),
    'date': ('Data da compra', ['data da compra', 'data compra', 'data', 'compra', 'comprou em', 'data de compra']),
    'value': ('Valor', ['valor', 'preco', 'total', 'valor pago', 'pago', 'price']),
    'expires': ('Vencimento', ['vencimento', 'validade', 'vence', 'expira', 'vence em', 'data de vencimento']),
    'tags': ('Etiquetas', ['etiqueta', 'etiquetas', 'tag', 'tags', 'grupo', 'categoria do cliente']),
    'origin': ('Origem', ['origem', 'canal', 'como conheceu', 'fonte', 'midia']),
    'notes': ('Observação', ['observacao', 'obs', 'anotacao', 'anotacoes', 'notas', 'nota', 'comentario']),
}

def _n(t):
    t = unicodedata.normalize('NFKD', str(t or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9 -]', ' ', t).strip()

def guess(headers):
    """Adivinha o campo de cada coluna pelo nome. Cada campo fica com uma coluna só (a que bate melhor)."""
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

def read_file(name, data):
    if len(data) > MAX_BYTES: raise HTTPException(400, 'A planilha passou de 15 MB. Divida em partes menores.')
    low = (name or '').lower()
    rows = []
    if low.endswith(('.xlsx', '.xlsm')):
        from openpyxl import load_workbook
        try: wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception: raise HTTPException(400, 'Não consegui abrir a planilha. Salve de novo como .xlsx ou .csv e tente outra vez.')
        ws = wb.worksheets[0]
        for r in ws.iter_rows(values_only=True):
            rows.append(list(r))
            if len(rows) > MAX_ROWS + 50: break
    elif low.endswith(('.csv', '.txt')):
        for enc in ('utf-8-sig', 'latin-1'):
            try: text = data.decode(enc); break
            except UnicodeDecodeError: continue
        first = text.split('\n', 1)[0]
        sep = max([';', ',', '\t'], key=first.count)
        for r in csv.reader(io.StringIO(text), delimiter=sep):
            rows.append(r)
            if len(rows) > MAX_ROWS + 50: break
    elif low.endswith('.xls'):
        raise HTTPException(400, 'Esse é o formato antigo do Excel (.xls). No Excel, use "Salvar como" e escolha .xlsx ou .csv.')
    else:
        raise HTTPException(400, 'Envie um arquivo .xlsx (Excel) ou .csv.')
    rows = [[('' if v is None else v) for v in r] for r in rows if r and any(str(v).strip() for v in r if v is not None)]
    if len(rows) < 2: raise HTTPException(400, 'A planilha está vazia ou só tem o cabeçalho. A primeira linha é o nome das colunas e as outras, uma pessoa por linha.')
    head = [str(h).strip() or f'Coluna {i + 1}' for i, h in enumerate(rows[0])]
    body = rows[1:]
    if len(body) > MAX_ROWS: raise HTTPException(400, f'A planilha tem mais de {MAX_ROWS:,} linhas. Divida em partes menores.'.replace(',', '.'))
    width = max(len(head), max(len(r) for r in body))
    head += [f'Coluna {i + 1}' for i in range(len(head), width)]
    return head, [list(r) + [''] * (width - len(r)) for r in body]

def to_date(v):
    if v in ('', None): return None
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date): return v
    if isinstance(v, (int, float)) and 20000 < v < 80000: return date(1899, 12, 30) + timedelta(days=int(v))  # data do Excel guardada como número
    t = str(v).strip()
    for f in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d-%m-%Y', '%d.%m.%Y', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M'):
        try: return datetime.strptime(t, f).date()
        except ValueError: pass
    return 'bad'

def to_money(v):
    if v in ('', None): return None
    if isinstance(v, (int, float)): return round(float(v), 2)
    t = re.sub(r'[^\d,.-]', '', str(v))
    if not t: return 'bad'
    if ',' in t: t = t.replace('.', '').replace(',', '.')
    elif t.count('.') > 1: t = t.replace('.', '')
    try: return round(float(t), 2)
    except ValueError: return 'bad'

def to_phone(v):
    if isinstance(v, float) and v.is_integer(): v = int(v)
    d = re.sub(r'\D', '', str(v or ''))
    if len(d) in (10, 11): d = '55' + d
    return d if 12 <= len(d) <= 13 and d.startswith('55') else ('bad' if d else None)

def parse(rows, mapping):
    """Linhas da planilha -> pessoas (agrupadas por telefone) com as compras de cada uma, e a lista de problemas."""
    col = {k: int(i) for i, k in mapping.items() if k in FIELDS}
    get = lambda r, k: r[col[k]] if k in col and col[k] < len(r) else ''
    people, errors, warns = {}, [], []
    for n, r in enumerate(rows, start=2):  # linha 1 é o cabeçalho
        name = str(get(r, 'name') or '').strip()
        ph = to_phone(get(r, 'phone'))
        if ph == 'bad': errors.append({'row': n, 'name': name, 'why': f'telefone "{get(r, "phone")}" não parece válido (precisa de DDD + número)'}); continue
        if not ph: errors.append({'row': n, 'name': name, 'why': 'sem telefone'}); continue
        p = people.setdefault(ph, {'phone': ph, 'name': '', 'email': '', 'tags': set(), 'origin': '', 'notes': [], 'buys': [], 'rows': []})
        p['rows'].append(n)
        if name and not p['name']: p['name'] = name[:80]
        em = str(get(r, 'email') or '').strip()
        if em and '@' in em and not p['email']: p['email'] = em[:120]
        for t in re.split(r'[,;/|]', str(get(r, 'tags') or '')):
            if t.strip(): p['tags'].add(t.strip()[:24])
        o = str(get(r, 'origin') or '').strip()
        if o and not p['origin']: p['origin'] = o[:60]
        nt = str(get(r, 'notes') or '').strip()
        if nt and nt not in p['notes']: p['notes'].append(nt[:500])
        prod = str(get(r, 'product') or '').strip(); val = to_money(get(r, 'value')); d = to_date(get(r, 'date')); ex = to_date(get(r, 'expires'))
        if val == 'bad': warns.append({'row': n, 'name': name, 'why': f'valor "{get(r, "value")}" não entendido: a compra entra sem valor'}); val = None
        if d == 'bad': warns.append({'row': n, 'name': name, 'why': f'data "{get(r, "date")}" não entendida: usamos a data de hoje'}); d = None
        if ex == 'bad': warns.append({'row': n, 'name': name, 'why': f'vencimento "{get(r, "expires")}" não entendido: ignorado'}); ex = None
        if d and d > (now() + timedelta(days=1)).date(): warns.append({'row': n, 'name': name, 'why': 'data da compra no futuro: usamos a data de hoje'}); d = None
        if prod or val:
            if not d: d = now().date()
            p['buys'].append({'product': prod or 'Compra importada', 'value': val or 0, 'date': d, 'expires': ex, 'row': n})
    for p in people.values():
        if not p['name']: p['name'] = '+' + p['phone']
    return people, errors, warns

async def _parse(user, rows, mapping):
    """Lê as linhas e tira o telefone da própria empresa (ele nunca entra na base de clientes)."""
    from wallet import own_numbers
    people, errors, warns = parse(rows, mapping)
    own = await own_numbers(user['company'])
    for ph in [k for k in people if k in own]:
        p = people.pop(ph)
        errors.extend({'row': n, 'name': p['name'], 'why': 'é o telefone da sua empresa: não entra como cliente'} for n in p['rows'])
    errors.sort(key=lambda e: e['row'])
    return people, errors, warns

async def _job(user, jid):
    j = await db.import_jobs.find_one({'id': jid, 'company_id': user['company_id']}, {'_id': 0, 'rows': 0})
    if not j: raise HTTPException(404, 'Importação não encontrada. Envie a planilha de novo.')
    return j

def _clean(v):
    return v if isinstance(v, (str, int, float, bool)) else str(v)

async def _save_rows(jid, cid, rows):
    docs = [{'job_id': jid, 'company_id': cid, 'n': i // CHUNK, 'rows': [[_clean(v) for v in r] for r in rows[i:i + CHUNK]]} for i in range(0, len(rows), CHUNK)]
    if docs: await db.import_rows.insert_many(docs)

async def _rows(jid):
    """Junta os blocos de linhas na ordem e devolve as datas ao tipo certo."""
    out = []
    for d in await db.import_rows.find({'job_id': jid}, {'_id': 0, 'rows': 1, 'n': 1}).sort('n', 1).to_list(None):
        out.extend(d['rows'])
    if not out: raise HTTPException(400, 'As linhas dessa planilha não estão mais aqui. Envie o arquivo de novo.')
    return _fix_rows(out)

async def _sweep(cid):
    """Rascunhos esquecidos há mais de 2 dias e linhas de importações antigas (30 dias) saem para não ocupar espaço."""
    old = await db.import_jobs.distinct('id', {'company_id': cid, '$or': [{'status': 'draft', 'created_at': {'$lt': now() - timedelta(days=2)}},
                                                                          {'created_at': {'$lt': now() - timedelta(days=30)}}]})
    if old:
        await db.import_rows.delete_many({'job_id': {'$in': old}})
        await db.import_jobs.delete_many({'id': {'$in': old}, 'status': 'draft'})

@router.post('/import/upload')
async def upload(file: UploadFile = File(...), user=Depends(managers)):
    data = await file.read()
    head, rows = read_file(file.filename, data)
    await _sweep(user['company_id'])
    j = {'id': uid(), 'company_id': user['company_id'], 'file': (file.filename or 'planilha')[:120], 'headers': head, 'count': len(rows), 'status': 'draft',
         'created_at': now(), 'by': user['name']}
    await db.import_jobs.insert_one(j.copy())
    await _save_rows(j['id'], user['company_id'], rows)
    return {'id': j['id'], 'file': j['file'], 'headers': head, 'count': len(rows), 'mapping': guess(head),
            'samples': [[('' if v is None else str(v))[:60] for v in r] for r in rows[:4]],
            'fields': {k: v[0] for k, v in FIELDS.items()}}

class CheckIn(BaseModel):
    mapping: Dict[str, str] = Field(default_factory=dict)

def _fix_rows(rows):
    """As datas voltam do banco como texto: devolve ao tipo certo para a leitura."""
    out = []
    for r in rows:
        out.append([datetime.fromisoformat(v) if isinstance(v, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?', v) else v for v in r])
    return out

async def _existing(cid, phones, fields):
    out, phones = {}, list(phones)
    for i in range(0, len(phones), 1000):
        for c in await db.contacts.find({'company_id': cid, 'wa_id': {'$in': phones[i:i + 1000]}}, {'_id': 0, **fields}).to_list(None):
            out[c['wa_id']] = c
    return out

@router.post('/import/{jid}/check')
async def check(jid: str, body: CheckIn, user=Depends(managers)):
    j = await _job(user, jid)
    if 'phone' not in body.mapping.values(): raise HTTPException(400, 'Diga qual coluna é o telefone: é por ele que a gente encontra cada pessoa.')
    people, errors, warns = await _parse(user, await _rows(jid), body.mapping)
    if j['status'] == 'draft': await db.import_jobs.update_one({'id': jid}, {'$set': {'mapping': body.mapping}})
    existing = await _existing(user['company_id'], people, {'wa_id': 1})
    cat = {_n(p['name']): p for p in await db.products.find({'company_id': user['company_id']}, {'_id': 0, 'name': 1, 'duration_days': 1}).to_list(1000)}
    buys = [b for p in people.values() for b in p['buys']]
    unknown = sorted({b['product'] for b in buys if _n(b['product']) not in cat and b['product'] != 'Compra importada'})
    clients = sum(1 for p in people.values() if p['buys'])
    return {'people': len(people), 'new': sum(1 for k in people if k not in existing), 'existing': sum(1 for k in people if k in existing),
            'clients': clients, 'prospects': len(people) - clients, 'buys': len(buys), 'value': round(sum(b['value'] for b in buys), 2),
            'with_expiry': sum(1 for b in buys if b['expires'] or (cat.get(_n(b['product'])) or {}).get('duration_days')),
            'unknown_products': unknown[:20], 'unknown_count': len(unknown), 'errors': errors[:50], 'error_count': len(errors), 'warns': warns[:50], 'warn_count': len(warns),
            'preview': [{'name': p['name'], 'phone': p['phone'], 'buys': len(p['buys']), 'exists': k in existing} for k, p in list(people.items())[:8]]}

@router.get('/import/{jid}/problems.xlsx')
async def problems(jid: str, user=Depends(managers)):
    """As linhas com problema, com as colunas originais e uma coluna "Problema" no fim. Corrige e importa de novo só esse arquivo."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    j = await _job(user, jid)
    if not j.get('mapping'): raise HTTPException(400, 'Confira as colunas antes de baixar as linhas com problema.')
    rows = await _rows(jid)
    _, errors, warns = await _parse(user, rows, j['mapping'])
    why = {}
    for e in errors + warns: why.setdefault(e['row'], []).append(e['why'])
    wb = Workbook(); ws = wb.active; ws.title = 'Linhas com problema'
    ws.append(['Linha'] + list(j['headers']) + ['Problema'])
    for n in sorted(why):
        r = rows[n - 2]
        ws.append([n] + [v.strftime('%d/%m/%Y') if isinstance(v, datetime) else v for v in r] + ['; '.join(why[n])])
    for c in ws[1]: c.font = Font(bold=True, color='FFFFFF'); c.fill = PatternFill('solid', fgColor='E8603C')
    ws.column_dimensions['A'].width = 8
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    stem = re.sub(r'[^\w-]+', '-', (j['file'] or 'planilha').rsplit('.', 1)[0])[:40].strip('-') or 'planilha'
    return StreamingResponse(buf, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             headers={'Content-Disposition': f'attachment; filename="{stem}-linhas-com-problema.xlsx"'})

class RunIn(CheckIn):
    consent: bool = False  # os prospects da planilha autorizaram receber mensagens

async def _tag(company):
    """Etiqueta só desta importação: "importado 20/11", e "importado 20/11 (2)" se já houve outra no mesmo dia."""
    base = 'importado ' + now().astimezone(__import__('agenda').tz_of(company)).strftime('%d/%m')
    used = set(await db.import_jobs.distinct('tag', {'company_id': company['id'], 'tag': {'$regex': '^' + re.escape(base)}}))
    used |= set(await db.contacts.distinct('tags', {'company_id': company['id'], 'tags': {'$regex': '^' + re.escape(base)}}))
    if base not in used: return base
    n = 2
    while f'{base} ({n})' in used: n += 1
    return f'{base} ({n})'

STALL = timedelta(minutes=3)

def _state(j):
    st = j['status']
    if st == 'running' and (j.get('beat') or j['created_at']).replace(tzinfo=None) < (now() - STALL).replace(tzinfo=None): st = 'stalled'
    return st

def _out(j):
    return {'id': j['id'], 'file': j['file'], 'status': _state(j), 'progress': j.get('progress') or {'done': 0, 'total': 0},
            'result': j.get('result'), 'tag': j.get('tag'), 'count': j.get('count'), 'created_at': j['created_at'], 'by': j.get('by')}

@router.get('/import/template.xlsx')
async def template(user=Depends(managers)):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = 'Clientes'
    ws.append(['Nome', 'Telefone', 'E-mail', 'Produto ou plano', 'Data da compra', 'Valor', 'Vencimento', 'Etiquetas', 'Origem', 'Observação'])
    ws.append(['Ana Souza', '(11) 98765-4321', 'ana@email.com', 'Plano mensal', '05/11/2026', '280,00', '05/12/2026', 'pilates, manhã', 'Instagram', 'Prefere aulas cedo'])
    ws.append(['Ana Souza', '(11) 98765-4321', '', 'Avaliação', '01/10/2026', '0', '', '', '', 'Mesma pessoa em outra linha = outra compra'])
    ws.append(['Bruno Lima', '11 91234-5678', '', '', '', '', '', 'indicação', 'Indicação', 'Sem compra: entra como prospect'])
    for c in ws[1]: c.font = Font(bold=True, color='FFFFFF'); c.fill = PatternFill('solid', fgColor='E8603C')
    for col, w in zip('ABCDEFGHIJ', (22, 18, 24, 22, 16, 12, 14, 20, 14, 36)): ws.column_dimensions[col].width = w
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             headers={'Content-Disposition': 'attachment; filename="modelo-clientes-oi-nanda.xlsx"'})

@router.get('/import/{jid}')
async def status(jid: str, user=Depends(managers)):
    return _out(await _job(user, jid))

@router.post('/import/{jid}/run')
async def run(jid: str, body: RunIn, request: Request, tasks: BackgroundTasks, user=Depends(managers)):
    j = await _job(user, jid)
    if 'phone' not in body.mapping.values(): raise HTTPException(400, 'Diga qual coluna é o telefone.')
    rows = await _rows(jid)
    tag = await _tag(user['company'])
    ok = await db.import_jobs.update_one({'id': jid, 'status': 'draft'}, {'$set': {'status': 'running', 'tag': tag, 'mapping': body.mapping, 'beat': now(),
                                                                                    'created_ids': [], 'updated_ids': [], 'progress': {'done': 0, 'total': 0}}})
    if not ok.modified_count: raise HTTPException(400, 'Essa planilha já foi importada.')
    people, errors, warns = await _parse(user, rows, body.mapping)
    await db.import_jobs.update_one({'id': jid}, {'$set': {'progress': {'done': 0, 'total': len(people)}}})
    tasks.add_task(_run, jid, j['file'], user, people, len(errors), tag, body.consent)
    return {'id': jid, 'status': 'running', 'progress': {'done': 0, 'total': len(people)}, 'tag': tag}

async def _run(jid, fname, user, people, n_errors, tag, consent):
    try: await _do_run(jid, fname, user, people, n_errors, tag, consent)
    except Exception as e:  # a importação para no meio: o que entrou fica marcado e pode ser desfeito
        import logging; logging.exception('importacao %s falhou', jid)
        await db.import_jobs.update_one({'id': jid}, {'$set': {'status': 'failed', 'error': str(e)[:300], 'done_at': now()}})

async def _do_run(jid, fname, user, people, n_errors, tag, consent):
    company = user['company']; cid = company['id']
    from sales import local_noon, refresh_contact
    from origins import origins_of
    tz = __import__('agenda').tz_of(company)
    onames = {_n(o['name']): o['id'] for o in await origins_of(company)}
    cat = {_n(p['name']): p for p in await db.products.find({'company_id': cid}, {'_id': 0}).to_list(1000)}
    have = await _existing(cid, people, {'id': 1, 'wa_id': 1, 'name': 1, 'tags': 1, 'email': 1, 'notes': 1, 'source': 1})
    created, updated, sales, done = [], [], 0, 0
    pend_c, pend_u = [], []
    async def flush():
        await db.import_jobs.update_one({'id': jid}, {'$push': {'created_ids': {'$each': pend_c}, 'updated_ids': {'$each': pend_u}},
                                                      '$set': {'progress': {'done': done, 'total': len(people)}, 'beat': now()}})
        pend_c.clear(); pend_u.clear()
    for ph, p in people.items():
        c = have.get(ph)
        tags = sorted(set(p['tags']) | {tag})
        if c:
            upd = {'tags': sorted(set(c.get('tags') or []) | set(tags))}
            if p['email'] and not c.get('email'): upd['email'] = p['email']
            if p['notes']: upd['notes'] = ((c.get('notes') or '') + ('\n' if c.get('notes') else '') + '\n'.join(p['notes']))[:3000]
            if consent and c.get('source') == 'importado': upd['consent'] = True
            await db.contacts.update_one({'id': c['id']}, {'$set': upd}); updated.append(c['id']); pend_u.append(c['id'])
        else:
            c = {'id': uid(), 'company_id': cid, 'wa_id': ph, 'name': p['name'], 'email': p['email'] or None, 'tags': tags, 'notes': '\n'.join(p['notes']) or None,
                 'source': 'importado', 'import_id': jid, 'consent': bool(consent), 'created_at': now(), 'assigned_to': None, 'unread': 0}
            o = onames.get(_n(p['origin'])) if p['origin'] else None
            if o: c['origin'] = o
            elif p['origin']: c['origin_detail'] = p['origin']
            await db.contacts.insert_one(c.copy()); created.append(c['id']); pend_c.append(c['id'])
        docs = []
        for b in p['buys']:
            prod = cat.get(_n(b['product']))
            it = {'name': prod['name'] if prod else b['product'], 'product_id': prod['id'] if prod else None, 'category': (prod or {}).get('category') or '',
                  'qty': 1, 'price': b['value'], 'discount': 0, 'total': b['value'], 'promo_discount': 0}
            if b['expires']: it['expires_at'] = datetime.combine(b['expires'], dtime(23, 59), tz)
            docs.append({'id': uid(), 'company_id': cid, 'contact_id': c['id'], 'contact_name': c.get('name'), 'date': local_noon(company, b['date'].isoformat()),
                         'items': [it], 'total': b['value'], 'subtotal': b['value'], 'discount': 0, 'promos': [], 'payment': '', 'sellers': [], 'notes': '',
                         'status': 'ok', 'imported': True, 'import_id': jid, 'created_by': user['id'], 'created_by_name': user['name'], 'created_at': now(), 'history': []})
        if docs:
            await db.sales.insert_many(docs); sales += len(docs)
            await refresh_contact(company, c['id'])
        done += 1
        if done % 25 == 0: await asyncio.sleep(0)  # deixa o resto do sistema respirar durante a importação
        if done % 100 == 0: await flush()
    res = {'created': len(created), 'updated': len(updated), 'sales': sales, 'errors': n_errors, 'tag': tag}
    await flush()
    await db.import_jobs.update_one({'id': jid}, {'$set': {'status': 'done', 'done_at': now(), 'result': res}})
    await audit(None, user, 'Importou planilha', f'{fname} · {len(created)} novos, {len(updated)} juntados, {sales} compras')

@router.get('/import')
async def history(user=Depends(managers)):
    rows = await db.import_jobs.find({'company_id': user['company_id'], 'status': {'$in': ['running', 'done', 'undone', 'failed']}},
                                     {'_id': 0, 'created_ids': 0, 'updated_ids': 0, 'headers': 0}).sort('created_at', -1).to_list(20)
    return [_out(j) for j in rows]

@router.post('/import/{jid}/undo')
async def undo(jid: str, request: Request, user=Depends(managers)):
    j = await db.import_jobs.find_one({'id': jid, 'company_id': user['company_id']}, {'_id': 0})
    if not j: raise HTTPException(404, 'Importação não encontrada.')
    if _state(j) not in ('done', 'failed', 'stalled'): raise HTTPException(400, 'Só dá para desfazer uma importação concluída. Se ainda está rodando, espere terminar.')
    from sales import refresh_contact
    co = user['company_id']
    touched = set(await db.sales.distinct('contact_id', {'company_id': co, 'import_id': jid}))
    await db.sales.delete_many({'company_id': co, 'import_id': jid})
    created = j.get('created_ids') or []
    keep = set()
    for i in range(0, len(created), 1000):
        part = created[i:i + 1000]
        keep |= set(await db.messages.distinct('contact_id', {'company_id': co, 'contact_id': {'$in': part}}))
        keep |= set(await db.sales.distinct('contact_id', {'company_id': co, 'contact_id': {'$in': part}}))
    gone = [x for x in created if x not in keep]
    for i in range(0, len(gone), 1000): await db.contacts.delete_many({'company_id': co, 'id': {'$in': gone[i:i + 1000]}})
    if keep: await db.contacts.update_many({'company_id': co, 'id': {'$in': list(keep)}}, {'$unset': {'import_id': ''}})
    tag = j.get('tag') or (j.get('result') or {}).get('tag')
    mine = [x for x in list(keep) + (j.get('updated_ids') or [])]
    if tag and mine:
        for i in range(0, len(mine), 1000): await db.contacts.update_many({'company_id': co, 'id': {'$in': mine[i:i + 1000]}}, {'$pull': {'tags': tag}})
    for cid in touched - set(gone):
        if await db.contacts.find_one({'id': cid}, {'_id': 1}): await refresh_contact(user['company'], cid)
    await db.import_jobs.update_one({'id': jid}, {'$set': {'status': 'undone', 'undone_at': now()}})
    await db.import_rows.delete_many({'job_id': jid})
    await audit(request, user, 'Desfez importação', f'{j["file"]} · {len(gone)} contatos apagados')
    return {'removed': len(gone), 'kept': len(keep)}
