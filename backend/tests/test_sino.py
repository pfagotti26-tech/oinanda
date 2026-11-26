import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from core import db, now, uid
from main import app

def test_sino_de_avisos():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Ana Dona', 'email': 'ana@sino.com', 'password': 'senha-forte-1', 'company': 'Clínica Ana', 'segment': 'clinica'})
        me = c.get('/api/auth/me').json(); cid, uid_ = me['company']['id'], me['user']['id']
        r = c.get('/api/alerts').json(); assert r['items'] == [] and r['unread'] == len(r['news'])
        k1, k2 = uid(), uid()
        async def put():
            await db.contacts.insert_many([{'id': k1, 'company_id': cid, 'name': 'Mariana Lopes', 'needs_human': True, 'last_in_at': now(), 'created_at': now()},
                                           {'id': k2, 'company_id': cid, 'name': 'Júlia Ramos', 'assigned_to': uid_, 'transfer_seen': False, 'transfer_by': 'Fernanda Silva', 'transfer_at': now(), 'created_at': now()}])
            await db.messages.insert_one({'id': uid(), 'company_id': cid, 'contact_id': k1, 'dir': 'in', 'text': 'Quero falar com alguém', 'at': now()})
            await db.ai_gaps.insert_one({'id': uid(), 'company_id': cid, 'status': 'open', 'q': 'Tem estacionamento?', 'count': 1, 'last_at': now()})
            await db.sales.insert_one({'id': uid(), 'company_id': cid, 'status': 'ok', 'total': 180, 'contact_name': 'Carla', 'items': [{'name': 'Limpeza de pele'}], 'created_by_name': 'Nanda (IA)', 'created_at': now()})
        c.portal.call(put)
        r = c.get('/api/alerts').json()
        t = [x['title'] for x in r['items']]
        assert 'Mariana pediu para falar com uma pessoa' in t and 'Fernanda passou uma conversa para você' in t
        assert any('não soube responder 1 pergunta' in x for x in t) and 'Venda registrada pela Nanda' in t
        assert r['unread'] == 4 + len(r['news']) and r['items'][-1]['tone'] == 'sale' and not r['items'][-1]['act']
        assert c.post('/api/alerts/seen').json()['unread'] == 0
        assert c.get('/api/alerts').json()['unread'] == 0
