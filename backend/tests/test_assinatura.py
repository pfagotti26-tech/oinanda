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


def test_resposta_fora_do_formato_nunca_mostra_json():
    t = 'Sobre desconto, vou chamar o Paulo. Não fazemos disparo para lista comprada.\n\n{"reply": "Sobre desconto, não tenho essa condição aqui, então vou chamar o Paulo, do nosso time, para'
    d = ai.parse(t)
    assert '{' not in d['reply'] and 'reply' not in d['reply'] and d['reply'].startswith('Sobre desconto') and d['handoff']
    d = ai.parse('{"reply": "O **Plano Pro** custa R$ 297. Posso te explicar mais sobre ele e tamb')
    assert d['reply'] == 'O Plano Pro custa R$ 297.'
    d = ai.parse('{"reply": "Oi **tudo** bem", "handoff": true, "reason": "pediu pessoa"}')
    assert d['reply'] == 'Oi tudo bem' and d['handoff'] and d['reason'] == 'pediu pessoa'
    assert ai.parse('Oi! Posso ajudar?')['reply'] == 'Oi! Posso ajudar?'


def test_caixa_por_prioridade():
    import inbox
    t = now()
    wait = {'last_dir': 'in', 'last_in_at': t - timedelta(hours=2)}
    old = {'last_dir': 'in', 'last_in_at': t - timedelta(days=9)}
    assert inbox.waiting(wait) and not inbox.waiting(old)  # depois de 7 dias sai da fila sozinha
    assert inbox.needs_you(wait, 'u1') and not inbox.needs_you(old, 'u1')
    assert inbox.needs_you({'needs_human': True, 'last_dir': 'out'}, 'u1')
    assert inbox.needs_you({'assigned_to': 'u1', 'transfer_seen': False, 'last_dir': 'out'}, 'u1') and not inbox.needs_you({'assigned_to': 'u2', 'transfer_seen': False, 'last_dir': 'out'}, 'u1')
    nz = {'last_dir': 'out', 'last_by': 'Nanda (IA)', 'last_at': t - timedelta(hours=1)}
    assert inbox.with_nanda(nz, 'auto') and not inbox.with_nanda(nz, 'copilot') and not inbox.with_nanda({**nz, 'last_by': 'Paulo'}, 'auto')
    assert not inbox.with_nanda({**nz, 'needs_human': True}, 'auto') and inbox.with_nanda({**nz, 'ai_mode': 'auto'}, 'copilot')
