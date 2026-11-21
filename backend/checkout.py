"""Plataformas de venda (Hotmart, Kiwify, Eduzz): o que acontece no checkout chega no Oi Nanda.

Cada empresa liga a plataforma em Empresa › Plataformas de venda. O Oi Nanda gera um endereço de webhook só dela;
o dono cola esse endereço na plataforma e cola aqui o código de segurança que a plataforma mostra.

Conferência de origem (sem ela, nada entra):
- Hotmart: cabeçalho X-HOTMART-HOTTOK igual ao hottok da conta.
- Kiwify: parâmetro ?signature= = HMAC-SHA1 (hex) do corpo cru, com o token do webhook como chave.
- Eduzz: cabeçalho x-signature = HMAC-SHA256 (hex) do corpo cru, com a chave secreta criada no Console.
- iugu: o gatilho manda o código de autorização cadastrado nele (Authorization). O aviso só traz o número da fatura,
  então o Oi Nanda busca a fatura na API da iugu com o token da conta: quem pagou, o quê e o link. Como a fatura vem
  direto da iugu, um aviso falso não registra nada.

O que cada evento faz:
- venda aprovada: registra a venda (sem duplicar), move o card para a etapa de venda e resolve boleto/carrinho em aberto;
- boleto ou Pix gerado: guarda o pendente (link do boleto, código Pix) para a automação "Boleto ou Pix sem pagar";
- carrinho abandonado: guarda para a automação "Carrinho abandonado";
- reembolso ou chargeback: cancela a venda que veio dessa compra.
Tudo vira nota na conversa, para o time ver o que aconteceu sem sair do Oi Nanda.
"""
import hmac, hashlib, json, logging, secrets
from datetime import timedelta
from typing import Optional, Literal
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, current_user, managers
from whatsapp import encrypt, decrypt, note, aware

log = logging.getLogger('checkout')
router = APIRouter()
PROVIDERS = {'hotmart': 'Hotmart', 'kiwify': 'Kiwify', 'eduzz': 'Eduzz', 'iugu': 'iugu'}
KIND_LABEL = {'approved': 'venda aprovada', 'pending': 'boleto ou Pix gerado', 'abandoned': 'carrinho abandonado', 'refunded': 'reembolso',
              'chargeback': 'chargeback', 'canceled': 'compra cancelada', 'refused': 'pagamento recusado', 'expired': 'boleto ou Pix vencido'}

# ---------- leitura de cada plataforma: tudo vira o mesmo formato ----------
def g(d, *path):
    for p in path:
        if not isinstance(d, dict): return None
        d = d.get(p)
    return d

def first(*vals):
    for v in vals:
        if v not in (None, '', [], {}): return v
    return None

def money(v, cents=False):
    try: x = float(str(v).replace(',', '.')) if v not in (None, '') else None
    except ValueError: return None
    return round(x / 100, 2) if x is not None and cents else x

HOTMART = {'PURCHASE_APPROVED': 'approved', 'PURCHASE_COMPLETE': 'approved', 'PURCHASE_BILLET_PRINTED': 'pending', 'PURCHASE_OUT_OF_SHOPPING_CART': 'abandoned',
           'PURCHASE_REFUNDED': 'refunded', 'PURCHASE_CHARGEBACK': 'chargeback', 'PURCHASE_PROTEST': 'refunded', 'PURCHASE_CANCELED': 'canceled',
           'PURCHASE_EXPIRED': 'expired', 'PURCHASE_DELAYED': 'refused'}
KIWIFY = {'order_approved': 'approved', 'billet_created': 'pending', 'pix_created': 'pending', 'order_rejected': 'refused',
          'order_refunded': 'refunded', 'chargeback': 'chargeback'}
EDUZZ = {'myeduzz.invoice_paid': 'approved', 'myeduzz.invoice_waiting_payment': 'pending', 'myeduzz.invoice_opened': 'pending',
         'myeduzz.invoice_refunded': 'refunded', 'myeduzz.invoice_chargeback': 'chargeback', 'myeduzz.invoice_canceled': 'canceled',
         'myeduzz.invoice_expired': 'expired', 'myeduzz.invoice_recovering': 'pending'}

def parse(provider, b):
    """Devolve o evento no formato do Oi Nanda, ou None se for um evento que não usamos."""
    if provider == 'hotmart':
        kind = HOTMART.get(b.get('event'))
        d = b.get('data') or {}; pay = g(d, 'purchase', 'payment') or {}
        return kind and {'kind': kind, 'ref': first(g(d, 'purchase', 'transaction'), b.get('id')),
            'name': g(d, 'buyer', 'name'), 'email': g(d, 'buyer', 'email'), 'phone': first(g(d, 'buyer', 'checkout_phone'), g(d, 'buyer', 'phone')),
            'product': g(d, 'product', 'name'), 'product_id': str(g(d, 'product', 'id') or ''), 'value': money(first(g(d, 'purchase', 'price', 'value'), g(d, 'purchase', 'full_price', 'value'))),
            'method': {'BILLET': 'boleto', 'PIX': 'pix', 'CREDIT_CARD': 'cartao'}.get(str(pay.get('type') or '').upper(), (pay.get('type') or '').lower()),
            'url': first(pay.get('billet_url'), g(d, 'checkout_url')), 'pix': pay.get('pix_code'), 'src': first(g(d, 'purchase', 'origin', 'src'), g(d, 'purchase', 'origin', 'sck'))}
    if provider == 'kiwify':
        if not b.get('webhook_event_type') and (b.get('checkout_link') or b.get('status') == 'abandoned'):  # carrinho abandonado vem em outro formato
            return {'kind': 'abandoned', 'ref': first(b.get('id'), b.get('checkout_link')), 'name': b.get('name'), 'email': b.get('email'), 'phone': first(b.get('phone'), b.get('mobile')),
                    'product': b.get('product_name'), 'product_id': str(b.get('product_id') or ''), 'value': None, 'method': '', 'url': b.get('checkout_link'), 'pix': None, 'src': None}
        kind = KIWIFY.get(b.get('webhook_event_type'))
        cu = b.get('Customer') or {}; pr = b.get('Product') or {}; cm = b.get('Commissions') or {}; tp = b.get('TrackingParameters') or {}
        return kind and {'kind': kind, 'ref': b.get('order_id'), 'name': first(cu.get('full_name'), cu.get('first_name')), 'email': cu.get('email'), 'phone': first(cu.get('mobile'), cu.get('phone')),
            'product': pr.get('product_name'), 'product_id': str(pr.get('product_id') or ''), 'value': money(first(cm.get('charge_amount'), cm.get('product_base_price')), cents=True),
            'method': {'credit_card': 'cartao', 'boleto': 'boleto', 'pix': 'pix'}.get(b.get('payment_method') or '', b.get('payment_method') or ''),
            'url': b.get('boleto_URL'), 'pix': b.get('pix_code'), 'src': first(tp.get('src'), tp.get('sck'), tp.get('utm_campaign'))}
    if provider == 'eduzz':
        kind = EDUZZ.get(b.get('event'))
        d = b.get('data') or {}; bu = d.get('buyer') or {}; it = (d.get('items') or [{}])[0] or {}
        return kind and {'kind': kind, 'ref': first(d.get('id'), b.get('id')), 'name': bu.get('name'), 'email': bu.get('email'), 'phone': first(bu.get('cellphone'), bu.get('phone'), bu.get('phone2')),
            'product': it.get('name'), 'product_id': str(it.get('productId') or ''), 'value': money(first(g(d, 'paid', 'value'), g(d, 'price', 'value'))),
            'method': {'bankslip': 'boleto', 'pix': 'pix', 'creditCard': 'cartao'}.get(d.get('paymentMethod') or '', d.get('paymentMethod') or ''),
            'url': first(d.get('bankslipUrl'), d.get('checkoutUrl')), 'pix': g(d, 'transaction', 'key') if d.get('paymentMethod') == 'pix' else None, 'src': g(d, 'utm', 'source')}
    if provider == 'iugu':
        return iugu_event(b)
    return None

IUGU_STATUS = {'paid': 'approved', 'refunded': 'refunded', 'chargeback': 'chargeback', 'canceled': 'canceled', 'expired': 'expired', 'pending': 'pending'}

def iugu_event(b):
    """b = {'event': ..., 'data': {...}, 'invoice': fatura buscada na API}."""
    ev, d, inv = b.get('event') or '', b.get('data') or {}, b.get('invoice') or {}
    st = (inv.get('status') or d.get('status') or '').lower()
    if ev == 'invoice.refund': kind = 'refunded'
    elif ev == 'invoice.payment_failed': kind = 'pending'  # cartão recusado: a recuperação manda o link para tentar de novo
    elif ev in ('invoice.created', 'invoice.status_changed', 'invoice.bank_slip_status', 'invoice.due'): kind = IUGU_STATUS.get(st)
    else: kind = None
    if not kind or not inv: return None
    var = {x.get('variable'): x.get('value') for x in inv.get('variables') or [] if isinstance(x, dict)}
    pre = str(first(inv.get('payer_phone_prefix'), var.get('payer.phone_prefix')) or '')
    ph = str(first(inv.get('payer_phone'), var.get('payer.phone'), g(inv, 'payer', 'phone')) or '')
    phone = (pre + ph) if ph and pre and not ph.startswith(pre) and len(ph) <= 9 else ph
    items = inv.get('items') or []
    pw = (inv.get('payment_method') or inv.get('payable_with') or '')
    pw = pw if isinstance(pw, str) else ''
    method = 'boleto' if 'bank_slip' in pw else 'pix' if 'pix' in pw else 'cartao' if 'credit_card' in pw else ''
    if ev == 'invoice.payment_failed': method = method or 'cartao'
    total = first(inv.get('total_paid_cents') if kind == 'approved' else None, inv.get('total_cents'))
    return {'kind': kind, 'ref': inv.get('id') or d.get('id'), 'name': first(inv.get('payer_name'), var.get('payer.name'), inv.get('customer_name'), g(inv, 'payer', 'name')),
            'email': first(inv.get('payer_email'), inv.get('email'), var.get('payer.email')), 'phone': phone,
            'product': first(*(i.get('description') for i in items if isinstance(i, dict))) or 'Fatura iugu', 'product_id': '',
            'value': money(total, cents=True) if total is not None else money(inv.get('total')), 'method': method,
            'url': first(inv.get('secure_url'), g(inv, 'bank_slip', 'bank_slip_url')), 'pix': first(g(inv, 'pix', 'qrcode_text'), g(inv, 'pix', 'qrcode')), 'src': None}

async def iugu_invoice(api_token, invoice_id):
    import httpx
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f'https://api.iugu.com/v1/invoices/{invoice_id}', auth=(api_token, ''))
    if r.status_code >= 300: raise HTTPException(502, f'A iugu não devolveu a fatura ({r.status_code}). Confira o token da API.')
    return r.json()

def verify(provider, secret, request, raw):
    if provider == 'hotmart':
        got = request.headers.get('x-hotmart-hottok') or ''
        return bool(got) and hmac.compare_digest(got, secret)
    if provider == 'kiwify':
        got = request.query_params.get('signature') or ''
        return bool(got) and hmac.compare_digest(got.lower(), hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest())
    if provider == 'iugu':  # o código do gatilho chega como Authorization (com ou sem "Basic", em texto ou base64)
        got = (request.headers.get('authorization') or '').strip()
        if got.lower().startswith('basic '): got = got[6:].strip()
        import base64
        opts = {secret, base64.b64encode(secret.encode()).decode(), base64.b64encode((secret + ':').encode()).decode()}
        return bool(got) and any(hmac.compare_digest(got, o) for o in opts)
    if provider == 'eduzz':
        got = (request.headers.get('x-signature') or '').strip()
        mac = hmac.new(secret.encode(), raw, hashlib.sha256)
        return bool(got) and (hmac.compare_digest(got.lower(), mac.hexdigest()) or hmac.compare_digest(got, __import__('base64').b64encode(mac.digest()).decode()))
    return False

# ---------- o que fazer com cada evento ----------
def brl(v): return f'R$ {float(v or 0):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

async def find_contact(company, ev):
    from channels import norm_phone, variants
    cid = company['id']; phone = norm_phone(ev.get('phone') or '') if ev.get('phone') else ''
    c = await db.contacts.find_one({'company_id': cid, 'wa_id': {'$in': variants(phone)}}, {'_id': 0}) if phone else None
    if not c and ev.get('email'): c = await db.contacts.find_one({'company_id': cid, 'email': ev['email'].strip().lower()}, {'_id': 0})
    if c:
        upd = {}
        if ev.get('email') and not c.get('email'): upd['email'] = ev['email'].strip().lower()
        if phone and not c.get('wa_id'): upd['wa_id'] = phone
        if upd: await db.contacts.update_one({'id': c['id']}, {'$set': upd}); c.update(upd)
        return c
    if not phone and not ev.get('email'): return None
    from funnel import stages_for
    stages = await stages_for(company)
    c = {'id': uid(), 'company_id': cid, 'wa_id': phone or None, 'email': (ev.get('email') or '').strip().lower() or None, 'name': (ev.get('name') or phone or ev.get('email') or 'Cliente').strip()[:80],
         'assigned_to': None, 'unread': 0, 'created_at': now(), 'last_at': now(), 'last_text': f'Veio da {PROVIDERS[ev["provider"]]}', 'last_dir': 'sys',
         'source': 'checkout', 'checkout_src': ev.get('src'), 'stage': stages[0]['id'], 'stage_at': now()}
    await db.contacts.insert_one(c.copy())
    return c

async def apply(company, ev):
    from funnel import stages_for, move_stage
    from sales import refresh_contact
    c = await find_contact(company, ev)
    if not c: return {'ok': True, 'skipped': 'sem telefone nem e-mail'}
    cid, p = company['id'], PROVIDERS[ev['provider']]
    what = f'{ev.get("product") or "produto"}' + (f' · {brl(ev["value"])}' if ev.get('value') else '')
    if ev['kind'] == 'approved':
        if not await db.sales.find_one({'company_id': cid, 'external_ref': ev['ref'], 'provider': ev['provider']}):
            total = ev.get('value') or 0
            sale = {'id': uid(), 'company_id': cid, 'contact_id': c['id'], 'contact_name': c.get('name'), 'date': now(),
                    'items': [{'product_id': None, 'name': ev.get('product') or f'Venda {p}', 'category': p, 'qty': 1, 'price': total, 'discount': 0, 'total': total}],
                    'total': total, 'subtotal': total, 'discount': 0, 'payment': ev.get('method') or '', 'notes': f'Venda aprovada na {p}',
                    'sellers': [{'user_id': c['assigned_to'], 'pct': 100}] if c.get('assigned_to') else [], 'team_id': c.get('team_id'),
                    'source': c.get('source') or 'checkout', 'status': 'ok', 'provider': ev['provider'], 'external_ref': ev['ref'],
                    'created_by': None, 'created_by_name': p, 'created_at': now(), 'history': []}
            await db.sales.insert_one(sale.copy())
            stages = await stages_for(company)
            won = next((s for s in stages if s['kind'] == 'won'), None)
            if won and next((s['kind'] for s in stages if s['id'] == c.get('stage')), None) != 'won': await move_stage(company, c, won['id'], p, stages)
            await refresh_contact(company, c['id'])
            try:
                from opps import on_sale
                await on_sale(cid, c['id'], sale)
            except Exception: log.exception('pós-venda da venda da plataforma')
            await note(cid, c['id'], f'{p}: venda aprovada · {what}. Registrada em Vendas.')
        # quem comprou não recebe mais a recuperação de boleto ou de carrinho
        await db.checkout_events.update_many({'company_id': cid, 'contact_id': c['id'], 'kind': {'$in': ['pending', 'abandoned']}, 'resolved': {'$ne': True}}, {'$set': {'resolved': True, 'resolved_at': now()}})
    elif ev['kind'] in ('pending', 'abandoned'):
        lab = 'gerou boleto' if ev.get('method') == 'boleto' else 'gerou Pix' if ev.get('method') == 'pix' else 'gerou o pagamento' if ev['kind'] == 'pending' else 'abandonou o carrinho'
        await note(cid, c['id'], f'{p}: {lab} · {what}' + ('. Pagamento ainda não entrou.' if ev['kind'] == 'pending' else '. Não concluiu a compra.'))
    elif ev['kind'] in ('refunded', 'chargeback', 'canceled'):
        s = await db.sales.find_one({'company_id': cid, 'external_ref': ev['ref'], 'provider': ev['provider'], 'status': 'ok'}, {'_id': 0})
        if s:
            await db.sales.update_one({'id': s['id']}, {'$set': {'status': 'cancelled', 'cancelled_at': now()}, '$push': {'history': {'at': now(), 'by': p, 'what': 'cancelou: ' + KIND_LABEL[ev['kind']]}}})
            await refresh_contact(company, c['id'])
        await note(cid, c['id'], f'{p}: {KIND_LABEL[ev["kind"]]} · {what}' + ('. A venda foi cancelada em Vendas.' if s else '.'))
    else:
        await note(cid, c['id'], f'{p}: {KIND_LABEL.get(ev["kind"], ev["kind"])} · {what}.')
    return {'ok': True, 'contact_id': c['id']}

# ---------- webhook ----------
@router.post('/checkout/{provider}/{hid}')
async def webhook(provider: str, hid: str, request: Request):
    if provider not in PROVIDERS: raise HTTPException(404, 'Plataforma desconhecida.')
    h = await db.checkout_hooks.find_one({'id': hid, 'provider': provider, 'active': True}, {'_id': 0})
    if not h: raise HTTPException(404, 'Integração não encontrada.')
    raw = await request.body()
    if not h.get('secret_enc'): raise HTTPException(401, 'Falta colar o código de segurança no Oi Nanda.')
    if not verify(provider, decrypt(h['secret_enc']), request, raw):
        await db.checkout_hooks.update_one({'id': hid}, {'$set': {'last_error': 'Assinatura não confere: confira o código de segurança.', 'last_error_at': now()}})
        raise HTTPException(401, 'Assinatura inválida.')
    try: body = json.loads(raw or b'{}')
    except ValueError:
        from urllib.parse import parse_qs  # iugu manda como formulário: event=...&data[id]=...&data[status]=...
        q = {k: v[-1] for k, v in parse_qs(raw.decode('utf-8', 'ignore')).items()}
        if not q: raise HTTPException(400, 'Corpo inválido.')
        body = {'event': q.get('event'), 'data': {k[5:-1]: v for k, v in q.items() if k.startswith('data[') and k.endswith(']')}}
    if provider == 'iugu':
        iid = (body.get('data') or {}).get('id')
        if body.get('event', '').startswith('invoice.') and iid:
            if not h.get('api_enc'): raise HTTPException(400, 'Falta o token da API da iugu no Oi Nanda.')
            body['invoice'] = await iugu_invoice(decrypt(h['api_enc']), iid)
    ev = parse(provider, body)
    await db.checkout_hooks.update_one({'id': hid}, {'$set': {'last_at': now(), 'last_error': None}, '$inc': {'received': 1}})
    if not ev: return {'ok': True, 'ignored': True}  # evento que não usamos (ex.: acesso à área de membros)
    ev['provider'] = provider
    if not ev.get('ref'): ev['ref'] = hashlib.sha1(raw).hexdigest()
    company = await db.companies.find_one({'id': h['company_id']}, {'_id': 0})
    if not company: raise HTTPException(404, 'Empresa não encontrada.')
    key = f'{provider}:{ev["ref"]}:{ev["kind"]}'
    if await db.checkout_events.find_one({'company_id': company['id'], 'key': key}): return {'ok': True, 'duplicate': True}
    r = await apply(company, ev)
    await db.checkout_events.insert_one({'id': uid(), 'company_id': company['id'], 'key': key, 'provider': provider, 'kind': ev['kind'], 'ref': ev['ref'],
                                         'contact_id': r.get('contact_id'), 'product': ev.get('product'), 'value': ev.get('value'), 'method': ev.get('method'),
                                         'url': ev.get('url'), 'pix': ev.get('pix'), 'src': ev.get('src'), 'resolved': ev['kind'] not in ('pending', 'abandoned'), 'at': now()})
    if ev['kind'] in ('pending', 'abandoned') and r.get('contact_id'):
        # comprou antes deste aviso chegar? então não há o que recuperar
        bought = await db.sales.find_one({'company_id': company['id'], 'contact_id': r['contact_id'], 'provider': provider, 'status': 'ok', 'created_at': {'$gte': now() - timedelta(hours=6)}})
        has_pending = ev['kind'] == 'abandoned' and await db.checkout_events.find_one({'company_id': company['id'], 'contact_id': r['contact_id'], 'kind': 'pending', 'resolved': {'$ne': True}, 'at': {'$gte': now() - timedelta(days=2)}})
        if bought or has_pending:  # já comprou, ou já gerou boleto/Pix (aí a recuperação certa é a do pagamento)
            await db.checkout_events.update_one({'key': key, 'company_id': company['id']}, {'$set': {'resolved': True}})
    return {'ok': True}

# ---------- configuração (Empresa › Plataformas de venda) ----------
def hook_out(h, base):
    return {'id': h['id'], 'provider': h['provider'], 'name': PROVIDERS[h['provider']], 'url': f'{base}/api/checkout/{h["provider"]}/{h["id"]}',
            'has_secret': bool(h.get('secret_enc')) and (h['provider'] != 'iugu' or bool(h.get('api_enc'))), 'has_api': bool(h.get('api_enc')), 'active': h.get('active', True), 'last_at': h.get('last_at'), 'received': h.get('received', 0),
            'last_error': h.get('last_error'), 'created_at': h.get('created_at')}

def base_url():
    import os
    return os.environ.get('APP_PUBLIC_URL', 'https://app.oinanda.com.br').rstrip('/')

@router.get('/checkout/hooks')
async def list_hooks(user=Depends(managers)):
    rows = await db.checkout_hooks.find({'company_id': user['company_id']}, {'_id': 0}).to_list(10)
    since = now() - timedelta(days=30)
    st = {}
    for k in ('approved', 'pending', 'abandoned'):
        st[k] = await db.checkout_events.count_documents({'company_id': user['company_id'], 'kind': k, 'at': {'$gte': since}})
    rec = await db.checkout_events.find({'company_id': user['company_id'], 'kind': {'$in': ['pending', 'abandoned']}, 'resolved_at': {'$gte': since}}, {'_id': 0, 'value': 1}).to_list(20000)
    st['recovered'] = len(rec); st['recovered_value'] = round(sum(r.get('value') or 0 for r in rec), 2)
    st['approved_value'] = round(sum(r.get('value') or 0 for r in await db.checkout_events.find({'company_id': user['company_id'], 'kind': 'approved', 'at': {'$gte': since}}, {'_id': 0, 'value': 1}).to_list(20000)), 2)
    return {'items': [hook_out(h, base_url()) for h in rows], 'stats': st}

@router.get('/checkout/hooks/{hid}/events')
async def hook_events(hid: str, user=Depends(managers)):
    """Últimos eventos que chegaram desta plataforma: para o dono ver que está funcionando (e o que chegou)."""
    h = await db.checkout_hooks.find_one({'id': hid, 'company_id': user['company_id']}, {'_id': 0})
    if not h: raise HTTPException(404, 'Integração não encontrada.')
    rows = await db.checkout_events.find({'company_id': user['company_id'], 'provider': h['provider']}, {'_id': 0}).sort('at', -1).to_list(8)
    names = {c['id']: c.get('name') for c in await db.contacts.find({'id': {'$in': [r.get('contact_id') for r in rows if r.get('contact_id')]}}, {'_id': 0, 'id': 1, 'name': 1}).to_list(20)}
    return {'hook': hook_out(h, base_url()), 'events': [{'kind': r['kind'], 'label': KIND_LABEL.get(r['kind'], r['kind']), 'product': r.get('product'), 'value': r.get('value'),
            'contact_id': r.get('contact_id'), 'name': names.get(r.get('contact_id')), 'resolved': r.get('resolved'), 'at': r['at']} for r in rows]}

class HookIn(BaseModel):
    provider: Literal['hotmart', 'kiwify', 'eduzz', 'iugu']
    secret: Optional[str] = Field(default=None, min_length=6, max_length=200)
    api_token: Optional[str] = Field(default=None, min_length=10, max_length=200)  # só a iugu: para buscar a fatura

@router.post('/checkout/hooks')
async def add_hook(body: HookIn, request: Request, user=Depends(managers)):
    h = await db.checkout_hooks.find_one({'company_id': user['company_id'], 'provider': body.provider}, {'_id': 0})
    if not h:
        h = {'id': secrets.token_urlsafe(12).replace('-', 'x').replace('_', 'y'), 'company_id': user['company_id'], 'provider': body.provider, 'active': True, 'received': 0, 'created_at': now()}
        await db.checkout_hooks.insert_one(h.copy())
    if body.secret:
        await db.checkout_hooks.update_one({'id': h['id']}, {'$set': {'secret_enc': encrypt(body.secret.strip()), 'active': True, 'last_error': None}})
        h['secret_enc'] = 'x'
    if body.api_token and body.provider == 'iugu':
        await db.checkout_hooks.update_one({'id': h['id']}, {'$set': {'api_enc': encrypt(body.api_token.strip()), 'last_error': None}})
    await audit(request, user, f'Ligou a integração com a {PROVIDERS[body.provider]}')
    return hook_out({**h, **(await db.checkout_hooks.find_one({'id': h['id']}, {'_id': 0}))}, base_url())

@router.delete('/checkout/hooks/{hid}')
async def del_hook(hid: str, request: Request, user=Depends(managers)):
    h = await db.checkout_hooks.find_one({'id': hid, 'company_id': user['company_id']}, {'_id': 0})
    if not h: raise HTTPException(404, 'Integração não encontrada.')
    await db.checkout_hooks.delete_one({'id': hid})
    await audit(request, user, f'Desligou a integração com a {PROVIDERS[h["provider"]]}')
    return {'ok': True}

@router.get('/contacts/{contact_id}/checkout')
async def contact_checkout(contact_id: str, user=Depends(current_user)):
    from whatsapp import get_contact
    await get_contact(user, contact_id)
    return await db.checkout_events.find({'company_id': user['company_id'], 'contact_id': contact_id}, {'_id': 0}).sort('at', -1).to_list(30)
