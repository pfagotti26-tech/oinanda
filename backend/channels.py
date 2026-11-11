"""Um cliente, dois canais: quem chamou no Instagram pode continuar no WhatsApp (e o contrário).

- A ficha guarda ig_id e wa_id juntos; cada canal tem a sua janela (ig_last_in_at / wa_last_in_at).
- reply_channel diz por onde sai a resposta. Segue o último canal em que o cliente escreveu, e o time pode trocar.
- Número de telefone mandado no Direct vira sugestão (phone_hint) para salvar como WhatsApp.
- Se já existir uma ficha com esse WhatsApp, as duas viram uma só (mensagens, vendas, agenda... tudo vai junto).
"""
import re, logging
from datetime import timedelta
from urllib.parse import quote
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, audit, current_user

log = logging.getLogger('channels')
router = APIRouter()

# ---------- telefone ----------
def norm_phone(v):
    """Número digitado pelo cliente -> formato do WhatsApp (só dígitos, com DDI). None se não parece telefone."""
    raw = (v or '').strip()
    d = re.sub(r'\D', '', raw)
    if raw.startswith('+') or raw.startswith('00'):
        d = d[2:] if raw.startswith('00') else d
        return d if 10 <= len(d) <= 15 else None
    if d.startswith('0'): d = d.lstrip('0')  # 011 9..., 0xx
    if len(d) in (10, 11) and d[0] != '0' and d[2:3] != '0': return '55' + d  # DDD + número (Brasil)
    if len(d) in (12, 13) and d.startswith('55'): return d
    return None

def variants(wa):
    """O WhatsApp às vezes devolve celular do Brasil sem o 9 da frente (e o cliente digita com)."""
    if not wa: return []
    out = [wa]
    if wa.startswith('55') and len(wa) == 13 and wa[4] == '9': out.append(wa[:4] + wa[5:])
    elif wa.startswith('55') and len(wa) == 12: out.append(wa[:4] + '9' + wa[4:])
    return out

PHONE_RE = re.compile(r'(?<![\w])(?:\+?55[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?![\w])|\+\d[\d\s().-]{8,18}\d')

def phones_in(text):
    seen = []
    for m in PHONE_RE.finditer(text or ''):
        n = norm_phone(m.group(0))
        if n and n not in seen: seen.append(n)
    return seen

def fmt(wa):
    if wa and wa.startswith('55') and len(wa) in (12, 13):
        r = wa[4:]
        return f'({wa[2:4]}) {r[:-4]}-{r[-4:]}'
    return '+' + wa if wa else ''

# ---------- os canais ----------
# Cada canal diz onde guarda o id do cliente na ficha, onde guarda a janela e quanto tempo ela dura.
# WhatsApp e Instagram vêm de fábrica. Os outros a empresa liga em Empresa › Mais canais.
CH = {
    'whatsapp':  {'label': 'WhatsApp',  'id': 'wa_id', 'at': 'wa_last_in_at', 'window': timedelta(hours=24), 'core': True},
    'instagram': {'label': 'Instagram', 'id': 'ig_id', 'at': 'ig_last_in_at', 'window': timedelta(hours=24), 'core': True},
    'messenger': {'label': 'Messenger', 'id': 'fb_id', 'at': 'fb_last_in_at', 'window': timedelta(hours=24)},
    'telegram':  {'label': 'Telegram',  'id': 'tg_id', 'at': 'tg_last_in_at', 'window': None},
    'email':     {'label': 'E-mail',    'id': 'em_id', 'at': 'em_last_in_at', 'window': None},
}
CORE = tuple(k for k, v in CH.items() if v.get('core'))
OPT = tuple(k for k, v in CH.items() if not v.get('core'))
DEFAULT = 'whatsapp'

def label(ch): return (CH.get(ch) or {}).get('label') or ch
def window_of(ch): return (CH.get(ch) or {}).get('window')
def id_of(c, ch): return (c or {}).get(CH[ch]['id']) if ch in CH else None
def has(c, ch): return bool(id_of(c, ch))
def channels_of(c): return [ch for ch in CH if has(c, ch)]

def on(company, ch):
    """O canal está ligado para a empresa? Os de fábrica estão sempre."""
    return True if ch in CORE else bool(((company or {}).get('channels') or {}).get(ch))

async def enabled(company_id):
    co = await db.companies.find_one({'id': company_id}, {'_id': 0, 'channels': 1}) or {}
    return [ch for ch in CH if on(co, ch)]

# ---------- canal de cada conversa ----------
def last_in(c, ch):
    if ch not in CH: return None
    v = c.get(CH[ch]['at'])
    if v: return v
    return c.get('last_in_at') if (c.get('channel') or DEFAULT) == ch else None  # ficha antiga, de um canal só

def out_channel(c):
    """Por onde a resposta sai: o que o time escolheu, senão o último canal em que o cliente escreveu."""
    got = channels_of(c)
    if c.get('reply_channel') in got: return c['reply_channel']
    if not got: return c.get('channel') or DEFAULT
    if len(got) == 1: return got[0]
    from whatsapp import aware
    wrote = [x for x in got if last_in(c, x)]
    if wrote: return max(wrote, key=lambda x: aware(last_in(c, x)))
    return c['channel'] if c.get('channel') in got else got[0]

def view(c, ch=None):
    """A ficha vista pelo canal de saída: channel e last_in_at daquele canal (o resto do código usa esses dois)."""
    if not c or len(channels_of(c)) < 2: return c
    ch = ch or out_channel(c)
    return {**c, 'channel': ch, 'last_in_at': last_in(c, ch)}

def on_inbound_set(c, ch, at):
    """Campos a gravar quando o cliente escreve por um canal."""
    if ch not in CH: return {}
    upd = {CH[ch]['at']: at}
    if len(channels_of(c)) > 1 and out_channel(c) != ch: upd['reply_channel'] = ch
    return upd

# ---------- juntar duas fichas ----------
MOVE = ('messages', 'appointments', 'campaign_sends', 'coupons', 'journey', 'notify_log', 'opps', 'quality', 'sales', 'stage_events', 'transfers')
NEVER = {'_id', 'id', 'company_id', 'created_at', 'name', 'unread', 'tags', 'sales_count', 'channel', 'reply_channel', 'phone_hint',
         'last_at', 'last_text', 'last_dir', 'last_in_at'} | {v['at'] for v in CH.values()}

def _later(a, b):
    from whatsapp import aware
    a, b = aware(a), aware(b)
    return max(x for x in (a, b) if x) if (a or b) else None

def _is_placeholder(name, c):
    n = (name or '').strip()
    return not n or n == c.get('wa_id') or n.startswith('@') or n == 'Instagram' or re.fullmatch(r'[\d+() .-]+', n)

async def merge(keep, drop):
    """Tudo da ficha `drop` passa para `keep`, e `drop` some."""
    cid = keep['company_id']
    for col in MOVE:
        await db[col].update_many({'company_id': cid, 'contact_id': drop['id']}, {'$set': {'contact_id': keep['id']}})
    upd = {k: v for k, v in drop.items() if k not in NEVER and v not in (None, '', [], {}) and keep.get(k) in (None, '', [], {})}
    if _is_placeholder(keep.get('name'), keep) and not _is_placeholder(drop.get('name'), drop): upd['name'] = drop['name']
    upd['tags'] = list(dict.fromkeys((keep.get('tags') or []) + (drop.get('tags') or [])))
    for ch, meta in CH.items():
        v = _later(last_in(keep, ch), last_in(drop, ch))
        if v: upd[meta['at']] = v
    upd['last_in_at'] = _later(keep.get('last_in_at'), drop.get('last_in_at'))
    from whatsapp import aware
    if (aware(drop.get('last_at')) or now().replace(year=2000)) > (aware(keep.get('last_at')) or now().replace(year=2000)):
        upd.update(last_at=drop.get('last_at'), last_text=drop.get('last_text'), last_dir=drop.get('last_dir'))
    ca = [aware(x) for x in (keep.get('created_at'), drop.get('created_at')) if x]
    if ca: upd['created_at'] = min(ca)
    if not keep.get('channel') and drop.get('channel'): pass  # a ficha que fica decide a origem do canal
    await db.contacts.delete_one({'id': drop['id'], 'company_id': cid})  # antes: wa_id/ig_id são únicos na empresa
    await db.contacts.update_one({'id': keep['id']}, {'$set': upd, '$inc': {'unread': drop.get('unread') or 0, 'sales_count': drop.get('sales_count') or 0},
                                                       '$unset': {'phone_hint': ''}})
    return await db.contacts.find_one({'id': keep['id']}, {'_id': 0})

# ---------- rotas ----------
class PhoneIn(BaseModel):
    phone: str = Field(min_length=8, max_length=30)

@router.post('/conversations/{contact_id}/whatsapp')
async def save_whatsapp(contact_id: str, body: PhoneIn, request: Request, user=Depends(current_user)):
    """Salva o WhatsApp de quem chamou no Instagram. Se o número já tem ficha, junta as duas."""
    from whatsapp import get_contact, note
    c = await get_contact(user, contact_id)
    wa = norm_phone(body.phone)
    if not wa: raise HTTPException(400, 'Esse número não parece um WhatsApp. Use DDD + número, ex.: (11) 94199-9929.')
    if c.get('wa_id') and c['wa_id'] in variants(wa): return {'contact_id': c['id'], 'merged': False, 'wa_id': c['wa_id']}
    if c.get('wa_id'): raise HTTPException(400, f'{c["name"]} já tem o WhatsApp {fmt(c["wa_id"])}.')
    other = await db.contacts.find_one({'company_id': c['company_id'], 'wa_id': {'$in': variants(wa)}, 'id': {'$ne': c['id']}}, {'_id': 0})
    who = user.get('impersonated_by') or user['name']
    if other:
        if other.get('ig_id') and c.get('ig_id'): raise HTTPException(409, f'Esse WhatsApp já está na ficha de {other["name"]}, que tem outro Instagram.')
        k = await merge(c, other)
        await note(c['company_id'], c['id'], f'{who} juntou esta conversa com a ficha do WhatsApp {fmt(other["wa_id"])} ({other["name"]}). O histórico dos dois canais fica aqui.')
        await audit(request, user, f'Juntou as fichas de {c["name"]} e {other["name"]}')
        return {'contact_id': k['id'], 'merged': True, 'wa_id': k.get('wa_id')}
    await db.contacts.update_one({'id': c['id']}, {'$set': {'wa_id': wa}, '$unset': {'phone_hint': ''}})
    await note(c['company_id'], c['id'], f'{who} salvou o WhatsApp {fmt(wa)}. Quando {c["name"].split(" ")[0]} escrever por lá, cai nesta mesma conversa.')
    return {'contact_id': c['id'], 'merged': False, 'wa_id': wa}

@router.delete('/conversations/{contact_id}/phone-hint')
async def drop_hint(contact_id: str, user=Depends(current_user)):
    from whatsapp import get_contact
    c = await get_contact(user, contact_id)
    await db.contacts.update_one({'id': c['id']}, {'$unset': {'phone_hint': ''}, '$set': {'phone_hint_no': c.get('phone_hint')}})
    return {'ok': True}

class ChannelIn(BaseModel):
    channel: str

@router.post('/conversations/{contact_id}/channel')
async def set_channel(contact_id: str, body: ChannelIn, user=Depends(current_user)):
    from whatsapp import get_contact
    if body.channel not in CH: raise HTTPException(400, 'Canal desconhecido.')
    c = await get_contact(user, contact_id)
    if not has(c, body.channel): raise HTTPException(400, f'Este cliente não tem {label(body.channel)}.')
    await db.contacts.update_one({'id': c['id']}, {'$set': {'reply_channel': body.channel}})
    return {'ok': True}

async def wa_link(company_id, contact=None):
    """Link para o cliente chamar no WhatsApp da empresa, com o @ do Instagram (assim a conversa cai na mesma ficha)."""
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0, 'display_phone': 1})
    d = re.sub(r'\D', '', (n or {}).get('display_phone') or '')
    if not d: return None
    msg = 'Oi! Vim do Instagram' + (f' (@{contact["ig_username"]})' if contact and contact.get('ig_username') else '')
    return f'https://wa.me/{d}?text={quote(msg)}'

@router.get('/conversations/{contact_id}/wa-invite')
async def wa_invite(contact_id: str, user=Depends(current_user)):
    from whatsapp import get_contact
    c = await get_contact(user, contact_id)
    link = await wa_link(user['company_id'], c)
    if not link: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro (Empresa → WhatsApp).')
    first = (c.get('name') or '').lstrip('@').split(' ')[0]
    return {'link': link, 'text': f'Perfeito{", " + first if first and not _is_placeholder(c.get("name"), c) else ""}! Me chama no WhatsApp por este link que eu continuo com você por lá: {link}'}

IG_REF = re.compile(r'Vim do Instagram \(@([A-Za-z0-9._]{1,30})\)', re.I)

async def claim_from_instagram(cid, wa_id, text):
    """Mensagem do link wa.me ("Vim do Instagram (@fulano)"): o WhatsApp entra na ficha do Instagram em vez de criar outra."""
    m = IG_REF.search(text or '')
    if not m: return None
    c = await db.contacts.find_one({'company_id': cid, 'ig_username': m.group(1), 'wa_id': None}, {'_id': 0}) \
        or await db.contacts.find_one({'company_id': cid, 'ig_username': m.group(1), 'wa_id': {'$exists': False}}, {'_id': 0})
    if not c: return None
    await db.contacts.update_one({'id': c['id']}, {'$set': {'wa_id': wa_id}, '$unset': {'phone_hint': ''}})
    from whatsapp import note
    await note(cid, c['id'], f'{c["name"].split(" ")[0]} chamou no WhatsApp pelo link do Instagram. A conversa continua aqui.')
    return {**c, 'wa_id': wa_id}

def nanda_block(contact, link):
    """Para a Nanda: quem está no Direct e quer seguir no WhatsApp recebe o link."""
    if not contact or not link or contact.get('wa_id') or contact.get('channel') != 'instagram': return ''
    return ('\nWHATSAPP DA EMPRESA: se o cliente pedir para continuar no WhatsApp, mandar o número dele ou perguntar o WhatsApp de vocês, '
            f'responda que pode continuar por lá e mande exatamente este link para ele chamar: {link}\n')
