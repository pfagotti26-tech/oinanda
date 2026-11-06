import os, json, hmac, hashlib, time
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai
from main import app

def inbound(c, wa, name, text, referral=None):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    if referral: m['referral'] = referral
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '6660011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_origem_links_e_cliente(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona O', 'email': 'do@x.com', 'password': 'senha-forte-1', 'company': 'Studio O', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        o = c.get('/api/origins').json()
        assert any(x['id'] == 'indicacao' for x in o['origins']) and o['ask_origin'] is True
        # link do evento com QR Code
        lk = c.post('/api/track-links', json={'label': 'Feira Fitness', 'origin': 'evento', 'detail': 'Feira Fitness 2026', 'message': 'Oi! Te conheci na feira'}).json()
        assert lk['wa'].startswith('https://wa.me/5511975754765?text=') and '%23oi-' + lk['code'] in lk['wa']
        r = c.get(f'/api/l/{lk["code"]}', follow_redirects=False)
        assert r.status_code == 302 and r.headers['location'] == lk['wa']
        assert c.get(f'/api/l/{lk["code"]}/qr.svg').text.startswith('<?xml')
        inbound(c, '5511911110000', 'Bia Feira', f'Oi! Te conheci na feira #oi-{lk["code"]}')
        inbound(c, '5511922220000', 'Leo Ads', 'Quero saber', referral={'source_type': 'ad', 'source_id': '123', 'headline': 'Aula grátis'})
        inbound(c, '5511933330000', 'Duda Sem', 'oi')
        cv = {x['name']: x for x in c.get('/api/conversations').json()}
        assert cv['Bia Feira']['origin'] == 'evento' and cv['Bia Feira']['origin_detail'] == 'Feira Fitness 2026' and cv['Bia Feira']['origin_by'] == 'link'
        assert cv['Leo Ads']['origin'] == 'anuncio' and cv['Leo Ads']['origin_detail'] == 'Aula grátis'
        assert c.get('/api/track-links').json()['items'][0]['contacts'] == 1 and c.get('/api/track-links').json()['items'][0]['clicks'] == 1
        # a Nanda pergunta para quem não tem origem e grava a resposta
        prompt = c.portal.call(lambda: ai.system_for({'name': 'Nanda', 'tone': 'proximo', 'goal': '', 'qualify': '', 'handoff': '', 'rules': '', 'about': '', 'offer': '', 'faq': '', 'knowledge': ''},
                                                    {'id': cv['Duda Sem']['company_id'], 'name': 'Studio O'}, cv['Duda Sem']))
        assert 'COMO O CLIENTE CONHECEU' in prompt and 'indicacao (Indicação)' in prompt
        assert 'COMO O CLIENTE CONHECEU' not in c.portal.call(lambda: ai.system_for({'name': 'Nanda', 'tone': 'proximo', 'goal': '', 'qualify': '', 'handoff': '', 'rules': '', 'about': '', 'offer': '', 'faq': '', 'knowledge': ''},
                                                    {'id': cv['Bia Feira']['company_id'], 'name': 'Studio O'}, cv['Bia Feira']))
        assert ai.parse('{"reply":"Que bom!","origin":"indicacao","origin_detail":"Carla"}')['origin'] == 'indicacao'
        # time marca indicação de outro contato
        assert c.put(f'/api/contacts/{cv["Duda Sem"]["id"]}/origin', json={'origin': 'indicacao', 'referred_by_id': cv['Bia Feira']['id']}).json()['ok']
        assert c.put(f'/api/contacts/{cv["Duda Sem"]["id"]}/origin', json={'origin': 'nao_existe'}).status_code == 400
        # prospect x cliente
        assert {x['name'] for x in c.get('/api/conversations', params={'kind': 'prospect'}).json()} == {'Bia Feira', 'Leo Ads', 'Duda Sem'}
        c.post('/api/sales', json={'contact_id': cv['Bia Feira']['id'], 'items': [{'name': 'Plano', 'price': 500}]})
        c.post('/api/sales', json={'contact_id': cv['Bia Feira']['id'], 'items': [{'name': 'Garrafa', 'price': 30}]})
        c.post('/api/sales', json={'contact_id': cv['Duda Sem']['id'], 'items': [{'name': 'Plano', 'price': 400}]})
        assert {x['name'] for x in c.get('/api/conversations', params={'kind': 'cliente'}).json()} == {'Bia Feira', 'Duda Sem'}
        assert [x['name'] for x in c.get('/api/conversations', params={'origin': 'anuncio'}).json()] == ['Leo Ads']
        # relatório de vendas por origem e primeira compra x recompra
        g = {x['label']: x['value'] for x in c.get('/api/sales/report', params={'group': 'source'}).json()['rows']}
        assert g == {'Evento': 530, 'Indicação': 400}
        k = {x['label']: x['value'] for x in c.get('/api/sales/report', params={'group': 'kind'}).json()['rows']}
        assert k == {'Primeira compra': 900, 'Recompra': 30}
        assert c.get('/api/sales/report', params={'kind': 'recompra'}).json()['totals']['value'] == 30
        assert c.get('/api/sales/report', params={'source': 'evento'}).json()['totals']['value'] == 530
        # relatório de origens: contatos, clientes, conversão e indicações
        rep = c.get('/api/origins/report').json()
        rows = {x['name']: x for x in rep['rows']}
        assert rows['Evento']['contacts'] == 1 and rows['Evento']['clients'] == 1 and rows['Evento']['value'] == 530
        assert rows['Anúncio (Meta)']['conversion'] == 0
        assert rep['referrals'][0]['name'] == 'Bia Feira' and rep['referrals'][0]['clients'] == 1
        # configurar origens: o automático do anúncio sempre fica
        cfg = c.put('/api/origins', json={'origins': [{'name': 'Google'}, {'name': 'Parceria com academia'}], 'inactive_days': 90}).json()
        assert [x['id'] for x in cfg['origins']] == ['anuncio', 'google', 'parceria_com_academia']
