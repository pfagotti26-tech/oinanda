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


def test_cartao_compartilhado_no_instagram():
    import instagram
    t, u = instagram.shared_card({'generic': {'elements': [{'title': 'Promoção de verão', 'default_action': {'url': 'https://x.com/p'}}]}})
    assert t == '🔗 Promoção de verão' and u == 'https://x.com/p'
    t, u = instagram.shared_card({'generic': [{'title': '', 'buttons': [{'url': 'https://y.com'}]}]})
    assert t == '🔗 Compartilhou um conteúdo' and u == 'https://y.com'
    assert instagram.shared_card({}) == ('🔗 Compartilhou um conteúdo', None)


def test_assinatura_cancelada():
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona A', 'email': 'da@x.com', 'password': 'senha-forte-1', 'company': 'Studio A', 'segment': 'studio'})
        p = c.post('/api/products', json={'name': 'Mensal recorrente', 'price': 300, 'kind': 'plano', 'duration_days': 30, 'recurring': True}).json()
        q = c.post('/api/products', json={'name': 'Anual recorrente', 'price': 2400, 'kind': 'plano', 'duration_days': 365, 'recurring': True}).json()
        ids = []
        for i, prod in enumerate([p, p, q]):
            ct = c.post('/api/contacts', json={'name': f'Aluno {i}', 'phone': f'1198888000{i}'}).json()
            ids.append(ct['id'])
            r = c.post('/api/sales', json={'contact_id': ct['id'], 'date': (now() - timedelta(days=60)).date().isoformat(), 'items': [{'product_id': prod['id'], 'name': prod['name'], 'qty': 1, 'price': prod['price']}]})
            assert r.status_code == 200, r.text
        ov = c.get('/api/segments/overview').json()
        assert ov['subs'] == 3 and ov['mrr'] == 300 + 300 + round(2400 * 30 / 365, 2)
        # aluno 0 cancelou hoje: sai das assinaturas, aparece como "voltar a ter"
        w = c.post(f'/api/contacts/{ids[0]}/subscription', json={'product_id': p['id'], 'reason': 'mudou de cidade'}).json()
        it = w['items'][0]
        assert it['cancelled_at'] and it['state'] == 'vencido' and [o['why'] for o in w['offers']] == ['voltar']
        # aluno 1 cancela no fim do mês: ainda vale até lá
        fut = (now() + timedelta(days=10)).date().isoformat()
        w1 = c.post(f'/api/contacts/{ids[1]}/subscription', json={'product_id': p['id'], 'at': fut}).json()
        assert w1['items'][0]['state'] == 'vence_logo'
        ov = c.get('/api/segments/overview').json()
        assert ov['subs'] == 2 and ov['subs_lost30'] == 1
        # voltou: desfaz
        w = c.post(f'/api/contacts/{ids[0]}/subscription', json={'product_id': p['id'], 'cancel': False}).json()
        assert w['items'][0]['state'] == 'ativo' and not w['items'][0].get('cancelled_at')
        # comprou de novo depois de cancelar: volta a valer sozinho
        c.post(f'/api/contacts/{ids[1]}/subscription', json={'product_id': p['id']})
        c.post('/api/sales', json={'contact_id': ids[1], 'date': (now() + timedelta(hours=1)).date().isoformat(), 'items': [{'product_id': p['id'], 'name': p['name'], 'qty': 1, 'price': 300}]})
        w = c.get(f'/api/contacts/{ids[1]}/wallet').json()
        assert w['items'][0]['state'] == 'ativo'
        # item que não é assinatura não pode ser "cancelado" aqui
        assert c.post(f'/api/contacts/{ids[0]}/subscription', json={'product_id': 'x'}).status_code == 400
