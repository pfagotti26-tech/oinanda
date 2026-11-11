import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif',
                  IG_APP_ID='111', IG_APP_SECRET='igsecreto', IG_CONFIG_ID='777', IG_VERIFY_TOKEN='igverif', APP_ORIGINS='https://app.test')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, instagram
from core import db, now, uid
from main import app
from channels import norm_phone, variants, phones_in
from tests.test_instagram import post, dm

IG = '17841400000000077'

def wa_in(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770001'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_telefone():
    assert norm_phone('11941999929') == '5511941999929'
    assert norm_phone('(11) 94199-9929') == '5511941999929'
    assert norm_phone('+55 11 94199-9929') == '5511941999929'
    assert norm_phone('011 94199 9929') == '5511941999929'
    assert norm_phone('12345') is None
    assert variants('5511941999929') == ['5511941999929', '551141999929']
    assert phones_in('meu zap é 11 94199-9929, me chama') == ['5511941999929']
    assert phones_in('custa 150 reais') == []

def setup(c, monkeypatch, sent):
    async def fake_ig(method, path, token=None, params=None, data=None, payload=None, base=None):
        if path.endswith('/messages'): sent.append(('ig', payload)); return {'message_id': 'igout' + str(len(sent))}
        if path.startswith('/5'): return {'name': 'Fernanda Fagotti', 'username': 'nanda.f'}
        return {'success': True}
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(('wa', payload)); return {'messages': [{'id': 'waout' + str(len(sent))}]}
        return {'success': True, 'display_phone_number': '+55 11 3333-4444'}
    monkeypatch.setattr(instagram, 'ig', fake_ig); monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setattr(whatsapp, 'ON_INBOUND', None)

def ig_account(c, cid):
    from whatsapp import encrypt
    c.portal.call(lambda: db.ig_accounts.insert_one({'company_id': cid, 'ig_id': IG, 'username': 'loja', 'status': 'connected', 'mode': 'page', 'token_enc': encrypt('pt')}))

def test_instagram_continua_no_whatsapp(monkeypatch):
    sent = []
    with TestClient(app, base_url='https://testserver') as c:
        setup(c, monkeypatch, sent)
        me = c.post('/api/auth/signup', json={'name': 'Dona C', 'email': 'canais@x.com', 'password': 'senha-forte-1', 'company': 'Loja C', 'segment': 'studio'}).json()
        cid = c.get('/api/auth/me').json()['user']['company_id']
        c.post('/api/wa/connect', json={'phone_number_id': '7770001', 'waba_id': '7770002', 'token': 'EAA' + 'q' * 30})
        ig_account(c, cid)
        post(c, dm('5001', 'Oi! meu whats é 11 94199-9929', ig_id=IG))
        conv = next(x for x in c.get('/api/conversations').json() if x.get('channel') == 'instagram')
        d = c.get(f'/api/conversations/{conv["id"]}').json()['contact']
        assert d['phone_hint'] == '5511941999929' and d['phone_hint_fmt'] == '(11) 94199-9929' and d['channels'] == ['instagram']
        # convite com link do WhatsApp da empresa, com o @ para cair na mesma ficha
        inv = c.get(f'/api/conversations/{conv["id"]}/wa-invite').json()
        assert inv['link'].startswith('https://wa.me/551133334444?text=') and 'nanda.f' in inv['link']
        # salva o número
        r = c.post(f'/api/conversations/{conv["id"]}/whatsapp', json={'phone': d['phone_hint']}).json()
        assert r == {'contact_id': conv['id'], 'merged': False, 'wa_id': '5511941999929'}
        d = c.get(f'/api/conversations/{conv["id"]}').json()['contact']
        assert d['channels'] == ['whatsapp', 'instagram'] and d['reply_channel'] == 'instagram' and not d.get('phone_hint')
        assert d['windows']['whatsapp']['open'] is False and d['windows']['instagram']['open'] is True
        # ela chama no WhatsApp (a Meta manda sem o 9): cai na mesma ficha e a resposta passa a sair pelo WhatsApp
        wa_in(c, '551141999929', 'Fernanda', 'Oi, sou eu')
        rows = c.get('/api/conversations').json()
        assert len(rows) == 1
        d = c.get(f'/api/conversations/{conv["id"]}').json()
        assert d['contact']['reply_channel'] == 'whatsapp' and d['contact']['channel'] == 'whatsapp' and d['contact']['window_open']
        assert d['contact']['name'] == 'Fernanda Fagotti'  # nome do Instagram fica
        assert [m['text'] for m in d['messages'] if m['dir'] == 'in'] == ['Oi! meu whats é 11 94199-9929', 'Oi, sou eu']
        assert c.post(f'/api/conversations/{conv["id"]}/send', json={'text': 'Oi Fernanda!'}).status_code == 200
        assert sent[-1][0] == 'wa' and sent[-1][1]['to'] == '551141999929'
        # o time escolhe responder pelo Instagram
        assert c.post(f'/api/conversations/{conv["id"]}/channel', json={'channel': 'instagram'}).status_code == 200
        c.post(f'/api/conversations/{conv["id"]}/send', json={'text': 'Te mandei no Direct também'})
        assert sent[-1][0] == 'ig' and sent[-1][1]['recipient'] == {'id': '5001'}

def test_juntar_com_ficha_do_whatsapp(monkeypatch):
    sent = []
    with TestClient(app, base_url='https://testserver') as c:
        setup(c, monkeypatch, sent)
        c.post('/api/auth/signup', json={'name': 'Dona J', 'email': 'juntar@x.com', 'password': 'senha-forte-1', 'company': 'Loja J', 'segment': 'studio'})
        cid = c.get('/api/auth/me').json()['user']['company_id']
        c.post('/api/wa/connect', json={'phone_number_id': '7770001', 'waba_id': '7770002', 'token': 'EAA' + 'q' * 30})
        ig_account(c, cid)
        wa_in(c, '5511988887777', 'Carla Lima', 'Oi pelo zap')
        old = c.get('/api/conversations').json()[0]
        c.post(f'/api/conversations/{old["id"]}/note', json={'text': 'cliente antiga'})
        post(c, dm('5002', 'Oi pelo Direct', ig_id=IG))
        ig = next(x for x in c.get('/api/conversations').json() if x.get('channel') == 'instagram')
        r = c.post(f'/api/conversations/{ig["id"]}/whatsapp', json={'phone': '(11) 98888-7777'}).json()
        assert r['merged'] and r['contact_id'] == ig['id']
        rows = c.get('/api/conversations').json()
        assert len(rows) == 1
        d = c.get(f'/api/conversations/{ig["id"]}').json()
        texts = [m['text'] for m in d['messages']]
        assert 'Oi pelo zap' in texts and 'Oi pelo Direct' in texts and 'cliente antiga' in texts
        assert d['contact']['wa_id'] == '5511988887777' and d['contact']['channels'] == ['whatsapp', 'instagram']

def test_link_do_whatsapp_identifica_pelo_arroba(monkeypatch):
    sent = []
    with TestClient(app, base_url='https://testserver') as c:
        setup(c, monkeypatch, sent)
        c.post('/api/auth/signup', json={'name': 'Dona L', 'email': 'link@x.com', 'password': 'senha-forte-1', 'company': 'Loja L', 'segment': 'studio'})
        cid = c.get('/api/auth/me').json()['user']['company_id']
        c.post('/api/wa/connect', json={'phone_number_id': '7770001', 'waba_id': '7770002', 'token': 'EAA' + 'q' * 30})
        ig_account(c, cid)
        post(c, dm('5003', 'Quero saber mais', ig_id=IG))
        ig = c.get('/api/conversations').json()[0]
        wa_in(c, '5521977776666', 'Fê', 'Oi! Vim do Instagram (@nanda.f)')
        rows = c.get('/api/conversations').json()
        assert len(rows) == 1 and rows[0]['id'] == ig['id'] and rows[0]['wa_id'] == '5521977776666'
