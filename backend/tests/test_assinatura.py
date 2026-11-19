"""Assinatura que renova sozinha: fica ativa na carteira, sem vencimento e sem oferta de renovação."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from core import now
import wallet, ai


def test_assinatura_nao_vence():
    old = now() - timedelta(days=200)
    products = {'a': {'id': 'a', 'name': 'Plano Pro', 'duration_days': 30, 'recurring': True, 'price': 297},
                'b': {'id': 'b', 'name': 'Pacote 10', 'duration_days': 30, 'price': 500}}
    sales = [{'date': old, 'items': [{'product_id': 'a', 'name': 'Plano Pro', 'qty': 1, 'total': 297}, {'product_id': 'b', 'name': 'Pacote 10', 'qty': 1, 'total': 500}]}]
    hs = {x['product_id']: x for x in wallet.compute_holdings(sales, products)}
    assert hs['a']['recurring'] and hs['a']['expires_at'] is None and wallet.state_of(hs['a']) == 'ativo'
    assert wallet.state_of(hs['b']) == 'vencido'
    offers = wallet.offers_for(list(hs.values()), products)
    assert [o['product_id'] for o in offers] == ['b']
    txt = ai.catalog_text([{'name': 'Plano Pro', 'category': 'Planos', 'price': 297, 'duration_days': 30, 'recurring': True}])
    assert 'assinatura' in txt and 'vale 30 dias' not in txt
