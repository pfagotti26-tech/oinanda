"""Catálogo por planilha: modelo, prévia (novo, atualiza, problema), gravar, juntar por código/nome e desfazer."""
import os, io
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def xlsx(rows):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    for r in rows: ws.append(r)
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def test_catalogo_planilha():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona C', 'email': 'cat@x.com', 'password': 'senha-forte-1', 'company': 'Studio C', 'segment': 'studio'})
        r = c.get('/api/catalog/import/template.xlsx'); assert r.status_code == 200 and r.content[:2] == b'PK'
        c.post('/api/products', json={'name': 'Plano mensal', 'price': 200, 'kind': 'plano', 'duration_days': 30})
        f = xlsx([['Produto', 'SKU', 'Grupo', 'Tipo', 'Valor', 'Validade', 'Status'],
                  ['Plano mensal', 'PL-01', 'Planos', 'Plano', '280,00', 30, 'Ativo'],
                  ['Protetor FPS 50', 123, 'Produtos', 'Produto', 'R$ 89,90', '', 'Sim'],
                  ['Pacote 10 sessões', 'PC-1', 'Corporal', 'Pacote', '1.200,00', '', ''],
                  ['Avaliação', '', '', '', '', '', 'Não'],
                  ['Protetor FPS 50 de novo', 123, '', '', 'abc', '', ''],
                  ['X', '', '', '', '', '', '']])
        r = c.post('/api/catalog/import/preview', files={'file': ('cat.xlsx', f)}).json()
        assert set(r['mapping'].values()) >= {'name', 'code', 'category', 'kind', 'price', 'duration', 'active'}
        it = {i['name']: i for i in r['items']}
        assert it['Plano mensal']['match'] and it['Protetor FPS 50']['code'] == '123' and it['Protetor FPS 50']['price'] == 89.9
        assert it['Pacote 10 sessões']['kind'] == 'plano' and it['Pacote 10 sessões']['duration_days'] == 30
        assert it['Avaliação']['active'] is False and it['Avaliação']['kind'] == 'servico'
        assert it['Protetor FPS 50 de novo'].get('error') and it['X'].get('error')
        assert r['summary'] == {'new': 3, 'update': 1, 'skip': 0, 'errors': 2, 'over': 0}
        ok = [i for i in r['items'] if not i.get('error')]
        j = c.post('/api/catalog/import', json={'items': ok, 'file': 'cat.xlsx'}).json()
        assert j['created'] == 3 and j['updated'] == 1
        ps = {p['name']: p for p in c.get('/api/products').json()}
        assert ps['Plano mensal']['price'] == 280 and ps['Plano mensal']['code'] == 'PL-01'
        # reimportar pelo código muda o nome em vez de duplicar
        f2 = xlsx([['Nome', 'Código', 'Preço'], ['Protetor solar FPS 50', '123', '99']])
        r2 = c.post('/api/catalog/import/preview', files={'file': ('c2.csv' if False else 'c2.xlsx', f2)}).json()
        assert r2['summary']['update'] == 1 and r2['summary']['new'] == 0
        u = c.post(f"/api/catalog/import/{j['id']}/undo").json()
        assert u == {'removed': 3, 'deactivated': 0, 'restored': 1}
        ps = {p['name']: p for p in c.get('/api/products').json()}
        assert not {'Protetor FPS 50', 'Pacote 10 sessões', 'Avaliação'} & set(ps) and ps['Plano mensal']['price'] == 200 and not ps['Plano mensal'].get('code')
        # CSV com ponto e vírgula
        csv = 'Nome;Preço\nCorte feminino;120,00\nEscova;60\n'.encode('utf-8')
        r3 = c.post('/api/catalog/import/preview', files={'file': ('c.csv', csv)}).json()
        assert r3['summary']['new'] == 2
