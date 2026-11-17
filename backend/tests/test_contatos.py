import os
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from core import db, now
from main import app
from tests.test_wallet import inbound, ago

def mk(): return TestClient(app, base_url='https://testserver')
def lst(c, **kw): return c.post('/api/contacts/list', json=kw).json()
def names(r): return [x['name'] for x in r['items']]

def prepara(c, monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    c.post('/api/auth/signup', json={'name': 'Dona C', 'email': 'dc@x.com', 'password': 'senha-forte-1', 'company': 'Studio C', 'segment': 'studio'})
    c.post('/api/wa/connect', json={'phone_number_id': '4440011', 'waba_id': '4440012', 'token': 'EAA' + 'q' * 30})
    plano = c.post('/api/products', json={'name': 'Plano mensal', 'category': 'Planos', 'price': 200, 'duration_days': 30}).json()
    for wa, nm in [('5511910000001', 'Ana Júlia'), ('5511910000002', 'Beto'), ('5511910000003', 'Caio'), ('5511910000004', 'Duda')]: inbound(c, wa, nm, 'oi')
    cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
    c.post('/api/sales', json={'contact_id': cv['Ana Júlia'], 'date': ago(5), 'items': [{'product_id': plano['id'], 'name': plano['name'], 'price': 200, 'qty': 1}]})
    c.post('/api/sales', json={'contact_id': cv['Beto'], 'date': ago(60), 'items': [{'product_id': plano['id'], 'name': plano['name'], 'price': 200, 'qty': 2}]})
    c.patch('/api/contacts/' + cv['Ana Júlia'], json={'tags': ['pilates', 'manhã']})
    c.patch('/api/contacts/' + cv['Beto'], json={'tags': ['pilates']})
    c.patch('/api/contacts/' + cv['Caio'], json={'tags': ['funcional'], 'email': 'caio@exemplo.com'})
    return cv, plano

def test_lista_visoes_busca_filtros_e_ordem(monkeypatch):
    with mk() as c:
        cv, plano = prepara(c, monkeypatch)
        me = c.get('/api/auth/me').json()['user']['id']
        r = lst(c, facets=True, sort='name', dir='asc')
        assert r['count'] == 4 and r['total'] == 4 and names(r) == ['Ana Júlia', 'Beto', 'Caio', 'Duda']
        v = {x['id']: x['count'] for x in r['views']}
        assert v == {'all': 4, 'clientes': 2, 'prospects': 2, 'waiting': 4, 'noowner': 4}
        assert {t['name']: t['count'] for t in r['facets']['tags']} == {'pilates': 2, 'manhã': 1, 'funcional': 1}
        assert r['facets']['products'][0]['name'] == 'Plano mensal' and r['facets']['stages'] and r['facets']['owners'][0]['id'] == me
        ana = r['items'][0]
        assert ana['spent'] == 200 and ana['purchases'] == 1 and ana['has'][0]['name'] == 'Plano mensal' and ana['waiting'] and ana['kind'] == 'cliente'
        # visões
        assert set(names(lst(c, view='clientes'))) == {'Ana Júlia', 'Beto'} and set(names(lst(c, view='prospects'))) == {'Caio', 'Duda'}
        # busca: sem acento, por telefone, por etiqueta e por e-mail
        assert names(lst(c, q='ana julia')) == ['Ana Júlia'] and names(lst(c, q='0000003')) == ['Caio']
        assert names(lst(c, q='funcional')) == ['Caio'] and names(lst(c, q='caio@exemplo')) == ['Caio'] and lst(c, q='zzz')['count'] == 0
        # etiquetas: qualquer, todas, não tem
        f = lambda **t: set(names(lst(c, filters={'tags': t})))
        assert f(mode='any', values=['pilates', 'funcional']) == {'Ana Júlia', 'Beto', 'Caio'}
        assert f(mode='all', values=['pilates', 'manhã']) == {'Ana Júlia'} and f(mode='none', values=['pilates']) == {'Caio', 'Duda'}
        # item ativo: a compra do Beto (60 dias, 2 meses) venceu; a da Ana vale
        assert names(lst(c, filters={'product': plano['id']})) == ['Ana Júlia']
        # última conversa
        c.portal.call(lambda: db.contacts.update_one({'id': cv['Duda']}, {'$set': {'last_at': now() - timedelta(days=40)}}))
        assert names(lst(c, filters={'last': {'op': 'more', 'days': 30}})) == ['Duda']
        assert 'Duda' not in names(lst(c, filters={'last': {'op': 'less', 'days': 30}}))
        # filtro soma com a visão
        assert names(lst(c, view='clientes', filters={'tags': {'values': ['manhã']}})) == ['Ana Júlia']
        # ordem e paginação
        assert names(lst(c, sort='spent'))[:2] == ['Beto', 'Ana Júlia'] and names(lst(c, sort='last', dir='asc'))[0] == 'Duda'
        assert names(lst(c, sort='expiry', dir='asc'))[0] == 'Ana Júlia'
        p2 = lst(c, sort='name', dir='asc', size=3, page=2)
        assert p2['pages'] == 2 and names(p2) == ['Duda'] and lst(c, size=3, page=9)['page'] == 2
        # segmento salvo vira visão
        s = c.post('/api/segments', json={'name': 'Gastou 300+', 'rules': [{'field': 'spent', 'op': 'gte', 'value': 300}]}).json()
        r = lst(c, facets=True)
        seg = next(x for x in r['views'] if x.get('segment'))
        assert seg == {'id': 'seg:' + s['id'], 'name': 'Gastou 300+', 'count': 1, 'segment': True} and names(lst(c, view=seg['id'])) == ['Beto']
        assert c.post('/api/contacts/list', json={'view': 'seg:nada'}).status_code == 404 and c.post('/api/contacts/list', json={'view': 'x'}).status_code == 400

def test_acoes_em_varios_e_exportar(monkeypatch):
    with mk() as c:
        cv, plano = prepara(c, monkeypatch)
        me = c.get('/api/auth/me').json()['user']['id']
        b = lambda **kw: c.post('/api/contacts/bulk', json=kw)
        assert b(ids=[cv['Caio'], cv['Duda'], 'nao-existe'], action='tag', value='vip').json()['count'] == 2
        assert set(names(lst(c, filters={'tags': {'values': ['vip']}}))) == {'Caio', 'Duda'}
        # todos os da lista filtrada, sem mandar ids
        assert b(query={'filters': {'tags': {'values': ['pilates']}}}, action='untag', value='pilates').json()['count'] == 2
        assert lst(c, filters={'tags': {'values': ['pilates']}})['count'] == 0
        st = lst(c, facets=True)['facets']['stages']
        assert b(ids=[cv['Caio'], cv['Duda']], action='stage', value=st[1]['id']).json()['count'] == 2
        assert set(names(lst(c, filters={'stage': [st[1]['id']]}))) == {'Caio', 'Duda'}
        assert b(ids=[cv['Caio']], action='stage', value='nao-existe').status_code == 400
        assert b(ids=[cv['Ana Júlia']], action='owner', value=me).json()['count'] == 1
        assert names(lst(c, filters={'owner': [me]})) == ['Ana Júlia'] and lst(c, view='noowner')['count'] == 3
        assert set(names(lst(c, filters={'owner': ['', me]}))) == {'Ana Júlia', 'Beto', 'Caio', 'Duda'}
        assert b(ids=[cv['Ana Júlia']], action='owner', value='').json()['count'] == 1 and lst(c, view='noowner')['count'] == 4
        assert b(ids=[], action='tag', value='x').status_code == 400 and b(ids=[cv['Caio']], action='tag', value=' ').status_code == 400
        assert b(ids=[cv['Caio']], action='owner', value='fantasma').status_code == 404
        x = c.post('/api/contacts/export', json={'query': {'view': 'clientes'}})
        linhas = x.text.strip().splitlines()
        assert x.status_code == 200 and len(linhas) == 3 and linhas[1].startswith('Ana Júlia;+5511910000001') and 'Plano mensal' in linhas[1]
        assert len(c.post('/api/contacts/export', json={'ids': [cv['Duda']]}).text.strip().splitlines()) == 2

def test_outra_conta_nao_ve_nem_mexe(monkeypatch):
    with mk() as c:
        cv, _ = prepara(c, monkeypatch)
        d = TestClient(app, base_url='https://testserver')
        d.post('/api/auth/signup', json={'name': 'Outra', 'email': 'o@x.com', 'password': 'senha-forte-1', 'company': 'Outra', 'segment': 'studio'})
        assert lst(d)['count'] == 0
        assert d.post('/api/contacts/bulk', json={'ids': [cv['Caio']], 'action': 'tag', 'value': 'x'}).status_code == 400
        assert d.post('/api/contacts/export', json={'ids': [cv['Caio']]}).status_code == 400
