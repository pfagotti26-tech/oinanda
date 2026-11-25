"""Ensinar a Nanda numa frase: o dono escreve como é o negócio (ou indica o site) e a IA organiza sobre, serviços com preço,
horários e pagamento para ele conferir antes de salvar. Nada é salvo sem o dono confirmar."""
import ipaddress, json, re, socket
from typing import List, Optional
from urllib.parse import urljoin, urlparse
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, managers
import ai

router = APIRouter()

PROMPT = """Você organiza as informações de um pequeno negócio brasileiro para a assistente de atendimento dele.
Leia o texto do dono (e, se houver, o texto do site) e devolva SÓ um JSON, sem comentário:
{"sobre": "uma frase: o que é o negócio e onde fica", "onde": "endereço ou como atende (online, delivery...), ou vazio",
 "itens": [{"nome": "serviço ou produto", "preco": número em reais ou null, "a_partir": true se o preço é 'a partir de', "duracao_min": minutos ou null}],
 "horarios": [{"dias": [números de 0=segunda a 6=domingo], "de": "HH:MM", "ate": "HH:MM"}],
 "pagamento": "formas de pagamento e parcelamento, ou vazio",
 "faltou": ["o que um cliente costuma perguntar e não está no texto, no máximo 2, curto"]}
Regras: use SOMENTE o que está escrito. Nunca invente preço, horário, endereço nem serviço. O que não estiver no texto fica vazio ou null.
Escreva em português do Brasil, com letra maiúscula no começo dos nomes. Preço só o número (ex.: 180, 1500.5). No máximo 20 itens."""

class DescIn(BaseModel):
    text: str = Field(default='', max_length=1500)
    url: str = Field(default='', max_length=300)

def _public(host):
    try: infos = socket.getaddrinfo(host, None)
    except Exception: return False
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified: return False
    return True

async def site_text(url):
    """Texto do site do negócio, com cuidado: só endereço público, pouco conteúdo e poucos redirecionamentos."""
    url = url.strip()
    if not url: return ''
    if not re.match(r'^https?://', url, re.I): url = 'https://' + url
    if re.search(r'(instagram|facebook|tiktok)\.com', url, re.I):
        raise HTTPException(400, 'Do Instagram não dá para ler direto. Copie a sua bio e os preços e cole no texto, ou use o endereço do seu site.')
    async with httpx.AsyncClient(timeout=8, follow_redirects=False, headers={'User-Agent': 'Mozilla/5.0 (OiNanda)'}) as c:
        for _ in range(4):
            u = urlparse(url)
            if u.scheme not in ('http', 'https') or not u.hostname or not _public(u.hostname): raise HTTPException(400, 'Não consegui abrir esse endereço. Confira se é o site do seu negócio.')
            try:
                async with c.stream('GET', url) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('location'):
                        url = urljoin(url, r.headers['location']); continue
                    if r.status_code >= 400: raise HTTPException(400, 'O site não abriu agora. Tente de novo ou escreva no texto.')
                    body = b''
                    async for ch in r.aiter_bytes():
                        body += ch
                        if len(body) > 400_000: break
            except httpx.HTTPError:
                raise HTTPException(400, 'O site não abriu agora. Tente de novo ou escreva no texto.')
            html = body.decode('utf-8', 'ignore')
            html = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', html)
            txt = re.sub(r'<[^>]+>', ' ', html)
            txt = re.sub(r'&nbsp;|&#160;', ' ', txt)
            return re.sub(r'\s+', ' ', txt).strip()[:6000]
    raise HTTPException(400, 'O site redireciona demais. Escreva no texto.')

def _hm(v):
    m = re.match(r'^(\d{1,2})(?::|h)?(\d{2})?$', str(v or '').strip())
    if not m: return ''
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return f'{h:02d}:{mi:02d}' if 0 <= h <= 23 and 0 <= mi <= 59 else ''

def normalize(d):
    its = []
    for x in (d.get('itens') or [])[:20]:
        if not isinstance(x, dict) or not str(x.get('nome') or '').strip(): continue
        p = x.get('preco')
        p = float(p) if isinstance(p, (int, float)) and 0 < p <= 10_000_000 else None
        m = x.get('duracao_min')
        its.append({'name': str(x['nome']).strip()[:80], 'price': p, 'from': bool(x.get('a_partir')) and p is not None,
                    'mins': int(m) if isinstance(m, (int, float)) and 5 <= m <= 600 else None})
    grid = []
    for g in (d.get('horarios') or [])[:7]:
        if not isinstance(g, dict): continue
        days = sorted({int(x) for x in g.get('dias') or [] if isinstance(x, (int, float)) and 0 <= int(x) <= 6})
        a, b = _hm(g.get('de')), _hm(g.get('ate'))
        if days and a and b and a < b: grid.append({'days': days, 'from': a, 'to': b})
    return {'about': str(d.get('sobre') or '').strip()[:300], 'where': str(d.get('onde') or '').strip()[:300], 'items': its, 'hours_grid': grid,
            'payment': str(d.get('pagamento') or '').strip()[:300], 'missing': [str(x).strip()[:120] for x in (d.get('faltou') or []) if str(x).strip()][:2]}

@router.post('/ai/describe')
async def describe(body: DescIn, user=Depends(managers)):
    text = body.text.strip()
    site = await site_text(body.url) if body.url.strip() else ''
    if len(text) < 15 and not site: raise HTTPException(400, 'Escreva um pouco mais: o que você faz, os preços e quando atende.')
    seg = user['company'].get('segment') or 'outro'
    msg = f"Negócio: {user['company'].get('name')} (ramo: {seg}).\n\nTEXTO DO DONO:\n{text or '(vazio)'}" + (f'\n\nTEXTO DO SITE:\n{site}' if site else '')
    out, usage = await ai.llm(PROMPT, [{'role': 'user', 'content': msg}], max_tokens=1500)
    await ai.track(user['company_id'], usage, 'describe')
    m = re.search(r'\{.*\}', out or '', re.S)
    try: d = json.loads(m.group(0)) if m else {}
    except Exception: d = {}
    r = normalize(d)
    if not (r['about'] or r['items'] or r['hours_grid']): raise HTTPException(422, 'Não consegui entender o suficiente. Escreva o que você vende, quanto custa e quando atende.')
    return r

class SaveItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    mins: Optional[int] = Field(default=None, ge=5, le=600)
    from_: bool = Field(default=False, alias='from')
    consult: bool = False

class SaveIn(BaseModel):
    about: str = Field(default='', max_length=300)
    where: str = Field(default='', max_length=300)
    items: List[SaveItem] = Field(default_factory=list, max_length=20)
    hours_grid: List[dict] = Field(default_factory=list, max_length=7)
    payment: str = Field(default='', max_length=300)
    rules: List[str] = Field(default_factory=list, max_length=20)  # regras tocadas (tolerância, sinal...)
    notes: List[str] = Field(default_factory=list, max_length=10)  # detalhes curtos (almoço, estacionamento, desconto no Pix)

def hours_text(grid):
    names = ['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo']
    parts = []
    for g in grid:
        ds = g['days']
        if len(ds) > 2 and ds == list(range(ds[0], ds[-1] + 1)): d = f'de {names[ds[0]]} a {names[ds[-1]]}'
        else: d = ', '.join(names[i] for i in ds)
        parts.append(f"{d}, das {g['from']} às {g['to']}")
    return '; '.join(parts)

@router.post('/ai/describe/save')
async def save(body: SaveIn, request: Request, user=Depends(managers)):
    """Salva o que o dono conferiu: itens no Catálogo, horário e endereço no 'sobre', pagamento e as condições de preço no que a Nanda sabe."""
    cid = user['company_id']
    grid = normalize({'horarios': [{'dias': g.get('days'), 'de': g.get('from'), 'ate': g.get('to')} for g in body.hours_grid]})['hours_grid']
    q = ai.QuickIn(items=[ai.QuickItem(name=i.name, price=None if i.consult else i.price, price_from=i.from_ and not i.consult, consult=i.consult) for i in body.items], hours=hours_text(grid), where=body.where.strip(), hours_grid=grid or None)
    await ai.quick(q, request, user)
    s = await ai.settings_for(cid); upd = {}
    if body.about.strip():
        rest = [l for l in (s.get('about') or '').split('\n') if l.startswith(('Horário de atendimento:', 'Onde fica / como atende:'))]
        upd['about'] = '\n'.join([body.about.strip(), *rest])
    notes = [f'{i.name} leva cerca de {i.mins} minutos' for i in body.items if i.mins]
    notes += [f'{i.name}: valor sob consulta. Não passe preço: ofereça a avaliação ou chame o time.' for i in body.items if i.consult]
    if body.payment.strip(): notes.append(f'Formas de pagamento: {body.payment.strip()}')
    notes += [n.strip()[:200] for n in body.notes if n.strip()]
    rules = [r.strip()[:200] for r in body.rules if r.strip()]
    if rules:
        cur = s.get('rules') or ''
        upd['rules'] = (cur.strip() + '\n' + '\n'.join(r for r in rules if r not in cur)).strip()
    if notes:
        k = s.get('knowledge') or ''
        upd['knowledge'] = (k.strip() + '\n' + '\n'.join(n for n in notes if n not in k)).strip()
    if upd: await db.ai_settings.update_one({'company_id': cid}, {'$set': upd}, upsert=True)
    return {'ok': True}
