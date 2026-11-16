"""Custo da Meta explicado e freios do número: limite diário, uma promoção por semana por pessoa,
pausa automática quando muita gente pede para sair, retomada só com quem conversou nos últimos 90 dias."""
import os, json, hmac, hashlib, time
from datetime import timedelta, datetime, timezone
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, automations, campaigns, metacost
from core import db
from main import app

PN = '4440011'
def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': PN}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_freios(monkeypatch):
    sent, state = [], {'tier': 'TIER_250', 'quality': 'GREEN'}
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'm%d' % len(sent)}]}
        if path.endswith('/message_templates') and method == 'POST':
            assert any(x['type'] == 'FOOTER' and 'SAIR' in x['text'] for x in payload['components'])  # promoção sempre ensina a sair
            return {'id': 't1', 'status': 'APPROVED', 'category': 'MARKETING'}
        if params and 'messaging_limit_tier' in (params.get('fields') or ''): return {'quality_rating': state['quality'], 'messaging_limit_tier': state['tier']}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    monkeypatch.setattr(automations, 'in_quiet', lambda company: False)
    monkeypatch.setattr(metacost, 'HEALTH_TTL', timedelta(0))
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona F', 'email': 'f@x.com', 'password': 'senha-forte-1', 'company': 'Clínica F', 'segment': 'clinica'})
        c.post('/api/wa/connect', json={'phone_number_id': PN, 'waba_id': '4440012', 'token': 'EAA' + 'q' * 30})
        # tabela de referência: aproximada, com data
        r = c.get('/api/meta/rates').json()
        assert r['MARKETING'] == 0.35 and r['checked_label'] and 'stale' not in r
        # 60 contatos
        for i in range(60): inbound(c, f'55119300{i:05d}', f'Pessoa {i}', 'oi')
        h = c.get('/api/meta/health').json()
        assert h['limit'] == 250 and h['quality'] == 'GREEN' and h['next'] == 2000
        tpl = c.post('/api/templates', json={'label': 'Promo', 'body': 'Oi {primeiro_nome}! Promoção da semana.'}).json()
        rules = []
        pv = c.post('/api/campaigns/preview', json={'rules': rules, 'template_id': tpl['id']}).json()
        assert pv['reachable'] == 60 and pv['per'] == 0.35 and pv['cost'] == 21.0 and pv['plan'] == [60] and pv['expected'] is None
        # limite diário: com o tier de 50 por dia, só sai 50 hoje
        state['tier'] = 'TIER_50'
        camp = c.post('/api/campaigns', json={'name': 'Promo 1', 'rules': rules, 'template_id': tpl['id']}).json()
        assert camp['category'] == 'MARKETING' and camp['quality_start'] == 'GREEN'
        assert c.portal.call(lambda: campaigns.tick(limit=100)) == 50
        assert c.portal.call(lambda: campaigns.tick(limit=100)) == 0  # espera o limite renovar
        lc = c.get('/api/campaigns').json()[0]
        assert lc['status'] == 'sending' and lc['queued'] == 10 and lc['waiting_limit'] and lc['cost_est'] == 17.5
        state['tier'] = 'TIER_250'
        assert c.portal.call(lambda: campaigns.tick(limit=100)) == 10
        assert c.get('/api/campaigns').json()[0]['status'] == 'done'
        # uma promoção por semana por pessoa: a segunda campanha não pega ninguém de novo
        pv2 = c.post('/api/campaigns/preview', json={'rules': rules, 'template_id': tpl['id']}).json()
        assert pv2['recent_promo'] == 60 and pv2['reachable'] == 0

        # freio: 4 de 40 pedem para sair (10%) → pausa e explica
        async def age_sends():
            await db.campaign_sends.update_many({}, {'$set': {'at': datetime.now(timezone.utc) - timedelta(days=8)}})
        c.portal.call(age_sends)
        camp2 = c.post('/api/campaigns', json={'name': 'Promo 2', 'rules': rules, 'template_id': tpl['id']}).json()
        assert c.portal.call(lambda: campaigns.tick(limit=40)) == 40
        for i in range(4): inbound(c, f'55119300{i:05d}', f'Pessoa {i}', 'sair')
        c.portal.call(lambda: campaigns.tick(limit=40))
        lc = next(x for x in c.get('/api/campaigns').json() if x['id'] == camp2['id'])
        assert lc['status'] == 'paused' and lc['brake']['reason'] == 'saida' and lc['opted'] == 4 and 'pediram para sair' in lc['brake']['text']
        # retomar amanhã só com quem conversou nos últimos 90 dias
        async def make_cold():
            ids = [s['contact_id'] for s in await db.campaign_sends.find({'campaign_id': camp2['id'], 'status': 'queued'}).to_list(100)][:5]
            await db.contacts.update_many({'id': {'$in': ids}}, {'$set': {'last_in_at': datetime.now(timezone.utc) - timedelta(days=200)}})
        c.portal.call(make_cold)
        rr = c.post(f'/api/campaigns/{camp2["id"]}/resume', json={'recent_days': 90, 'tomorrow': True}).json()
        assert rr['dropped'] == 5
        assert c.portal.call(lambda: campaigns.tick(limit=40)) == 0  # só amanhã
        lc = next(x for x in c.get('/api/campaigns').json() if x['id'] == camp2['id'])
        assert lc['status'] == 'sending' and lc['resume_at']

        # nota caiu na Meta → pausa
        async def now_resume():
            await db.campaigns.update_one({'id': camp2['id']}, {'$unset': {'resume_at': ''}})
            await db.contacts.update_many({}, {'$unset': {'opt_out_at': ''}})
        c.portal.call(now_resume)
        state['quality'] = 'YELLOW'
        c.portal.call(lambda: campaigns.tick(limit=40))
        lc = next(x for x in c.get('/api/campaigns').json() if x['id'] == camp2['id'])
        assert lc['status'] == 'paused' and lc['brake']['reason'] == 'qualidade'

        # admin atualiza a tabela; data muda
        assert c.put('/api/admin/meta-rates', json={'MARKETING': 0.4, 'UTILITY': 0.05}).status_code == 403
