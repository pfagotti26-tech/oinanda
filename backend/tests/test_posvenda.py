import os, json, hmac, hashlib, time
from datetime import timedelta, datetime
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai, automations, campaigns
from core import db
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '3330011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def ago(d): return (datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')
S = {'name': 'Nanda', 'tone': 'proximo', 'goal': '', 'qualify': '', 'handoff': '', 'rules': '', 'about': '', 'offer': '', 'faq': '', 'knowledge': ''}

def test_pos_venda(monkeypatch):
    sent = []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'o%d' % len(sent)}]}
        if path.endswith('/message_templates') and method == 'POST': return {'id': 't1', 'status': 'APPROVED', 'category': 'MARKETING'}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setattr(automations, 'in_quiet', lambda company: False)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona PV', 'email': 'pv@x.com', 'password': 'senha-forte-1', 'company': 'Escola PV', 'segment': 'escola'})
        c.post('/api/wa/connect', json={'phone_number_id': '3330011', 'waba_id': '3330012', 'token': 'EAA' + 'q' * 30})
        m2 = c.post('/api/products', json={'name': 'Módulo 2', 'price': 900, 'duration_days': 365}).json()
        mens = c.post('/api/products', json={'name': 'Mensalidade', 'price': 200, 'duration_days': 30, 'next_ids': [m2['id']]}).json()
        for wa, nm in [('5511920000001', 'Ana'), ('5511920000002', 'Beto'), ('5511920000003', 'Caio')]: inbound(c, wa, nm, 'oi')
        cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        sale = lambda who, prod, d: c.post('/api/sales', json={'contact_id': cv[who], 'date': ago(d), 'items': [{'product_id': prod['id'], 'name': prod['name'], 'price': prod['price']}]}).json()
        sale('Ana', mens, 25)   # vence em ~5 dias
        sale('Beto', mens, 40)  # venceu há ~10
        sale('Caio', mens, 70); sale('Caio', mens, 38)  # renovou e venceu há ~8 (somou no fim)
        # a Nanda sabe o que a Ana tem e o que oferecer
        ct = next(x for x in c.get('/api/conversations').json() if x['name'] == 'Ana')
        pr = c.portal.call(lambda: ai.system_for(S, {'id': ct['company_id'], 'name': 'Escola PV'}, ct))
        assert 'O QUE ESTE CLIENTE JÁ COMPROU: Mensalidade (até' in pr and 'renovação de Mensalidade' in pr and 'Módulo 2' in pr
        # automação: 7 dias antes de vencer, com cupom individual
        promo = c.post('/api/promos', json={'name': 'Renova 10', 'kind': 'percent', 'value': 10}).json()
        a = c.post('/api/automations', json={'name': 'Renovação', 'trigger': 'before_expiry', 'hours': 24 * 7, 'message': 'Oi {primeiro_nome}! Seu {produto} vence em {vencimento}. Use {cupom} e ganhe 10%.', 'coupon_promo_id': promo['id']}).json()
        async def win():  # cliente falou há pouco: sai como mensagem normal
            await db.contacts.update_many({}, {'$set': {'last_in_at': datetime.now().astimezone()}})
        c.portal.call(win)
        n = c.portal.call(automations.run_once)
        assert n == 1 and 'Seu Mensalidade vence em' in sent[-1]['text']['body'] and ' OI' in sent[-1]['text']['body']
        assert c.portal.call(automations.run_once) == 0  # não repete
        # depois de vencer sem renovar
        c.post('/api/automations', json={'name': 'Volta', 'trigger': 'after_expiry', 'hours': 24 * 8, 'message': 'Oi {primeiro_nome}, sentimos sua falta!'})
        assert c.portal.call(automations.run_once) == 2  # Beto (~10 dias) e Caio (~8 dias)
        # oportunidades de pós-venda a partir do público "venceu e não renovou"
        r = c.post('/api/opps/bulk', json={'rules': [{'field': 'expired_within', 'value': 30}], 'kind': 'renovacao', 'assign': 'none'}).json()
        assert r['created'] == 2
        assert c.post('/api/opps/bulk', json={'rules': [{'field': 'expired_within', 'value': 30}], 'kind': 'renovacao'}).json()['skipped'] == 2
        o = c.get('/api/opps').json()
        assert [s['name'] for s in o['stages']][0] == 'A contatar' and o['totals']['todo']['count'] == 2
        beto = next(x for x in o['items'] if x['contact_name'] == 'Beto')
        c.patch(f'/api/opps/{beto["id"]}', json={'stage_id': 'talking'})
        sale('Beto', mens, 0)  # renovou: card vai sozinho para "Renovou / comprou"
        assert next(x for x in c.get('/api/opps').json()['items'] if x['contact_name'] == 'Beto')['stage_id'] == 'won'
        # campanha com modelo aprovado e cupom individual
        tpl = c.post('/api/templates', json={'label': 'Volta com cupom', 'body': 'Oi {primeiro_nome}! Volta pra gente com o cupom {cupom}. Vale por 15 dias.'}).json()
        rules = [{'field': 'kind', 'value': 'cliente'}]
        pv = c.post('/api/campaigns/preview', json={'name': 'x', 'rules': rules, 'template_id': tpl['id']}).json()
        assert pv['reachable'] == 3 and pv['needs_coupon'] and pv['cost'] > 0
        assert c.post('/api/campaigns', json={'name': 'Volta', 'rules': rules, 'template_id': tpl['id']}).status_code == 400  # precisa da promoção
        camp = c.post('/api/campaigns', json={'name': 'Volta', 'rules': rules, 'template_id': tpl['id'], 'coupon_promo_id': promo['id']}).json()
        before = len(sent)
        assert c.portal.call(campaigns.tick) == 3
        assert len(sent) == before + 3 and all(p['type'] == 'template' for p in sent[-3:])
        codes = {p['template']['components'][0]['parameters'][1]['text'] for p in sent[-3:]}
        assert len(codes) == 3  # um cupom para cada um
        lc = c.get('/api/campaigns').json()[0]
        assert lc['status'] == 'done' and lc['sent'] == 3
        # relatório de renovação e upsell
        rr = c.get('/api/reports/retention', params={'start': ago(60), 'end': ago(0)}).json()
        assert rr['due'] >= 2 and rr['renewed'] >= 1 and rr['by_product'][0]['name'] == 'Mensalidade'
        assert rr['upsell'][0]['name'] == 'Mensalidade' and rr['upsell'][0]['converted'] == 0
        assert rr['opps_won'] == 1
