"""Promoções: cupom criado junto, quem sumiu com dias próprios, já comprou item, etiqueta, público que cumpre e regra 'vale a maior / soma'."""
import os
from datetime import date, timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from core import db
from main import app

def test_promos_condicoes():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona', 'email': 'pc@x.com', 'password': 'senha-forte-1', 'company': 'Clínica P', 'segment': 'clinica'})
        plano = c.post('/api/products', json={'name': 'Plano mensal', 'price': 280, 'kind': 'plano', 'duration_days': 30}).json()
        limp = c.post('/api/products', json={'name': 'Limpeza', 'price': 200, 'category': 'Facial'}).json()
        a = c.post('/api/contacts', json={'name': 'Ana Antiga', 'phone': '11911110001'}).json()
        b = c.post('/api/contacts', json={'name': 'Bia Nova', 'phone': '11911110002'}).json()
        old = (date.today() - timedelta(days=70)).isoformat()
        c.post('/api/sales', json={'contact_id': a['id'], 'date': old, 'items': [{'product_id': plano['id'], 'name': 'Plano mensal', 'qty': 1, 'price': 280}], 'payment': 'pix'})
        c.portal.call(lambda: db.contacts.update_one({'id': a['id']}, {'$set': {'tags': ['VIP']}}))
        # cupom digitado junto
        r = c.post('/api/promos', json={'name': 'Volta pra gente', 'kind': 'percent', 'value': 15, 'requires_coupon': True, 'code': 'volta15', 'only_inactive': True, 'inactive_days': 60,
                                        'bought_ids': [plano['id']], 'tags': ['VIP'], 'scope': 'products', 'product_ids': [plano['id']], 'max_per_contact': 1})
        assert r.status_code == 200, r.text
        pid = r.json()['id']
        ls = c.get('/api/promos').json()
        pr = next(x for x in ls['items'] if x['id'] == pid)
        assert pr['public_code'] == 'VOLTA15' and pr['coupons'] == 1 and 'há mais de 60 dias' in pr['rule'] and 'já comprou Plano mensal' in pr['rule'] and 'VIP' in pr['rule']
        assert c.post('/api/promos', json={'name': 'Outra', 'kind': 'percent', 'value': 5, 'requires_coupon': True, 'code': 'VOLTA15'}).status_code == 409
        # público
        au = c.post('/api/promos/audience', json={'only_inactive': True, 'inactive_days': 60, 'bought_ids': [plano['id']], 'tags': ['VIP']}).json()
        assert au['count'] == 1 and au['sample'] == ['Ana Antiga']
        assert c.post('/api/promos/audience', json={'only_inactive': True, 'inactive_days': 90}).json()['count'] == 0
        assert c.post('/api/promos/audience', json={'first_purchase': True}).json()['count'] == 1
        # na venda: Ana ganha com o cupom; Bia não (não comprou o plano)
        it = [{'product_id': plano['id'], 'qty': 1, 'price': 280}]
        ea = c.post('/api/promos/evaluate', json={'contact_id': a['id'], 'items': it, 'coupon': 'VOLTA15'}).json()
        assert ea['discount'] == 42 and ea['coupon']['ok']
        eb = c.post('/api/promos/evaluate', json={'contact_id': b['id'], 'items': it, 'coupon': 'VOLTA15'}).json()
        assert eb['discount'] == 0 and any('já comprou' in (x['reason'] or '') or 'sem comprar' in (x['reason'] or '') for x in eb['available'])
        # vale a maior x soma
        c.post('/api/promos', json={'name': 'Dez', 'kind': 'percent', 'value': 10})
        c.post('/api/promos', json={'name': 'Cinco', 'kind': 'percent', 'value': 5})
        it2 = [{'product_id': limp['id'], 'qty': 1, 'price': 200}]
        assert c.post('/api/promos/evaluate', json={'contact_id': b['id'], 'items': it2}).json()['discount'] == 20
        c.put('/api/promos-settings', json={'max_discount_pct': None, 'promo_stack': 'sum'})
        assert c.post('/api/promos/evaluate', json={'contact_id': b['id'], 'items': it2}).json()['discount'] == 29  # 10% de 200 + 5% de 180
        assert c.get('/api/promos').json()['promo_stack'] == 'sum'
