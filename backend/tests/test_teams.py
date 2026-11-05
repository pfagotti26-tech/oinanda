import os, json, hmac, hashlib, time
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai, teams
from core import db, now
from main import app

def inbound(c, wa, name, text):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def login(c, email):
    return c.post('/api/auth/login', json={'email': email, 'password': 'senha-forte-1'})

def test_equipes_e_transferencia(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona Time', 'email': 'dt@x.com', 'password': 'senha-forte-1', 'company': 'Studio Time', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770011', 'waba_id': '7770012', 'token': 'EAA' + 'q' * 30})
        ids = {}
        for nm, em in [('Ana Pré', 'ana@x.com'), ('João Closer', 'joao@x.com'), ('Bia Closer', 'bia@x.com'), ('Sol Suporte', 'sol@x.com')]:
            r = c.post('/api/team', json={'name': nm, 'email': em, 'role': 'agent'}).json()
            ids[nm.split()[0]] = r['member']['id']
            async def setpw(uid=r['member']['id']):
                from core import hash_password
                await db.users.update_one({'id': uid}, {'$set': {'password_hash': hash_password('senha-forte-1'), 'must_change_password': False}})
            c.portal.call(setpw)
        stages = c.get('/api/funnel/stages').json()
        neg = next(s for s in stages if s['name'] == 'Negociando')
        pre = c.post('/api/teams', json={'name': 'Pré-vendas', 'member_ids': [ids['Ana']], 'handoff_default': True}).json()
        ven = c.post('/api/teams', json={'name': 'Vendas', 'member_ids': [ids['João'], ids['Bia']], 'distribution': 'roundrobin', 'stages': [neg['id']], 'sla_minutes': 10}).json()
        sup = c.post('/api/teams', json={'name': 'Suporte', 'member_ids': [ids['Sol']], 'topics': 'cancelamento, reclamação'}).json()
        assert c.post('/api/teams', json={'name': 'vendas'}).status_code == 409
        assert c.post('/api/teams', json={'name': 'Outra', 'stages': [neg['id']]}).status_code == 409
        inbound(c, '5511911112222', 'Marina Alves', 'Quero saber dos planos')
        cid = c.get('/api/conversations').json()[0]['id']
        # pré-vendas (atendente) transfere para a equipe de Vendas: rodízio escolhe o João
        c.post('/api/auth/logout'); login(c, 'ana@x.com')
        assert c.post(f'/api/conversations/{cid}/transfer', json={'team_id': ven['id'], 'reason': 'x'}).status_code == 422
        r = c.post(f'/api/conversations/{cid}/transfer', json={'team_id': ven['id'], 'reason': 'Quer fechar o anual', 'stage_id': neg['id']}).json()
        assert r['assigned_to'] == ids['João'] and r['team_id'] == ven['id']
        assert c.get(f'/api/conversations/{cid}').status_code == 404  # deixou de ser da Ana
        # o João é avisado até abrir
        c.post('/api/auth/logout'); login(c, 'joao@x.com')
        cnt = c.get('/api/inbox/counts').json()
        assert cnt['has_team'] and cnt['transfers'][0]['transfer_by'] == 'Ana Pré' and cnt['transfers'][0]['transfer_reason'] == 'Quer fechar o anual'
        assert [x['id'] for x in c.get('/api/conversations', params={'filter': 'team'}).json()] == [cid]
        msgs = c.get(f'/api/conversations/{cid}').json()['messages']
        assert any(m['dir'] == 'note' and 'Quer fechar o anual' in m['text'] for m in msgs) and any('transferiu para Vendas · João Closer' in m['text'] for m in msgs)
        assert c.get('/api/inbox/counts').json()['transfers'] == []
        # passa para uma pessoa específica (Bia)
        assert c.post(f'/api/conversations/{cid}/transfer', json={'user_id': ids['Bia'], 'reason': 'Bia cuida dos anuais'}).json()['assigned_to'] == ids['Bia']
        # regra por etapa: outro cliente entra em Negociando e vai para Vendas (rodízio: próxima é a Bia)
        c.post('/api/auth/logout'); login(c, 'dt@x.com')
        inbound(c, '5511933334444', 'Rafael Costa', 'Oi')
        rid = next(x['id'] for x in c.get('/api/conversations').json() if x['name'] == 'Rafael Costa')
        c.patch(f'/api/contacts/{rid}', json={'stage': neg['id']})
        rc = c.get(f'/api/conversations/{rid}').json()['contact']
        assert rc['team_id'] == ven['id'] and rc['assigned_to'] == ids['Bia']
        # Nanda passa para o time: assunto de cancelamento vai para Suporte; o resto para Pré-vendas
        inbound(c, '5511955556666', 'Lia Melo', 'quero o cancelamento do meu plano')
        lid = next(x for x in c.get('/api/conversations').json() if x['name'] == 'Lia Melo')
        async def ho(contact=lid): await ai.handoff(contact['company_id'], contact, 'cliente quer cancelamento')
        c.portal.call(ho)
        assert c.get(f'/api/conversations/{lid["id"]}').json()['contact']['team_id'] == sup['id']
        inbound(c, '5511977778888', 'Duda Reis', 'quero falar com alguém')
        did = next(x for x in c.get('/api/conversations').json() if x['name'] == 'Duda Reis')
        async def ho2(contact=did): await ai.handoff(contact['company_id'], contact, 'cliente pediu uma pessoa')
        c.portal.call(ho2)
        dc = c.get(f'/api/conversations/{did["id"]}').json()['contact']
        assert dc['team_id'] == pre['id'] and dc['assigned_to'] is None  # fila (sem rodízio)
        # alerta de fila: cliente esperando em Vendas há mais de 10 min
        async def old():
            await db.contacts.update_one({'id': rid}, {'$set': {'last_in_at': now() - timedelta(minutes=30), 'last_dir': 'in'}})
            return await teams.check_sla()
        assert c.portal.call(old) == 1 and c.portal.call(teams.check_sla) == 0
        rep = c.get('/api/reports/sales').json()
        tv = next(t for t in rep['teams'] if t['name'] == 'Vendas')
        assert tv['contacts'] == 2 and tv['members'] == 2
        assert 'tempo limite' in rep['alerts'][0]['text']
        tl = c.get('/api/teams').json()
        assert next(t for t in tl if t['name'] == 'Pré-vendas')['queue'] == 1
        assert c.delete(f'/api/teams/{sup["id"]}').json()['ok']
        assert c.get(f'/api/conversations/{lid["id"]}').json()['contact']['team_id'] is None
