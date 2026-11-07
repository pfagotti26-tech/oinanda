"""Fotos, áudios, vídeos e arquivos nas conversas.

Recebido: o arquivo é baixado da Meta logo que chega (o link da Meta expira) e guardado no servidor. Áudio do cliente
é transcrito (se houver chave de transcrição) e só então a Nanda é chamada, para ela responder entendendo o áudio.
Enviado: o arquivo sobe para a Meta e vai para o cliente. Áudio gravado no navegador vira mensagem de voz (ogg/opus).
"""
import os, asyncio, logging, mimetypes, shutil, subprocess, tempfile
from pathlib import Path
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from core import db, now, uid, current_user
import whatsapp
from whatsapp import get_contact, can_see, aware, decrypt, WINDOW, GRAPH

router = APIRouter()
log = logging.getLogger('oinanda.media')
KINDS = ('image', 'video', 'audio', 'document', 'sticker')
LABEL = {'image': '📷 Foto', 'video': '🎬 Vídeo', 'audio': '🎤 Áudio', 'document': '📄 Documento', 'sticker': 'Figurinha'}
MAX = 16 * 1024 * 1024
SEND_TYPES = {
    'image/jpeg': 'image', 'image/png': 'image', 'video/mp4': 'video',
    'audio/ogg': 'audio', 'audio/mpeg': 'audio', 'audio/mp4': 'audio', 'audio/aac': 'audio', 'audio/amr': 'audio', 'audio/webm': 'audio', 'audio/x-m4a': 'audio',
    'application/pdf': 'document', 'text/plain': 'document', 'application/msword': 'document',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'document',
    'application/vnd.ms-excel': 'document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'document',
    'application/vnd.ms-powerpoint': 'document', 'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'document',
}
_tasks = set()

def media_dir(): return Path(os.environ.get('MEDIA_DIR', '/media'))

def meta_of(m):
    """Campos de mídia de uma mensagem da Meta, para guardar junto da mensagem."""
    t = m.get('type')
    if t not in KINDS: return {}
    x = m.get(t) or {}
    return {k: v for k, v in {'media_id': x.get('id'), 'mime': (x.get('mime_type') or '').split(';')[0] or None, 'filename': x.get('filename'),
                              'voice': x.get('voice'), 'caption': x.get('caption')}.items() if v}

def ext_of(mime, filename=None):
    if filename and '.' in filename: return '.' + filename.rsplit('.', 1)[1].lower()[:6]
    return {'audio/ogg': '.ogg', 'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'video/mp4': '.mp4',
            'application/pdf': '.pdf'}.get(mime or '') or mimetypes.guess_extension(mime or '') or '.bin'

def path_for(msg):
    return media_dir() / msg['company_id'] / (msg['id'] + ext_of(msg.get('mime'), msg.get('filename')))

# ---------- conversas com a Meta (isoladas para os testes) ----------
async def download(media_id, token):
    """Baixa o arquivo de uma mídia da Meta. Devolve (bytes, mime)."""
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(f'{GRAPH}/{media_id}', headers={'Authorization': 'Bearer ' + token})
        if r.status_code >= 400: raise HTTPException(400, 'A Meta não entregou o arquivo.')
        info = r.json()
        f = await c.get(info['url'], headers={'Authorization': 'Bearer ' + token})
        if f.status_code >= 400: raise HTTPException(400, 'A Meta não entregou o arquivo.')
        return f.content, (info.get('mime_type') or '').split(';')[0]

async def download_url(url):
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(url)
    if r.status_code >= 400: raise HTTPException(400, 'O arquivo não está mais disponível.')
    return r.content, (r.headers.get('content-type') or '').split(';')[0]

async def upload(phone_number_id, token, data, mime, filename):
    """Sobe um arquivo para a Meta. Devolve o id da mídia."""
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f'{GRAPH}/{phone_number_id}/media', headers={'Authorization': 'Bearer ' + token},
                         data={'messaging_product': 'whatsapp', 'type': mime}, files={'file': (filename, data, mime)})
    try: d = r.json()
    except ValueError: d = {}
    if r.status_code >= 400 or not d.get('id'):
        raise HTTPException(400, 'A Meta recusou o arquivo: ' + ((d.get('error') or {}).get('message') or f'erro {r.status_code}'))
    return d['id']

def transcribe_ready(): return bool(os.environ.get('TRANSCRIBE_API_KEY'))

async def transcribe(data, mime, filename):
    """Áudio vira texto. Serviço compatível com a API de transcrição da OpenAI (OpenAI, Groq...)."""
    key = os.environ.get('TRANSCRIBE_API_KEY')
    if not key: return None
    url = os.environ.get('TRANSCRIBE_URL') or 'https://api.openai.com/v1/audio/transcriptions'
    model = os.environ.get('TRANSCRIBE_MODEL') or 'gpt-4o-mini-transcribe'
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(url, headers={'Authorization': 'Bearer ' + key}, data={'model': model, 'language': 'pt', 'response_format': 'json'},
                         files={'file': (filename, data, mime or 'audio/ogg')})
    if r.status_code >= 400:
        log.warning('transcrição recusada: %s %s', r.status_code, r.text[:200]); return None
    return ((r.json() or {}).get('text') or '').strip() or None

def to_m4a(data, mime):
    """Instagram não aceita ogg/webm: o áudio vira m4a (AAC)."""
    if mime in ('audio/mp4', 'audio/aac', 'audio/x-m4a', 'audio/wav'): return data, mime
    if not shutil.which('ffmpeg'): raise HTTPException(400, 'Não foi possível converter o áudio neste servidor.')
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / 'in', Path(d) / 'out.m4a'
        src.write_bytes(data)
        p = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src), '-vn', '-ac', '1', '-c:a', 'aac', '-b:a', '64k', str(dst)], capture_output=True, timeout=60)
        if p.returncode != 0 or not dst.exists(): raise HTTPException(400, 'Não foi possível converter o áudio.')
        return dst.read_bytes(), 'audio/mp4'

def to_voice(data, mime):
    """Áudio gravado no navegador (webm, mp4...) vira ogg/opus, o formato de mensagem de voz do WhatsApp."""
    if mime == 'audio/ogg': return data, 'audio/ogg'
    if not shutil.which('ffmpeg'):
        if mime in ('audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/amr'): return data, mime
        raise HTTPException(400, 'Não foi possível converter o áudio neste servidor.')
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / 'in', Path(d) / 'out.ogg'
        src.write_bytes(data)
        p = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src), '-vn', '-ac', '1', '-ar', '48000', '-c:a', 'libopus', '-b:a', '32k', str(dst)],
                           capture_output=True, timeout=60)
        if p.returncode != 0 or not dst.exists(): raise HTTPException(400, 'Não foi possível converter o áudio.')
        return dst.read_bytes(), 'audio/ogg'

# ---------- recebidos ----------
async def fetch(msg):
    """Garante o arquivo no servidor. Devolve o caminho ou None."""
    p = path_for(msg)
    if p.exists(): return p
    if msg.get('media_url') and not msg.get('media_id'):  # Instagram: link direto (expira em alguns dias)
        data, mime = await download_url(msg['media_url'])
    else:
        if not msg.get('media_id'): return None
        n = await db.wa_numbers.find_one({'company_id': msg['company_id']}, {'_id': 0, 'token_enc': 1})
        if not n: return None
        data, mime = await download(msg['media_id'], decrypt(n['token_enc']))
    if len(data) > 100 * 1024 * 1024: return None
    if mime and not msg.get('mime'):
        msg['mime'] = mime; p = path_for(msg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    await db.messages.update_one({'id': msg['id']}, {'$set': {'file': p.name, 'size': len(data), 'mime': msg.get('mime')}})
    return p

async def process(msg_id, notify=True):
    """Chegou mídia: baixa, transcreve áudio e só então chama a Nanda."""
    msg = await db.messages.find_one({'id': msg_id}, {'_id': 0})
    if not msg: return
    try:
        p = await fetch(msg)
        if p and msg.get('type') == 'audio' and transcribe_ready():
            t = await transcribe(p.read_bytes(), msg.get('mime'), p.name)
            if t:
                await db.messages.update_one({'id': msg_id}, {'$set': {'transcript': t}})
                c = await db.contacts.find_one({'id': msg['contact_id']}, {'_id': 0, 'last_text': 1, 'last_at': 1})
                if c and c.get('last_text') == '🎤 Áudio' and aware(c.get('last_at')) == aware(msg['at']):
                    await db.contacts.update_one({'id': msg['contact_id']}, {'$set': {'last_text': ('🎤 ' + t)[:200]}})
    except Exception: log.exception('falha ao processar mídia')
    finally:
        await db.messages.update_one({'id': msg_id}, {'$set': {'pending': False}})
    if notify and msg.get('dir') == 'in' and whatsapp.ON_INBOUND and now() - aware(msg['at']) < whatsapp.timedelta(minutes=10):
        whatsapp.ON_INBOUND(msg['company_id'], msg['contact_id'], msg_id)

def schedule(msg_id, notify=True):
    t = asyncio.get_running_loop().create_task(process(msg_id, notify))
    _tasks.add(t); t.add_done_callback(_tasks.discard)

@router.get('/media/{msg_id}')
async def get_media(msg_id: str, user=Depends(current_user)):
    m = await db.messages.find_one({'id': msg_id, 'company_id': user['company_id']}, {'_id': 0})
    if not m or not (m.get('media_id') or m.get('file') or m.get('media_url')): raise HTTPException(404, 'Arquivo não encontrado.')
    c = await db.contacts.find_one({'id': m['contact_id']}, {'_id': 0})
    if not c or not can_see(user, c): raise HTTPException(404, 'Arquivo não encontrado.')
    p = await fetch(m)
    if not p: raise HTTPException(404, 'O arquivo não está mais disponível na Meta.')
    name = m.get('filename') or p.name
    return FileResponse(p, media_type=m.get('mime') or 'application/octet-stream', filename=name if m.get('type') == 'document' else None,
                        headers={'Cache-Control': 'private, max-age=86400'})

# ---------- enviados ----------
@router.post('/conversations/{contact_id}/media')
async def send_media(contact_id: str, file: UploadFile = File(...), caption: str = Form(default=''), voice: bool = Form(default=False), user=Depends(current_user)):
    from channels import view
    c = view(await get_contact(user, contact_id))
    li = aware(c.get('last_in_at'))
    if c.get('channel') != 'instagram' and (not li or now() - li >= WINDOW): raise HTTPException(400, 'Passaram 24 horas desde a última mensagem do cliente. Fora da janela, só modelo aprovado.')
    data = await file.read()
    if not data: raise HTTPException(400, 'Arquivo vazio.')
    if len(data) > MAX: raise HTTPException(400, 'Arquivo grande demais (máximo 16 MB).')
    mime = (file.content_type or '').split(';')[0].lower()
    if mime in ('', 'application/octet-stream'): mime = (mimetypes.guess_type(file.filename or '')[0] or '').lower()
    kind = SEND_TYPES.get(mime)
    if not kind: raise HTTPException(400, 'Tipo de arquivo não aceito pelo WhatsApp. Envie foto (JPG/PNG), vídeo MP4, áudio, PDF, Word, Excel ou PowerPoint.')
    if kind == 'image' and len(data) > 5 * 1024 * 1024: raise HTTPException(400, 'Foto grande demais (máximo 5 MB).')
    if c.get('channel') == 'instagram': return await send_ig(user, c, data, mime, kind, file.filename, caption)
    if kind == 'audio' and (voice or mime == 'audio/webm'): data, mime = to_voice(data, mime); voice = True
    n = await db.wa_numbers.find_one({'company_id': user['company_id']}, {'_id': 0})
    if not n: raise HTTPException(400, 'Conecte o WhatsApp da empresa primeiro.')
    token = decrypt(n['token_enc'])
    fname = (file.filename or 'arquivo').replace('/', '_')[:80]
    if voice: fname = 'audio.ogg'
    media_id = await upload(n['phone_number_id'], token, data, mime, fname)
    body = {'id': media_id}
    cap = caption.strip()[:1024]
    if cap and kind in ('image', 'video', 'document'): body['caption'] = cap
    if kind == 'document': body['filename'] = fname
    res = await whatsapp.graph('POST', f'/{n["phone_number_id"]}/messages', token, {'messaging_product': 'whatsapp', 'to': c['wa_id'], 'type': kind, kind: body})
    who = user.get('impersonated_by') or user['name']
    text = cap or LABEL[kind]
    msg = {'id': uid(), 'company_id': user['company_id'], 'contact_id': c['id'], 'dir': 'out', 'type': kind, 'text': text, 'mime': mime, 'media_id': media_id,
           'filename': fname if kind == 'document' else None, 'voice': voice or None, 'caption': cap or None, 'size': len(data),
           'wa_msg_id': ((res.get('messages') or [{}])[0]).get('id'), 'status': 'sent', 'by_id': user['id'], 'by': who, 'at': now()}
    p = path_for(msg); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); msg['file'] = p.name
    await db.messages.insert_one(msg.copy())
    upd = {'last_at': msg['at'], 'last_text': text[:200], 'last_dir': 'out'}
    if not c.get('assigned_to'): upd['assigned_to'] = user['id']
    if c.get('needs_human'): upd['needs_human'] = False
    await db.contacts.update_one({'id': c['id']}, {'$set': upd})
    if whatsapp.ON_HUMAN_SEND: await whatsapp.ON_HUMAN_SEND(user, c)
    msg.pop('_id', None)
    return msg

async def send_ig(user, c, data, mime, kind, filename, caption):
    import instagram
    normal, hum = instagram.window(c)
    if not hum: raise HTTPException(400, 'No Instagram só dá para responder até 7 dias depois da última mensagem do cliente.')
    if kind == 'audio': data, mime = to_m4a(data, mime)
    if kind == 'document' and mime != 'application/pdf': raise HTTPException(400, 'No Instagram só dá para mandar PDF como arquivo.')
    if kind == 'image' and len(data) > 8 * 1024 * 1024: raise HTTPException(400, 'Foto grande demais (máximo 8 MB).')
    who = user.get('impersonated_by') or user['name']
    fname = (filename or 'arquivo').replace('/', '_')[:80]
    msg = {'id': uid(), 'company_id': user['company_id'], 'contact_id': c['id'], 'channel': 'instagram', 'dir': 'out', 'type': kind, 'mime': mime, 'size': len(data),
           'filename': fname if kind == 'document' else None, 'text': LABEL[kind], 'status': 'sent', 'by_id': user['id'], 'by': who, 'at': now()}
    p = path_for(msg); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); msg['file'] = p.name
    res = await instagram.send(user['company_id'], c, {'attachment': {'type': {'document': 'file'}.get(kind, kind), 'payload': {'url': instagram.media_link(msg['id'])}}}, True)
    msg['wa_msg_id'] = res.get('message_id')
    await db.messages.insert_one(msg.copy())
    cap = (caption or '').strip()
    if cap: await instagram.deliver(user['company_id'], c, cap, user['id'], who)
    upd = {'last_at': now(), 'last_text': (cap or LABEL[kind])[:200], 'last_dir': 'out'}
    if not c.get('assigned_to'): upd['assigned_to'] = user['id']
    if c.get('needs_human'): upd['needs_human'] = False
    await db.contacts.update_one({'id': c['id']}, {'$set': upd})
    if whatsapp.ON_HUMAN_SEND: await whatsapp.ON_HUMAN_SEND(user, c)
    msg.pop('_id', None)
    return msg

@router.get('/media/pub/{msg_id}')
async def public_media(msg_id: str, e: str = '', s: str = ''):
    """Arquivo que mandamos pelo Instagram: a Meta baixa por este link assinado (vale 1 dia)."""
    import instagram
    if not instagram.link_ok(msg_id, e, s): raise HTTPException(404, 'Arquivo não encontrado.')
    m = await db.messages.find_one({'id': msg_id}, {'_id': 0})
    if not m or not m.get('file'): raise HTTPException(404, 'Arquivo não encontrado.')
    p = path_for(m)
    if not p.exists(): raise HTTPException(404, 'Arquivo não encontrado.')
    return FileResponse(p, media_type=m.get('mime') or 'application/octet-stream')

@router.get('/media-status')
async def status(user=Depends(current_user)):
    return {'transcribe': transcribe_ready(), 'convert': bool(shutil.which('ffmpeg'))}
