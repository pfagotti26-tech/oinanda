"""Google Agenda: cada empresa conecta a própria conta Google (OAuth).

- Horários ocupados no Google bloqueiam a agenda do Oi Nanda (consulta de livre/ocupado).
- Agendamentos criados no Oi Nanda viram eventos no Google (e são atualizados/cancelados junto).

.env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET. Redirect: {APP_PUBLIC_URL}/api/google/callback
"""
import os, json, base64, time, logging
from datetime import timedelta
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse
from core import db, now, uid, audit, current_user, owners
from whatsapp import encrypt, decrypt, PUBLIC_URL

log = logging.getLogger('oinanda.google')
router = APIRouter()
SCOPES = 'openid email https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy'
API = 'https://www.googleapis.com/calendar/v3'
_tokens, _busy = {}, {}

def configured(): return bool(os.environ.get('GOOGLE_CLIENT_ID') and os.environ.get('GOOGLE_CLIENT_SECRET'))
def redirect_uri(): return PUBLIC_URL + '/api/google/callback'

async def http(method, url, token=None, data=None, json_body=None, params=None):
    """Chamada ao Google. Isolada para os testes substituírem."""
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, url, headers=headers, data=data, json=json_body, params=params)
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        raise HTTPException(502, 'Google recusou: ' + str((body.get('error') or {}).get('message') if isinstance(body.get('error'), dict) else body.get('error_description') or body.get('error') or r.status_code))
    return body

async def account(company_id):
    return await db.google_accounts.find_one({'company_id': company_id}, {'_id': 0})

async def access_token(company_id):
    hit = _tokens.get(company_id)
    if hit and hit[1] > time.time() + 60: return hit[0]
    acc = await account(company_id)
    if not acc: return None
    try:
        r = await http('POST', 'https://oauth2.googleapis.com/token', data={'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
                       'refresh_token': decrypt(acc['refresh_enc']), 'grant_type': 'refresh_token'})
    except Exception as e:
        await db.google_accounts.update_one({'company_id': company_id}, {'$set': {'error': str(getattr(e, 'detail', e))[:200]}})
        return None
    _tokens[company_id] = (r['access_token'], time.time() + int(r.get('expires_in', 3600)))
    if acc.get('error'): await db.google_accounts.update_one({'company_id': company_id}, {'$unset': {'error': 1}})
    return r['access_token']

async def busy(company_id, start, end):
    """Intervalos ocupados no Google entre start e end (UTC). Guarda por 60 s."""
    key = (company_id, start.date(), end.date())
    hit = _busy.get(key)
    if hit and hit[1] > time.time(): return hit[0]
    tok = await access_token(company_id)
    if not tok: return []
    acc = await account(company_id)
    try:
        r = await http('POST', f'{API}/freeBusy', tok, json_body={'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'items': [{'id': acc.get('calendar_id', 'primary')}]})
    except Exception:
        log.warning('falha ao ler livre/ocupado do Google'); return []
    from datetime import datetime
    out = [(datetime.fromisoformat(b['start'].replace('Z', '+00:00')), datetime.fromisoformat(b['end'].replace('Z', '+00:00')))
           for cal in (r.get('calendars') or {}).values() for b in cal.get('busy') or []]
    _busy[key] = (out, time.time() + 60)
    return out

def forget_busy(company_id):
    for k in [k for k in _busy if k[0] == company_id]: _busy.pop(k, None)

async def sync_event(appt):
    """Cria, atualiza ou cancela o evento do agendamento no Google. Nunca derruba o agendamento se o Google falhar."""
    try:
        tok = await access_token(appt['company_id'])
        if not tok: return
        acc = await account(appt['company_id']); cal = acc.get('calendar_id', 'primary')
        forget_busy(appt['company_id'])
        if appt.get('status') == 'canceled':
            if appt.get('google_event_id'):
                await http('DELETE', f'{API}/calendars/{cal}/events/{appt["google_event_id"]}', tok)
            return
        ev = {'summary': f'{appt["service_name"]} · {appt["contact_name"]}', 'start': {'dateTime': appt['start'].isoformat()}, 'end': {'dateTime': appt['end'].isoformat()},
              'description': f'WhatsApp: +{appt.get("contact_phone", "")}\n{appt.get("notes") or ""}\n\nAgendado pelo Oi Nanda ({appt.get("by") or ""}).'.strip()}
        if appt.get('google_event_id'):
            await http('PATCH', f'{API}/calendars/{cal}/events/{appt["google_event_id"]}', tok, json_body=ev)
        else:
            r = await http('POST', f'{API}/calendars/{cal}/events', tok, json_body=ev)
            await db.appointments.update_one({'id': appt['id']}, {'$set': {'google_event_id': r.get('id')}})
    except Exception:
        log.exception('falha ao sincronizar com o Google')

# ---------- conexão ----------
@router.get('/google/status')
async def status(user=Depends(current_user)):
    acc = await account(user['company_id'])
    return {'configured': configured(), 'connected': bool(acc), 'email': acc and acc.get('email'), 'error': acc and acc.get('error')}

@router.post('/google/connect')
async def connect(request: Request, user=Depends(owners)):
    if not configured(): raise HTTPException(400, 'A integração com o Google ainda não foi ativada no servidor.')
    state = uid() + uid()
    await db.oauth_states.insert_one({'state': state, 'company_id': user['company_id'], 'user_id': user['id'], 'expires_at': now() + timedelta(minutes=15)})
    q = {'client_id': os.environ['GOOGLE_CLIENT_ID'], 'redirect_uri': redirect_uri(), 'response_type': 'code', 'scope': SCOPES,
         'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'true', 'state': state}
    return {'url': 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(q)}

@router.get('/google/callback')
async def callback(request: Request, state: str = '', code: str = '', error: str = ''):
    st = await db.oauth_states.find_one_and_delete({'state': state}) if state else None
    if not st or st['expires_at'].replace(tzinfo=None) < now().replace(tzinfo=None):
        return RedirectResponse(PUBLIC_URL + '/?google=expirado')
    if error or not code: return RedirectResponse(PUBLIC_URL + '/?google=cancelado')
    try:
        r = await http('POST', 'https://oauth2.googleapis.com/token', data={'code': code, 'client_id': os.environ['GOOGLE_CLIENT_ID'],
                       'client_secret': os.environ['GOOGLE_CLIENT_SECRET'], 'redirect_uri': redirect_uri(), 'grant_type': 'authorization_code'})
    except Exception:
        log.exception('troca de código do Google falhou'); return RedirectResponse(PUBLIC_URL + '/?google=erro')
    if not r.get('refresh_token'): return RedirectResponse(PUBLIC_URL + '/?google=erro')
    email = None
    try:  # o id_token veio direto do Google pela conexão segura; só lemos o e-mail
        payload = r.get('id_token', '').split('.')[1]
        email = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('email')
    except Exception: pass
    await db.google_accounts.update_one({'company_id': st['company_id']}, {'$set': {'company_id': st['company_id'], 'email': email, 'calendar_id': 'primary',
        'refresh_enc': encrypt(r['refresh_token']), 'connected_at': now(), 'connected_by': st['user_id']}, '$unset': {'error': 1}}, upsert=True)
    _tokens[st['company_id']] = (r['access_token'], time.time() + int(r.get('expires_in', 3600)))
    user = await db.users.find_one({'id': st['user_id']}, {'_id': 0})
    await audit(request, user, 'Conectou o Google Agenda', email, company_id=st['company_id'])
    return RedirectResponse(PUBLIC_URL + '/?google=ok')

@router.delete('/google')
async def disconnect(request: Request, user=Depends(owners)):
    acc = await account(user['company_id'])
    if acc:
        try: await http('POST', 'https://oauth2.googleapis.com/revoke', data={'token': decrypt(acc['refresh_enc'])})
        except Exception: pass
        await db.google_accounts.delete_one({'company_id': user['company_id']})
        _tokens.pop(user['company_id'], None); forget_busy(user['company_id'])
        await audit(request, user, 'Desconectou o Google Agenda', acc.get('email'))
    return {'ok': True}
