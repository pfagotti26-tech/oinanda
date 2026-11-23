import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from core import db, now, uid
import push, teams
from main import app

def test_equipe_vazia():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona Rosa', 'email': 'rosa@eq.com', 'password': 'senha-forte-1', 'company': 'Loja Rosa', 'segment': 'outro'})
        me = c.get('/api/auth/me').json(); cid = me['company']['id']
        c.portal.call(lambda: db.companies.update_one({'id': cid}, {'$set': {'plan': 'equipe', 'billing': {'status': 'active', 'plan': 'equipe', 'paid_until': now().replace(year=now().year + 1)}}}))
        ana = c.post('/api/team', json={'name': 'Ana Vendas', 'email': 'ana@eq.com', 'role': 'agent'}).json()['member']
        vazia = c.post('/api/teams', json={'name': 'Pré-vendas', 'handoff_default': True}).json()
        cheia = c.post('/api/teams', json={'name': 'Suporte', 'member_ids': [ana['id']], 'topics': 'troca'}).json()
        ct = {'id': uid(), 'company_id': cid, 'name': 'Lia', 'wa_id': '5511955550000', 'created_at': now()}
        c.portal.call(lambda: db.contacts.insert_one(ct.copy()))
        # repasse geral: a equipe padrão está vazia, então não vai para ela
        comp = c.portal.call(lambda: db.companies.find_one({'id': cid}, {'_id': 0}))
        c.portal.call(lambda: teams.route_handoff(comp, ct, 'quer falar com alguém'))
        assert c.portal.call(lambda: db.contacts.find_one({'id': ct['id']}, {'_id': 0})).get('team_id') is None
        # assunto bate com equipe com gente: vai
        c.portal.call(lambda: teams.route_handoff(comp, ct, 'quer fazer uma troca'))
        assert c.portal.call(lambda: db.contacts.find_one({'id': ct['id']}, {'_id': 0}))['team_id'] == cheia['id']
        # conversa numa equipe vazia: quem é avisado é a gerência
        r = c.portal.call(lambda: push.recipients(cid, {**ct, 'team_id': vazia['id']}))
        assert r == [me['user']['id']]
        # ana ausente na equipe dela: avisa a gerência também
        c.portal.call(lambda: db.users.update_one({'id': ana['id']}, {'$set': {'away': True}}))
        assert c.portal.call(lambda: push.recipients(cid, {**ct, 'team_id': cheia['id']})) == [me['user']['id']]
