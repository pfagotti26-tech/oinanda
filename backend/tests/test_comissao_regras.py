"""Comissão com regras: padrão da pessoa, regra do item (% ou fixo), exceção por pessoa, plano que paga só nas N primeiras cobranças,
venda dividida, a pagar x pago, extrato e o atendente vendo só a dele."""
import os
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def test_comissao_regras():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dono', 'email': 'd@cr.com', 'password': 'senha-forte-1', 'company': 'Salão R', 'segment': 'salao'})
        f = c.post('/api/team', json={'name': 'Fernanda Corte', 'email': 'f@cr.com', 'role': 'agent'}).json()['member']
        b = c.post('/api/team', json={'name': 'Bianca Estetica', 'email': 'b@cr.com', 'role': 'agent'}).json()['member']
        assert c.put('/api/commission/people', json={'pct': {f['id']: 30, b['id']: 20}}).json()['ok']
        corte = c.post('/api/products', json={'name': 'Corte', 'price': 100}).json()
        prog = c.post('/api/products', json={'name': 'Progressiva', 'price': 400}).json()
        sh = c.post('/api/products', json={'name': 'Shampoo', 'price': 90, 'kind': 'produto'}).json()
        plano = c.post('/api/products', json={'name': 'Plano mensal', 'price': 280, 'kind': 'plano', 'duration_days': 30}).json()
        c.put(f"/api/commission/item/{prog['id']}", json={'mode': 'pct', 'value': 40})
        c.put(f"/api/commission/item/{sh['id']}", json={'mode': 'fixed', 'value': 8})
        c.put(f"/api/commission/item/{plano['id']}", json={'mode': 'pct', 'value': 10, 'renew': 'n', 'n': 2, 'exceptions': [{'user_id': b['id'], 'pct': 15}]})
        rl = c.get('/api/commission/rules').json()
        assert {p['id']: p.get('commission_pct') for p in rl['people']}[f['id']] == 30 and next(i for i in rl['items'] if i['id'] == sh['id'])['commission']['mode'] == 'fixed'
        ct = c.post('/api/contacts', json={'name': 'Marina', 'phone': '11911110000'}).json()
        from datetime import datetime
        from zoneinfo import ZoneInfo
        hoje = datetime.now(ZoneInfo('America/Sao_Paulo')).date()  # o "hoje" da empresa, não o do servidor (UTC)
        d = lambda n: (hoje - timedelta(days=n)).isoformat()
        S = lambda items, sellers, dd: c.post('/api/sales', json={'contact_id': ct['id'], 'date': dd, 'items': items, 'payment': 'pix', 'sellers': sellers}).json()
        S([{'product_id': corte['id'], 'name': 'Corte', 'qty': 1, 'price': 100}], [{'user_id': f['id'], 'pct': 100}], d(0))                       # 30% de 100 = 30
        S([{'product_id': prog['id'], 'name': 'Progressiva', 'qty': 1, 'price': 400, 'discount': 40}], [{'user_id': f['id'], 'pct': 100}], d(0))  # 40% de 360 = 144
        S([{'product_id': sh['id'], 'name': 'Shampoo', 'qty': 2, 'price': 90}], [{'user_id': f['id'], 'pct': 50}, {'user_id': b['id'], 'pct': 50}], d(0))  # 8*2*0.5 = 8 cada
        for n in (2, 1, 0):  # 3 cobranças do plano: paga só as 2 primeiras
            S([{'product_id': plano['id'], 'name': 'Plano mensal', 'qty': 1, 'price': 280}], [{'user_id': f['id'], 'pct': 100}], d(n))  # 28, 28, 0
        S([{'product_id': plano['id'], 'name': 'Plano mensal', 'qty': 1, 'price': 280}], [{'user_id': b['id'], 'pct': 100}], d(0))  # 3ª... já passou: 0 (conta por cliente+item)
        st = c.get(f"/api/commission/statement?user_id={f['id']}").json()
        byi = [(l['item'], l['commission'], l['rule']) for l in st['lines']]
        assert st['commission'] == 30 + 144 + 8 + 28 + 28, byi
        assert any('padrão de Fernanda' in r for _, _, r in byi) and any('regra do item · 40%' in r for _, _, r in byi) and any('já passou das 2' in r for _, _, r in byi)
        sm = c.get('/api/commission/summary').json()
        rf = next(r for r in sm['rows'] if r['user_id'] == f['id']); rb = next(r for r in sm['rows'] if r['user_id'] == b['id'])
        assert rf['due'] == 238 and rb['commission'] == 8 and rb['due'] == 8
        # exceção da Bianca vale quando ainda está dentro das 2 cobranças: outro cliente
        ct2 = c.post('/api/contacts', json={'name': 'Joana', 'phone': '11922220000'}).json()
        c.post('/api/sales', json={'contact_id': ct2['id'], 'date': d(0), 'items': [{'product_id': plano['id'], 'name': 'Plano mensal', 'qty': 1, 'price': 280}], 'payment': 'pix', 'sellers': [{'user_id': b['id'], 'pct': 100}]})
        stb = c.get(f"/api/commission/statement?user_id={b['id']}").json()
        assert stb['commission'] == 8 + 42 and any('exceção de Bianca · 15%' in l['rule'] for l in stb['lines'])
        # pagar e desfazer
        p = c.post('/api/commission/pay', json={'user_id': f['id'], 'start': sm['start'], 'end': sm['end'], 'amount': 200}).json()
        sm2 = c.get('/api/commission/summary').json(); rf = next(r for r in sm2['rows'] if r['user_id'] == f['id'])
        assert rf['paid'] == 200 and rf['due'] == 38 and sm2['paid'] == 200
        csv = c.get(f"/api/commission/statement?user_id={f['id']}&fmt=csv"); assert csv.status_code == 200 and 'A receber' in csv.text and '38,00' in csv.text
        c.delete(f"/api/commission/pay/{p['id']}")
        assert next(r for r in c.get('/api/commission/summary').json()['rows'] if r['user_id'] == f['id'])['due'] == 238
        # tirar a regra do item volta para o padrão
        c.put(f"/api/commission/item/{prog['id']}", json=None)
        assert c.get(f"/api/commission/statement?user_id={f['id']}").json()['commission'] == 30 + 108 + 8 + 56
