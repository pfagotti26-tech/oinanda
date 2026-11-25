"""Voz da Nanda (fase 1): quando o cliente manda áudio no WhatsApp, no Instagram ou no Messenger, a Nanda pode responder em áudio.

O texto da resposta vira voz na ElevenLabs (modelo rápido, português), é convertido para mensagem de voz do WhatsApp
(ogg/opus) e sai como áudio. A conta da ElevenLabs é do Oi Nanda (ELEVENLABS_API_KEY); cada plano tem uma franquia
de áudios por mês. Sem chave, sem franquia, resposta longa ou com link: a Nanda responde em texto, como sempre.
"""
import os, re, logging, httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from core import db, now, uid, current_user, managers

log = logging.getLogger('oinanda.voz')
router = APIRouter()
API = 'https://api.elevenlabs.io/v1'
MODEL = os.environ.get('ELEVENLABS_MODEL', 'eleven_flash_v2_5')
MAX_CHARS = 600            # resposta maior que isso vai em texto (áudio longo cansa e custa mais)
QUOTA = {'essencial': 100, 'profissional': 300, 'equipe': 1000, 'trial': 30}
# vozes em português do Brasil (biblioteca da ElevenLabs); a amostra é a prévia pública da própria voz, sem custo
VOICES = [
    {'id': 'GDzHdQOi6jjf8zaXhCYD', 'name': 'Raquel', 'g': 'f', 'desc': 'Jovem, simpática e clara', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/user/t1nBFVlw8ahi8WVJ0bXtyohGNAI2/voices/GDzHdQOi6jjf8zaXhCYD/LCt0ufE4oVO5zb1QbhsQ.mp3'},
    {'id': 'RGymW84CSmfVugnA5tvA', 'name': 'Roberta', 'g': 'f', 'desc': 'Leve e descontraída', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/workspace/d2d6d8fb79be40a2a3a1599d78da8346/voices/RGymW84CSmfVugnA5tvA/3vmILerGd3NQICCWM4Wa.mp3'},
    {'id': 'KHmfNHtEjHhLK9eER20w', 'name': 'Fernanda', 'g': 'f', 'desc': 'Acolhedora e profissional', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/user/y4fGgnCZeMQFBLjx1iroYxfYaWg1/voices/KHmfNHtEjHhLK9eER20w/yEntfRlJ2mSKw6OFC1PF.mp3'},
    {'id': 'GOkMqfyKMLVUcYfO2WbB', 'name': 'Jenifer', 'g': 'f', 'desc': 'Natural, com sotaque do interior', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/workspace/2df14998aae3469b884bde71c860576c/voices/GOkMqfyKMLVUcYfO2WbB/ufoI3jubdu5lI6iMW3Qp.mp3'},
    {'id': 'lvkgCBi6spByiTZMPJEK', 'name': 'Gabriel', 'g': 'm', 'desc': 'Calmo e simpático', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/workspace/de492322bd704062958833a36075b9f6/voices/lvkgCBi6spByiTZMPJEK/LTIeFq0rp5CdmNbV6DLO.mp3'},
    {'id': 'ec54d9BmuMSN4IinPrjv', 'name': 'Will', 'g': 'm', 'desc': 'Jovem e descontraído', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/workspace/1d8116e2c9c14d50affe4a07bc647489/voices/ec54d9BmuMSN4IinPrjv/anNpu2Y1E3cONfvgq6ao.mp3'},
    {'id': '4J31DrhygVjvFsoj7BsM', 'name': 'Eduardo', 'g': 'm', 'desc': 'Claro e seguro, sotaque do Sul', 'preview': 'https://storage.googleapis.com/eleven-public-prod/database/workspace/a036f1e2fd4249e9b7c6b0fefe1cbfd7/voices/4J31DrhygVjvFsoj7BsM/W8N1C6aNQPzPZIGnQriX.mp3'},
]
DEFAULT = VOICES[0]['id']

def ready(): return bool(os.environ.get('ELEVENLABS_API_KEY'))
def month(): return now().strftime('%Y-%m')

async def quota_of(company):
    from billing import plan_of
    state, plan = plan_of(company)
    q = QUOTA['trial'] if state == 'trial' else QUOTA.get(plan, 0) if state == 'active' else 0
    return q + int((company.get('voice_extra') or {}).get(month(), 0))

async def used(company_id):
    r = await db.voice_usage.find_one({'company_id': company_id, 'month': month()}, {'_id': 0}) or {}
    return int(r.get('count', 0))

async def tts(text, voice_id):
    """Texto vira áudio (mp3). Isolado para os testes trocarem."""
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f'{API}/text-to-speech/{voice_id}', params={'output_format': 'mp3_44100_64'},
                         headers={'xi-api-key': os.environ['ELEVENLABS_API_KEY']},
                         json={'text': text, 'model_id': MODEL, 'language_code': 'pt'})
    if r.status_code >= 400:
        log.warning('ElevenLabs recusou: %s %s', r.status_code, r.text[:200])
        global LAST_ERR; LAST_ERR = why(r); return None
    return r.content

LAST_ERR = ''
def why(r):
    """Motivo da recusa em português, para o painel."""
    try: d = r.json().get('detail') or {}
    except ValueError: d = {}
    st = (d.get('status') or '') if isinstance(d, dict) else ''
    msg = (d.get('message') or '') if isinstance(d, dict) else str(d)
    if r.status_code == 401 or 'api_key' in st or 'permission' in st: return 'A chave da ElevenLabs não tem permissão para gerar áudio.'
    if 'quota' in st or 'credits' in msg.lower(): return 'Os créditos da ElevenLabs acabaram.'
    if 'voice' in st or 'library' in msg.lower() or 'paid' in msg.lower() or 'subscription' in msg.lower():
        return 'A ElevenLabs não liberou essa voz pela API. As vozes da biblioteca exigem plano pago (Starter ou acima).'
    return f'A ElevenLabs recusou ({r.status_code}). {msg[:120]}'.strip()

def speakable(text):
    """Tira o que não se fala (emoji, markdown). Link ou texto longo: melhor em texto."""
    if re.search(r'https?://|www\.', text) or len(text) > MAX_CHARS: return None
    t = re.sub(r'[*_~`]', '', text)
    t = re.sub(r'[\U0001F000-\U0001FAFF☀-➿️]', '', t)
    return re.sub(r'\s+', ' ', t).strip() or None

async def reply(company_id, contact, text, by_name, trigger_id=None):
    """Responde em áudio se a empresa ligou a voz, o cliente mandou áudio e ainda há franquia. Senão, texto."""
    from whatsapp import deliver, note
    why_text = None
    try:
        msg = await _voice(company_id, contact, text, by_name, trigger_id)
        if msg: return msg
        why_text = WHY.pop(trigger_id, None) if trigger_id else None
    except Exception as e:
        log.exception('falha na resposta em áudio; vai em texto')
        det = getattr(e, 'detail', None) or f'{type(e).__name__}: {e}'
        why_text = 'o envio do áudio falhou (' + str(det).strip()[:220] + ')'
    out = await deliver(company_id, contact, text, None, by_name)
    if why_text:  # o cliente mandou áudio e a voz está ligada: o time vê por que foi em texto
        try: await note(company_id, contact['id'], f'A Nanda respondeu em texto em vez de áudio: {why_text}.')
        except Exception: pass
    return out

WHY = {}  # motivo de a resposta não ter saído em áudio, por mensagem do cliente

async def _voice(company_id, contact, text, by_name, trigger_id):
    from channels import view
    if not ready() or not trigger_id: return None
    c = view(contact)
    ch = c.get('channel') or 'whatsapp'
    if ch == 'whatsapp' and not c.get('wa_id'): return None
    if ch not in ('whatsapp', 'instagram', 'messenger'): return None
    s = await db.ai_settings.find_one({'company_id': company_id}, {'_id': 0, 'voice_reply': 1, 'voice_id': 1}) or {}
    if not s.get('voice_reply'): return None
    t = await db.messages.find_one({'id': trigger_id}, {'_id': 0, 'type': 1})
    if not t or t.get('type') != 'audio': return None
    say = speakable(text)
    if not say: WHY[trigger_id] = 'a resposta tem link ou é longa demais para áudio'; return None
    company = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if await used(company_id) >= await quota_of(company): WHY[trigger_id] = 'os áudios do mês acabaram'; return None
    data = await tts(say, s.get('voice_id') or DEFAULT)
    if not data: WHY[trigger_id] = (LAST_ERR or 'a ElevenLabs não gerou o áudio').rstrip('.'); return None
    if ch != 'whatsapp': return await _voice_meta(company_id, c, ch, text, say, data, by_name, trigger_id)
    import media, whatsapp
    data, mime = media.to_voice(data, 'audio/mpeg')
    n = await db.wa_numbers.find_one({'company_id': company_id}, {'_id': 0})
    if not n: return None
    token = whatsapp.decrypt(n['token_enc'])
    media_id = await media.upload(n['phone_number_id'], token, data, mime, 'audio.ogg')
    res = await whatsapp.graph('POST', f'/{n["phone_number_id"]}/messages', token, {'messaging_product': 'whatsapp', 'to': c['wa_id'], 'type': 'audio', 'audio': {'id': media_id}})
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': c['id'], 'dir': 'out', 'type': 'audio', 'voice': True, 'tts': True, 'text': text, 'mime': mime,
           'media_id': media_id, 'size': len(data), 'wa_msg_id': ((res.get('messages') or [{}])[0]).get('id'), 'status': 'sent', 'by_id': None, 'by': by_name, 'at': now()}
    p = media.path_for(msg); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); msg['file'] = p.name
    await db.messages.insert_one(msg.copy())
    await db.contacts.update_one({'id': c['id']}, {'$set': {'last_at': msg['at'], 'last_text': '🔊 ' + text[:190], 'last_dir': 'out', 'last_by': by_name}})
    await db.voice_usage.update_one({'company_id': company_id, 'month': month()}, {'$inc': {'count': 1, 'chars': len(say)}}, upsert=True)
    msg.pop('_id', None)
    return msg

async def _voice_meta(company_id, c, ch, text, say, data, by_name, trig=None):
    """Instagram e Messenger: o áudio vira m4a, fica num link assinado e a Meta baixa de lá. Falhou: a resposta vai em texto."""
    import media, instagram
    if not instagram.window(c)[0]: WHY[trig] = 'passou a janela de 24 horas do Instagram'; return None
    data, mime = media.to_m4a(data, 'audio/mpeg')
    msg = {'id': uid(), 'company_id': company_id, 'contact_id': c['id'], 'channel': ch, 'dir': 'out', 'type': 'audio', 'voice': True, 'tts': True, 'text': text, 'mime': mime,
           'size': len(data), 'status': 'sent', 'by_id': None, 'by': by_name, 'at': now()}
    p = media.path_for(msg); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); msg['file'] = p.name
    # a Meta baixa o arquivo pelo link durante o envio: a mensagem precisa existir antes, senão o link responde 404
    await db.messages.insert_one({**msg, 'status': 'sending'})
    try: res = await instagram.send(company_id, c, {'attachment': {'type': 'audio', 'payload': {'url': instagram.media_link(msg['id'])}}}, False, ch)
    except Exception:
        await db.messages.delete_one({'id': msg['id']}); raise
    msg['wa_msg_id'] = res.get('message_id')
    await db.messages.update_one({'id': msg['id']}, {'$set': {'status': 'sent', 'wa_msg_id': msg['wa_msg_id']}})
    await db.contacts.update_one({'id': c['id']}, {'$set': {'last_at': msg['at'], 'last_text': '🔊 ' + text[:190], 'last_dir': 'out', 'last_by': by_name}})
    await db.voice_usage.update_one({'company_id': company_id, 'month': month()}, {'$inc': {'count': 1, 'chars': len(say)}}, upsert=True)
    msg.pop('_id', None)
    return msg

# ---------- painel ----------
class VoiceIn(BaseModel):
    voice_reply: Optional[bool] = None
    voice_id: Optional[str] = None

from fastapi import Request
from fastapi.responses import Response

@router.post('/ai/voice/test')
async def test_voice(body: VoiceIn, user=Depends(current_user)):
    """Gera uma frase curta com a voz escolhida (pela API de verdade) para ouvir antes de ligar. Fica guardada: ouvir de novo não gasta."""
    if not ready(): raise HTTPException(400, 'A voz ainda não foi ativada no Oi Nanda.')
    vid = body.voice_id or DEFAULT
    if vid not in {v['id'] for v in VOICES}: raise HTTPException(400, 'Voz não encontrada.')
    s = await db.ai_settings.find_one({'company_id': user['company_id']}, {'_id': 0, 'name': 1}) or {}
    name = s.get('name') or 'Nanda'
    text = f'Oi! Eu sou a {name}, do {user["company"]["name"]}. Recebi seu áudio, já te ajudo. Quer que eu veja um horário para você?'
    import media, hashlib
    key = hashlib.sha1(f'{vid}|{text}'.encode()).hexdigest()[:20]
    p = media.media_dir() / 'voz-teste' / f'{key}.mp3'
    if p.exists(): return Response(p.read_bytes(), media_type='audio/mpeg')
    day = now().strftime('%Y-%m-%d')
    r = await db.voice_tests.find_one_and_update({'company_id': user['company_id'], 'day': day}, {'$inc': {'n': 1}}, upsert=True, return_document=True)
    if (r or {}).get('n', 1) > 15: raise HTTPException(429, 'Muitos testes hoje. Tente amanhã.')
    data = await tts(text, vid)
    if not data: raise HTTPException(400, LAST_ERR or 'A ElevenLabs não respondeu. Tente de novo.')
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    return Response(data, media_type='audio/mpeg')
@router.get('/ai/voice')
async def get_voice(user=Depends(current_user)):
    s = await db.ai_settings.find_one({'company_id': user['company_id']}, {'_id': 0, 'voice_reply': 1, 'voice_id': 1}) or {}
    return {'ready': ready(), 'on': bool(s.get('voice_reply')), 'voice_id': s.get('voice_id') or DEFAULT, 'voices': VOICES,
            'used': await used(user['company_id']), 'quota': await quota_of(user['company'])}

@router.put('/ai/voice')
async def put_voice(body: VoiceIn, user=Depends(managers)):
    upd = {}
    if body.voice_reply is not None: upd['voice_reply'] = body.voice_reply
    if body.voice_id is not None:
        if body.voice_id not in {v['id'] for v in VOICES}: raise HTTPException(400, 'Voz não encontrada.')
        upd['voice_id'] = body.voice_id
    if upd: await db.ai_settings.update_one({'company_id': user['company_id']}, {'$set': upd}, upsert=True)
    return await get_voice(user)
