"""Campanhas guiadas: a Nanda escreve, regras de conselho, imagem no topo, espera a aprovação da Meta, agenda,
vagas do serviço, chama de novo quem pediu e não marcou, e o resultado por pessoa."""
import os, json, hmac, hashlib, time, io
from datetime import timedelta, datetime, timezone
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, automations, campaigns, metacost, media
from core import db, now
from main import app

PN = '5550011'
def inbound(c, wa, name, text, kind='text'):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': kind}
    if kind == 'button': m['button'] = {'text': text, 'payload': text}
    else: m['text'] = {'body': text}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': PN}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def jpg(w=1600, h=1200):
    from PIL import Image
    b = io.BytesIO(); Image.new('RGB', (w, h), (230, 120, 90)).save(b, 'JPEG'); return b.getvalue()

def test_campanha_guiada(monkeypatch, tmp_path):
    sent, tpls = [], []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'g%d' % len(sent)}]}
        if path.endswith('/message_templates') and method == 'POST': tpls.append(payload); return {'id': 't%d' % len(tpls), 'status': 'PENDING', 'category': payload['category']}
        if params and 'messaging_limit_tier' in (params.get('fields') or ''): return {'quality_rating': 'GREEN', 'messaging_limit_tier': 'TIER_1K'}
        return {'success': True, 'display_phone_number': '+55'}
    async def fake_handle(token, data, mime): return 'h:123'
    async def fake_upload(pn, token, data, mime, fn): return 'media-1'
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setattr(media, 'meta_upload_handle', fake_handle)
    monkeypatch.setattr(media, 'upload', fake_upload)
    monkeypatch.setenv('MEDIA_DIR', str(tmp_path))
    monkeypatch.setattr(automations, 'in_quiet', lambda company: False)
    monkeypatch.setattr(metacost, 'HEALTH_TTL', timedelta(0))
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona G', 'email': 'g@x.com', 'password': 'senha-forte-1', 'company': 'Sorriso G', 'segment': 'clinica'})
        c.post('/api/wa/connect', json={'phone_number_id': PN, 'waba_id': '5550012', 'token': 'EAA' + 'q' * 30})
        for i in range(5): inbound(c, f'55119400{i:05d}', f'Pessoa {i}', 'oi')
        # a Nanda escreve (sem IA: texto pronto por objetivo)
        w = c.post('/api/campaigns/write', json={'goal': 'agendar', 'service': 'Limpeza', 'offer': 'R$ 99 com 40% de desconto'}).json()
        assert '{primeiro_nome}' in w['text'] and w['buttons'] == ['Quero agendar', 'Parar promoções']
        # dentista: preço e desconto bloqueiam, com sugestão segura
        assert c.put('/api/company/practice', json={'practice': 'odonto'}).status_code == 200
        ck = c.post('/api/campaigns/check', json={'text': 'Oi, {primeiro_nome}! Limpeza por R$ 99, 40% de desconto. Quer?', 'goal': 'agendar', 'service': 'Limpeza'}).json()
        assert ck['block'] and 'Odontológica' in ck['issues'][0]['reason'] and '{primeiro_nome}' in ck['suggestion']
        w2 = c.post('/api/campaigns/write', json={'goal': 'agendar', 'service': 'Limpeza', 'offer': 'R$ 99 com 40% de desconto'}).json()
        assert 'R$' not in w2['text'] and '%' not in w2['text']
        r = c.post('/api/campaigns', json={'name': 'Limpeza dezembro', 'text': 'Oi, {primeiro_nome}! Limpeza por R$ 99. Quer agendar?', 'goal': 'agendar'})
        assert r.status_code == 400 and 'Odontológica' in r.json()['detail']
        # estética: pode falar de preço
        c.put('/api/company/practice', json={'practice': 'estetica'})
        # imagem: recortada no tamanho do WhatsApp
        im = c.post('/api/campaigns/image', files={'file': ('foto.jpg', jpg(), 'image/jpeg')}, data={'x': '0', 'y': '0.1', 'w': '1', 'h': '0.6'}).json()
        assert im['size'] == [1125, 590] and not im['warn']
        small = c.post('/api/campaigns/image', files={'file': ('p.jpg', jpg(420, 520), 'image/jpeg')}).json()
        assert small['warn'] and 'borrada' in small['warn'][0]
        assert c.get(im['url']).headers['content-type'] == 'image/jpeg'
        # agenda: serviço e vagas
        s = c.get('/api/agenda/settings').json()
        sv = {**s['services'][0], 'name': 'Limpeza de pele', 'duration': 60}
        c.put('/api/agenda/settings', json={'services': [sv]})
        cap = c.get('/api/campaigns/capacity', params={'service_id': sv['id']}).json()
        assert cap['service']['name'] == 'Limpeza de pele' and cap['total'] > 0 and cap['days']
        # alcance e custo máximo
        rc = c.post('/api/campaigns/reach', json={'rules': []}).json()
        assert rc['reach'] == 5 and rc['cost_max'] == round(5 * rc['per'], 2)
        # cria: modelo novo com imagem e botões, espera a Meta aprovar
        camp = c.post('/api/campaigns', json={'name': 'Limpeza dezembro', 'text': 'Oi, {primeiro_nome}! Faz {tempo_sem_vir} da sua última limpeza. Em dezembro sai por R$ 149. Quer agendar?',
                                              'buttons': ['Quero agendar', 'Parar promoções'], 'image_id': im['id'], 'goal': 'agendar', 'service_id': sv['id'],
                                              'after': {'nudge_hours': 2, 'remind_days': 3}}).json()
        assert camp['status'] == 'waiting_approval' and camp['remind_template_id']
        mt = tpls[0]
        assert mt['components'][0] == {'type': 'HEADER', 'format': 'IMAGE', 'example': {'header_handle': ['h:123']}}
        assert any(x['type'] == 'BUTTONS' for x in mt['components']) and not any(x['type'] == 'FOOTER' for x in mt['components'])  # botão "Parar promoções" substitui o rodapé
        assert c.portal.call(lambda: campaigns.tick(limit=100)) == 0
        c.portal.call(lambda: db.templates.update_many({'company_id': camp['company_id']}, {'$set': {'status': 'APPROVED'}}))
        assert c.portal.call(lambda: campaigns.tick(limit=100)) == 5
        tp = [p for p in sent if p.get('type') == 'template']
        assert tp[0]['template']['components'][0] == {'type': 'header', 'parameters': [{'type': 'image', 'image': {'id': 'media-1'}}]}
        assert c.get('/api/campaigns').json()[0]['status'] == 'done'
        # "Parar promoções" pelo botão: sai da lista
        inbound(c, '5511940000004', 'Pessoa 4', 'Parar promoções', 'button')
        assert c.portal.call(lambda: db.contacts.find_one({'wa_id': '5511940000004'}))['opt_out']
        # tocou em "Quero agendar" e não marcou: a Nanda chama de novo depois de 2 h, com 2 horários
        inbound(c, '5511940000001', 'Pessoa 1', 'Quero agendar', 'button')
        async def back():
            ct = await db.contacts.find_one({'wa_id': '5511940000001'})
            old = now() - timedelta(hours=3)
            await db.contacts.update_one({'id': ct['id']}, {'$set': {'last_in_at': old, 'last_dir': 'in'}})
            await db.messages.update_many({'contact_id': ct['id'], 'dir': 'in', 'text': 'Quero agendar'}, {'$set': {'at': old}})
            await db.campaign_sends.update_many({'contact_id': ct['id']}, {'$set': {'at': old - timedelta(minutes=5)}})
        c.portal.call(back)
        n0 = len(sent)
        c.portal.call(campaigns.after_send)
        nud = [p for p in sent[n0:] if p.get('type') == 'text']
        assert nud and 'Ainda quer agendar' in nud[0]['text']['body']
        c.portal.call(campaigns.after_send)
        assert len([p for p in sent if p.get('type') == 'text']) == len(nud)  # uma vez só
        # resultado por pessoa
        d = c.get(f"/api/campaigns/{camp['id']}").json()
        st = {p['name']: p['state'] for p in d['people']}
        assert st.get('Pessoa 4') == 'saiu' and 'Pessoa 1' in st and d['image_url']
        # a Nanda sabe da campanha quando a pessoa responde
        ct = c.portal.call(lambda: db.contacts.find_one({'wa_id': '5511940000001'}, {'_id': 0}))
        co = c.portal.call(lambda: db.companies.find_one({'id': camp['company_id']}, {'_id': 0}))
        blk = c.portal.call(lambda: campaigns.nanda_block(co, ct))
        assert 'CAMPANHA' in blk and 'Limpeza de pele' in blk
        # advocacia: só aviso
        c.put('/api/company/practice', json={'practice': 'advocacia'})
        r = c.post('/api/campaigns', json={'name': 'Oferta', 'text': 'Oi, {primeiro_nome}! Consulta grátis este mês. Quer?', 'goal': 'vender'})
        assert r.status_code == 400 and 'OAB' in r.json()['detail']
