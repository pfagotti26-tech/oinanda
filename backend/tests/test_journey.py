from zoneinfo import ZoneInfo
import os, json, hmac, hashlib, time, asyncio
from datetime import timedelta, datetime
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, journey, ai, automations
from core import db, now
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '3330011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_jornada(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona J', 'email': 'dj@x.com', 'password': 'senha-forte-1', 'company': 'Studio J', 'segment': 'studio'})
        c.put('/api/company/modules', json={'jornada': True})  # conta nova começa com a Jornada desligada
        c.post('/api/wa/connect', json={'phone_number_id': '3330011', 'waba_id': '3330012', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511940000001', 'Lead A', 'oi, quanto custa?')
        inbound(c, '5511940000002', 'Lead B', 'oi')
        cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        a = cv['Lead A']
        c.post(f'/api/conversations/{a}/send', json={'text': 'Oi! Te passo os valores.'})
        st = c.get('/api/journey/settings').json()
        ids = [m['id'] for m in st['milestones']]
        assert ids[0] == 'entrou' and 'compareceu' in ids and ids[-1] == 'comprou'
        stages = st['stages']
        c.patch(f'/api/contacts/{a}', json={'stage': stages[2]['id']})
        # marco manual com data retroativa e sugestão da Nanda
        assert c.post(f'/api/contacts/{a}/milestones', json={'milestone_id': 'orcamento', 'at': (now() - timedelta(hours=2)).isoformat()}).status_code == 200
        assert c.post(f'/api/contacts/{a}/milestones', json={'milestone_id': 'orcamento'}).status_code == 200  # pode repetir
        assert c.post(f'/api/contacts/{a}/milestones', json={'milestone_id': 'xx'}).status_code == 404
        company = {'id': c.portal.call(lambda: db.companies.find_one({'name': 'Studio J'}))['id']}
        async def sug():
            co = await db.companies.find_one({'id': company['id']}, {'_id': 0})
            await journey.suggest(co, {'id': a}, ['aceitou', 'nada'])
            await journey.suggest(co, {'id': a}, ['aceitou'])  # repetida em 12 h: ignora
        c.portal.call(sug)
        cm = c.get(f'/api/contacts/{a}/milestones').json()
        assert len(cm['suggestions']) == 1
        c.post(f"/api/journey/suggestions/{cm['suggestions'][0]['id']}/confirm")
        cm = c.get(f'/api/contacts/{a}/milestones').json()
        got = {m['id']: m['count'] for m in cm['milestones']}
        assert got['entrou'] == 1 and got['orcamento'] == 2 and got['aceitou'] == 1 and got['comprou'] == 0 and not cm['suggestions']
        today = datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d')
        c.post('/api/sales', json={'contact_id': a, 'date': today, 'items': [{'name': 'Plano', 'price': 300}]})
        j = c.get(f'/api/contacts/{a}/journey').json()
        titles = [e['title'] for e in j['items']]
        assert 'Entrou como contato' in titles and 'Mandou a primeira mensagem' in titles and any(t.startswith('Primeira resposta em') for t in titles)
        assert 'Comprou R$ 300' in titles and titles.count('Pediu orçamento') == 2
        assert any(m['id'] == 'comprou' for e in j['items'] for m in e.get('marcos') or []) and any(m['id'] == 'entrou' for e in j['items'] for m in e.get('marcos') or [])
        assert j['counts']['etapa'] >= 1 and not any('registrou uma venda' in t for t in titles)
        assert all(e['cat'] == 'venda' for e in c.get(f'/api/contacts/{a}/journey?cats=venda').json()['items'])
        # registro manual e desfazer
        r = c.post(f'/api/contacts/{a}/journey', json={'title': 'Visitou o studio', 'note': 'veio com a amiga'}).json()
        assert c.post(f'/api/contacts/{a}/journey', json={'title': 'Futuro', 'at': (now() + timedelta(days=2)).isoformat()}).status_code == 400
        c.post(f"/api/journey/{r['id']}/undo")
        e = next(x for x in c.get(f'/api/contacts/{a}/journey').json()['items'] if x['title'] == 'Visitou o studio')
        assert e['undone'] and e['undone_by'] == 'Dona J'
        # segmentos: aceitou e não comprou / pediu orçamento nos últimos 7 dias
        n = lambda rules: c.post('/api/segments/preview', json={'rules': rules}).json()['count']
        assert n([{'field': 'milestone', 'op': 'ever', 'value': {'id': 'orcamento'}}]) == 1
        assert n([{'field': 'milestone', 'op': 'never', 'value': {'id': 'orcamento'}}]) == 1
        assert n([{'field': 'milestone', 'op': 'within', 'value': {'id': 'orcamento', 'days': 7}}]) == 1
        assert n([{'field': 'milestone', 'op': 'more', 'value': {'id': 'orcamento', 'days': 7}}]) == 0
        # relatório
        rp = c.get('/api/reports/journey').json()
        assert rp['total'] == 2
        s = {x['id']: x for x in rp['steps']}
        assert s['entrou']['reached'] == 2 and s['orcamento']['reached'] == 1 and s['comprou']['reached'] == 1
        assert rp['by_origin'][0]['total'] == 2 and sum(x['count'] for x in rp['activity']) >= 5
        # personalizar: marco por etapa, Nanda exige explicação
        ms = st['milestones'] + [{'id': 'fezaula', 'name': 'Chegou na etapa', 'icon': '⭐', 'rule': {'type': 'stage', 'stage_id': stages[2]['id']}}]
        assert c.put('/api/journey/settings', json={'milestones': ms}).status_code == 200
        bad = ms + [{'name': 'Sem dica', 'nanda': True}]
        assert c.put('/api/journey/settings', json={'milestones': bad}).status_code == 400
        got = {m['id']: m['count'] for m in c.get(f'/api/contacts/{a}/milestones').json()['milestones']}
        assert got['fezaula'] == 1
        # Nanda: bloco no prompt e leitura dos marcos
        co = c.portal.call(lambda: db.companies.find_one({'id': company['id']}, {'_id': 0}))
        assert 'aceitou: Aceitou proposta' in journey.nanda_block(co)
        assert ai.parse('{"reply": "oi", "marcos": ["orcamento", 3]}')['marcos'] == ['orcamento', '3']
        assert ai.parse('{"reply": "oi"}')['marcos'] == []
        # automação depois de um marco, a não ser que compre
        au = c.post('/api/automations', json={'name': 'Retomar orçamento', 'trigger': 'after_milestone', 'milestone_id': 'orcamento', 'unless_id': 'comprou', 'hours': 1, 'message': 'Oi {primeiro_nome}, conseguiu ver os valores?'})
        assert au.status_code == 200, au.text
        assert c.post('/api/automations', json={'name': 'xx', 'trigger': 'after_milestone', 'hours': 1, 'message': 'Oi tudo bem?'}).status_code == 400
        b = cv['Lead B']
        c.post(f'/api/contacts/{b}/milestones', json={'milestone_id': 'orcamento', 'at': (now() - timedelta(hours=2)).isoformat()})
        async def cands():
            a_ = await db.automations.find_one({'id': au.json()['id']}, {'_id': 0}); co = await db.companies.find_one({'id': company['id']}, {'_id': 0})
            return await automations.candidates(a_, co, [])
        got = [x[1]['name'] for x in c.portal.call(cands)]
        assert got == ['Lead B']  # Lead A já comprou
