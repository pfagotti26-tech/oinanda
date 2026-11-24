"""Verificação em duas etapas (código de aplicativo autenticador) para o admin da plataforma.
O admin entra na conta de qualquer cliente; só a senha não basta para isso."""
import base64, hashlib, hmac, secrets, struct, time
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import db, now, current_user, digest

router = APIRouter()
ISSUER = 'Oi Nanda'

def new_secret(): return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')

def _hotp(secret, counter):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    h = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    o = h[-1] & 15
    return '%06d' % ((struct.unpack('>I', h[o:o + 4])[0] & 0x7fffffff) % 1_000_000)

def check_totp(secret, code, last_step=None, at=None):
    """Aceita o código atual e o vizinho (relógio do celular um pouco adiantado ou atrasado). Devolve o passo usado, ou None."""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != 6: return None
    step = int((at or time.time()) // 30)
    for s in (step, step - 1, step + 1):
        if hmac.compare_digest(_hotp(secret, s), code) and (last_step is None or s > last_step): return s
    return None

def _enc(v):
    from whatsapp import encrypt
    return encrypt(v)
def _dec(v):
    from whatsapp import decrypt
    return decrypt(v)

async def verify_login(user, code):
    """Login do admin com 2 etapas ligadas: código do aplicativo ou um código de recuperação. True quando vale."""
    if not code: return False
    c = str(code).strip().replace(' ', '').replace('-', '').lower()
    if len(c) == 10:  # código de recuperação: vale uma vez
        h = digest('mfa-rec:' + c)
        r = await db.users.update_one({'id': user['id'], 'mfa_recovery': h}, {'$pull': {'mfa_recovery': h}})
        return bool(r.modified_count)
    st = check_totp(_dec(user['mfa_secret']), c, user.get('mfa_last_step'))
    if st is None: return False
    r = await db.users.update_one({'id': user['id'], 'mfa_last_step': user.get('mfa_last_step')}, {'$set': {'mfa_last_step': st}})
    return bool(r.modified_count)  # o mesmo código não vale duas vezes

async def admin_only(user=Depends(current_user)):
    if not user['is_admin']: raise HTTPException(403, 'Área exclusiva do admin do Oi Nanda.')
    return user

@router.get('/admin/2fa')
async def status(user=Depends(admin_only)):
    u = await db.users.find_one({'id': user['id']}, {'_id': 0, 'mfa_on': 1, 'mfa_recovery': 1})
    return {'on': bool(u.get('mfa_on')), 'ok': bool(user.get('mfa')), 'recovery_left': len(u.get('mfa_recovery') or [])}

@router.post('/admin/2fa/start')
async def start(user=Depends(admin_only)):
    u = await db.users.find_one({'id': user['id']}, {'_id': 0, 'mfa_on': 1})
    if u.get('mfa_on'): raise HTTPException(400, 'A verificação em duas etapas já está ligada.')
    sec = new_secret()
    await db.users.update_one({'id': user['id']}, {'$set': {'mfa_pending': _enc(sec)}})
    from urllib.parse import quote
    uri = f'otpauth://totp/{quote(ISSUER)}:{quote(user["email"])}?secret={sec}&issuer={quote(ISSUER)}&digits=6&period=30'
    import segno, io
    buf = io.BytesIO(); segno.make(uri, error='m').save(buf, kind='svg', scale=5, border=2, dark='#17212B', omitsize=True, xmldecl=False)
    return {'secret': ' '.join(sec[i:i + 4] for i in range(0, len(sec), 4)), 'uri': uri, 'qr': buf.getvalue().decode()}

class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=12)

@router.post('/admin/2fa/confirm')
async def confirm(body: CodeIn, request: Request, user=Depends(admin_only)):
    from core import audit
    u = await db.users.find_one({'id': user['id']}, {'_id': 0, 'mfa_pending': 1, 'mfa_on': 1})
    if u.get('mfa_on'): raise HTTPException(400, 'A verificação em duas etapas já está ligada.')
    if not u.get('mfa_pending'): raise HTTPException(400, 'Comece de novo: gere o QR code.')
    sec = _dec(u['mfa_pending']); st = check_totp(sec, body.code)
    if st is None: raise HTTPException(400, 'Código não confere. Confira a hora do celular e digite o código que está aparecendo agora.')
    codes = [secrets.token_hex(5) for _ in range(8)]
    await db.users.update_one({'id': user['id']}, {'$set': {'mfa_on': True, 'mfa_secret': u['mfa_pending'], 'mfa_last_step': st, 'mfa_at': now(),
                                                            'mfa_recovery': [digest('mfa-rec:' + c) for c in codes]}, '$unset': {'mfa_pending': 1}})
    await db.sessions.update_one({'token_hash': user['session_hash']}, {'$set': {'mfa': True}})
    await audit(request, user, 'Ligou a verificação em duas etapas do admin')
    return {'ok': True, 'recovery': [c[:5] + '-' + c[5:] for c in codes]}
