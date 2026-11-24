"""Atendente vê só as próprias vendas e tem guia próprio; quem paga antes do horário só vai para a etapa de venda depois do atendimento."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
import asyncio
from datetime import timedelta
from fastapi.testclient import TestClient
from core import db, now
from main import app


def test_pago_antes_do_horario_e_atendente():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona A', 'email': 'da@x.com', 'password': 'senha-forte-1', 'company': 'Studio A', 'segment': 'salao'})
        cid = c.get('/api/auth/me').json()['company']['id']
        asyncio.run(db.companies.update_one({'id': cid}, {'$set': {'plan': 'profissional', 'billing': {'status': 'active', 'plan': 'profissional', 'paid_until': now() + timedelta(days=20)}}}))
        k = c.post('/api/contacts', json={'name': 'Carla', 'phone': '11977776666'}).json()
        sv = c.get('/api/agenda/settings').json()['services'][0]
        d = (now() + timedelta(days=2)).strftime('%Y-%m-%d')
        ap = c.post('/api/appointments', json={'contact_id': k['id'], 'service_id': sv['id'], 'date': d, 'time': '10:00', 'force': True}).json()
        c.post('/api/sales', json={'contact_id': k['id'], 'items': [{'name': 'Escova', 'qty': 1, 'price': 80}], 'payment': 'pix'})
        f = c.get('/api/funnel').json(); kinds = {s['id']: s['kind'] for s in f['stages']}
        card = next(x for x in f['cards'] if x['id'] == k['id'])
        assert kinds[card['stage']] == 'open'  # pagou antes: segue em horário marcado
        c.patch(f'/api/appointments/{ap["id"]}', json={'status': 'done'})
        card = next(x for x in c.get('/api/funnel').json()['cards'] if x['id'] == k['id'])
        assert kinds[card['stage']] == 'won'
        # atendente
        inv = c.post('/api/team', json={'name': 'Ana', 'email': 'ana@x.com', 'role': 'agent'}).json()
        assert c.get('/api/funnel/summary').json()['won_value'] == 80
        with TestClient(app, base_url='https://testserver') as a:
            a.post('/api/auth/login', json={'email': 'ana@x.com', 'password': inv['temp_password']})
            a.post('/api/auth/password', json={'current_password': inv['temp_password'], 'new_password': 'Senha-da-Ana-1'})
            assert a.get('/api/funnel/summary').json()['won_value'] == 0  # a venda foi da dona
            s = a.get('/api/setup').json()
            assert s['agent'] and [x['key'] for x in s['steps']][:1] == ['push'] and 'responder' in [x['key'] for x in s['steps']]
            assert 'ativar' not in [x['key'] for x in s['steps']] and 'whatsapp' not in [x['key'] for x in s['steps']]
            k2 = a.post('/api/contacts', json={'name': 'Bia', 'phone': '11977775555'}).json()
            a.post('/api/sales', json={'contact_id': k2['id'], 'items': [{'name': 'Corte', 'qty': 1, 'price': 60}], 'payment': 'pix'})
            assert a.get('/api/funnel/summary').json()['won_value'] == 60
            assert next(x for x in a.get('/api/setup').json()['steps'] if x['key'] == 'venda')['done']
        assert c.get('/api/funnel/summary').json()['won_value'] == 140
