import os, json, hmac, hashlib, time, asyncio, base64
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient
import whatsapp, push, ai
from core import db
from main import app

def test_criptografia():
    ua = ec.generate_private_key(ec.SECP256R1()); ua_pub = push.raw_pub(ua.public_key()); secret = os.urandom(16)
    body = push.encrypt(b'{"title":"Oi"}', push.b64u(ua_pub), push.b64u(secret))
    # lado do navegador (RFC 8291)
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], 'big'), body[20]
    as_pub = body[21:21 + idlen]; ct = body[21 + idlen:]
    assert rs == 4096 and idlen == 65
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = HKDF(hashes.SHA256(), 32, secret, b'WebPush: info\x00' + ua_pub + as_pub).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b'Content-Encoding: aes128gcm\x00').derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b'Content-Encoding: nonce\x00').derive(ikm)
    assert AESGCM(cek).decrypt(nonce, ct, None) == b'{"title":"Oi"}\x02'
    # VAPID: assinatura confere com a chave pública
    k = ec.generate_private_key(ec.SECP256R1()); pub = push.b64u(push.raw_pub(k.public_key()))
    h = push.vapid_header(k, pub, 'https://fcm.googleapis.com/fcm/send/abc')
    t = h.split('t=')[1].split(',')[0]; a, b, sig = t.split('.')
    assert json.loads(push.unb64u(b))['aud'] == 'https://fcm.googleapis.com'
    raw = push.unb64u(sig)
    k.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')), f'{a}.{b}'.encode(), ec.ECDSA(hashes.SHA256()))

def test_avisos(monkeypatch):
    got = []
    async def fake_deliver(sub, data): got.append((sub['user_id'], data)); return sub['endpoint'] != 'https://x/morto'
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(push, 'deliver', fake_deliver); monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setattr(whatsapp, 'ON_INBOUND', None)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona P', 'email': 'dp@x.com', 'password': 'senha-forte-1', 'company': 'Loja P', 'segment': 'loja'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        assert len(c.get('/api/push/key').json()['public']) > 80
        assert c.post('/api/push/test').status_code == 400
        sub = {'endpoint': 'https://fcm.googleapis.com/fcm/send/1', 'p256dh': 'B' * 87, 'auth': 'a' * 22, 'device': 'Celular'}
        assert c.post('/api/push/subscribe', json=sub).json()['devices'] == 1
        c.post('/api/push/subscribe', json={**sub, 'endpoint': 'https://x/morto'})
        assert c.post('/api/push/test').json()['devices'] == 2
        assert c.portal.call(lambda: db.push_subs.count_documents({})) == 1  # aparelho que sumiu é apagado
        m = {'from': '5511970000001', 'id': 'w1', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'Oi, tem horário amanhã?'}}
        raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '6660011'}, 'contacts': [{'wa_id': '5511970000001', 'profile': {'name': 'Cli P'}}], 'messages': [m]}}]}]}).encode()
        got.clear()
        c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})
        c.portal.call(asyncio.sleep, 0.2)
        assert got and got[0][1]['title'] == 'Cli P' and got[0][1]['body'] == 'Oi, tem horário amanhã?' and got[0][1]['url'].startswith('/conversas/')
        # passou do tempo: avisa uma vez só
        async def velho(): await db.contacts.update_many({}, {'$set': {'last_in_at': whatsapp.now() - whatsapp.timedelta(minutes=40)}})
        c.portal.call(velho)
        async def semhorario():
            co = await db.companies.find_one({'name': 'Loja P'})
            await db.agenda_settings.update_one({'company_id': co['id']}, {'$set': {'company_id': co['id'], 'hours': {str(d): [['00:00', '23:59']] for d in range(7)}}}, upsert=True)
        c.portal.call(semhorario)
        got.clear()
        assert c.portal.call(push.late_tick) == 1 and 'espera resposta há' in got[0][1]['title']
        assert c.portal.call(push.late_tick) == 0
        c.post('/api/push/unsubscribe', json={'endpoint': sub['endpoint']})
        assert c.get('/api/push/key').json()['devices'] == 0
