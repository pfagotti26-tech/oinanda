"""Relatórios contam as vendas registradas (como a página Vendas); funil não soma compra feita como negociação; guia conforme o plano."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from fastapi.testclient import TestClient
from core import db, now
from main import app
import asyncio


def test_relatorio_conta_venda_mesmo_depois_de_agendar():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona N', 'email': 'dn@x.com', 'password': 'senha-forte-1', 'company': 'Studio N', 'segment': 'salao'})
        k = c.post('/api/contacts', json={'name': 'Carla', 'phone': '11977776666'}).json()
        assert c.post('/api/sales', json={'contact_id': k['id'], 'items': [{'name': 'Escova', 'qty': 1, 'price': 80}], 'payment': 'pix'}).status_code == 200
        sv = c.get('/api/agenda/settings').json()['services'][0]
        d = (now() + timedelta(days=2)).strftime('%Y-%m-%d')
        assert c.post('/api/appointments', json={'contact_id': k['id'], 'service_id': sv['id'], 'date': d, 'time': '10:00', 'force': True}).status_code == 200
        f = c.get('/api/funnel').json()
        card = next(x for x in f['cards'] if x['id'] == k['id'])
        kinds = {s['id']: s['kind'] for s in f['stages']}
        assert kinds[card['stage']] == 'open' and card['bought'] is True
        assert f['totals'][card['stage']]['value'] == 0  # já comprou: não é negociação
        r = c.get('/api/reports/sales?days=30').json()['totals']
        assert r['won'] == 1 and r['won_value'] == 80 and r['converted'] == 1
        v = c.get('/api/sales/report?start=%s&end=%s' % ((now() - timedelta(days=29)).strftime('%Y-%m-%d'), now().strftime('%Y-%m-%d'))).json()['totals']
        assert v['value'] == r['won_value'] and v['count'] == r['won']


def test_guia_demo_e_plano():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona G', 'email': 'dg@x.com', 'password': 'senha-forte-1', 'company': 'Studio G', 'segment': 'salao'})
        s = c.get('/api/setup').json()
        keys = [x['key'] for x in s['steps']]
        me = c.get('/api/auth/me').json()
        if me['access']['state'] == 'demo':
            assert keys[0] == 'ativar' and next(x for x in s['steps'] if x['key'] == 'time')['act'] == 'ativar'
        cid = me['company']['id']
        asyncio.run(db.companies.update_one({'id': cid}, {'$set': {'plan': 'profissional', 'billing': {'status': 'active', 'plan': 'profissional', 'paid_until': now() + timedelta(days=20)}}}))
        s = c.get('/api/setup').json(); keys = [x['key'] for x in s['steps']]
        assert 'meta' not in keys and 'ativar' not in keys
        asyncio.run(db.companies.update_one({'id': cid}, {'$set': {'plan': 'equipe', 'billing.plan': 'equipe'}}))
        assert 'meta' in [x['key'] for x in c.get('/api/setup').json()['steps']]
