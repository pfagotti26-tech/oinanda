"""Início: o que precisa do gestor; modelos pendentes conferidos sozinhos; conta oficial do Instagram fora da base."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from fastapi.testclient import TestClient
import whatsapp
from core import db, now, uid
from main import app
from tests.test_origins import inbound


def test_precisa_de_voce(monkeypatch):
    calls = []
    async def fake_graph(method, path, token, payload=None, params=None):
        calls.append(path)
        if path.endswith('/message_templates') and method == 'GET':
            return {'data': [{'name': 'retomada_72h', 'status': 'APPROVED', 'language': 'pt_BR', 'components': [{'type': 'BODY', 'text': 'Oi {{1}}'}]}]}
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona H', 'email': 'dh@x.com', 'password': 'senha-forte-1', 'company': 'Studio H', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511988880009', 'Robô Divulga', 'Conheça nosso curso https://exemplo.com/oferta-imperdivel')
        me = c.get('/api/auth/me').json(); cid = me['company']['id']
        async def seed():
            await db.ai_gaps.insert_one({'id': uid(), 'company_id': cid, 'q': 'Tem estacionamento?', 'key': 'x', 'count': 2, 'status': 'open', 'last_at': now()})
            t = {'id': uid(), 'company_id': cid, 'name': 'retomada_72h', 'language': 'pt_BR', 'status': 'PENDING', 'created_at': now() - timedelta(days=2)}
            await db.templates.insert_one(t)
            await db.automations.insert_one({'id': uid(), 'company_id': cid, 'name': 'Sem resposta há 3 dias', 'active': True, 'template_id': t['id'], 'created_at': now()})
        c.portal.call(seed)
        td = {x['k']: x for x in c.get('/api/home/todo').json()}
        assert set(td) == {'gaps', 'tpl_late', 'auto'} and 'Tem estacionamento?' in td['gaps']['sub']
        # o modelo pendente é conferido sozinho na Meta e a pendência some
        import automations
        c.portal.call(automations.sync_pending)
        assert 'tpl_late' not in {x['k'] for x in c.get('/api/home/todo').json()}
        c.portal.call(automations.sync_pending)  # conferido agora há pouco: não chama a Meta de novo
        assert sum(1 for p in calls if p.endswith('/message_templates')) == 1


def test_conta_oficial_do_instagram_fora_da_base(monkeypatch):
    import instagram
    async def prof(*_a, **_k): return {'username': 'instagram', 'name': 'Instagram'}
    monkeypatch.setattr(instagram, 'profile', prof)
    with TestClient(app, base_url='https://testserver') as tc:
        c, _ = tc.portal.call(instagram.upsert, {'company_id': 'co-ig', 'token_enc': ''}, '999')
    assert c['internal'] is True
