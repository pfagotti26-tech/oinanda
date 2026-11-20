import os, io
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
import whatsapp
from main import app
from tests.test_origins import inbound

def xlsx(rows):
    wb = Workbook(); ws = wb.active
    for r in rows: ws.append(r)
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def test_importar_planilha(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona I', 'email': 'di@x.com', 'password': 'senha-forte-1', 'company': 'Studio I', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        inbound(c, '5511988880001', 'Já Conversou', 'Oi!')
        c.post('/api/products', json={'name': 'Plano mensal', 'price': 280, 'kind': 'plano', 'duration_days': 30, 'category': 'Planos'})
        data = xlsx([['Cliente', 'Celular', 'E-mail', 'Plano', 'Data', 'Valor pago', 'Validade', 'Tags', 'Origem'],
                     ['Ana Souza', '(11) 98765-4321', 'ana@x.com', 'plano MENSAL', '05/11/2026', 'R$ 280,00', '', 'pilates', 'Instagram'],
                     ['Ana Souza', '11987654321', '', 'Avaliação', '01/10/2026', '0', '', '', ''],
                     ['Bruno Lima', '11 91234-5678', '', '', '', '', '', 'indicação', ''],
                     ['Já Conversou', '11988880001', '', 'Aula avulsa', '10/11/2026', '60', '20/12/2026', '', ''],
                     ['Sem Fone', '', '', '', '', '', '', '', ''],
                     ['Fone Ruim', '1234', '', '', '', '', '', '', '']])
        up = c.post('/api/import/upload', files={'file': ('clientes.xlsx', data, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}).json()
        m = up['mapping']
        assert {m[k] for k in m} >= {'name', 'phone', 'email', 'product', 'date', 'value', 'expires', 'tags', 'origin'} and up['count'] == 6
        ck = c.post(f'/api/import/{up["id"]}/check', json={'mapping': m}).json()
        assert ck['people'] == 3 and ck['new'] == 2 and ck['existing'] == 1 and ck['clients'] == 2 and ck['prospects'] == 1 and ck['buys'] == 3 and ck['error_count'] == 2
        assert set(ck['unknown_products']) == {'Avaliação', 'Aula avulsa'}
        r = c.post(f'/api/import/{up["id"]}/run', json={'mapping': m, 'consent': False}).json()
        assert r['created'] == 2 and r['updated'] == 1 and r['sales'] == 3
        assert c.post(f'/api/import/{up["id"]}/run', json={'mapping': m}).status_code == 400  # não importa duas vezes
        base = {x['name']: x for x in c.post('/api/segments/preview', json={'rules': []}).json()['items']}
        assert base['Ana Souza']['kind'] == 'cliente' and base['Bruno Lima']['kind'] == 'prospect'
        # Bruno (prospect importado sem autorização) não recebe campanha; o vencimento informado vale
        pv = c.post('/api/segments/preview', json={'rules': []}).json()
        assert pv['reachable'] == pv['count'] - 1
        ov = c.get('/api/segments/overview').json()
        assert ov['due30'] >= 1  # Ana (plano mensal) e Já Conversou (vencimento informado)
        assert c.get('/api/reports/sales?days=30').json()['totals']['new_contacts'] == 1  # só quem chegou conversando
        assert {x['name'] for x in c.get('/api/funnel').json()['cards']} == {'Já Conversou'}
        lst = c.get('/api/sales?start=2026-09-01&end=2026-12-31&imported=hide').json()
        assert not any(s.get('imported') for s in (lst['items'] if isinstance(lst, dict) else lst))
        # desfazer
        u = c.post(f'/api/import/{up["id"]}/undo').json()
        assert u['removed'] == 2
        names = {x['name'] for x in c.post('/api/segments/preview', json={'rules': []}).json()['items']}
        assert names == {'Já Conversou'}
        tpl = c.get('/api/import/template.xlsx')
        assert tpl.status_code == 200 and load_workbook(io.BytesIO(tpl.content)).active['A1'].value == 'Nome'
        # CSV com ponto e vírgula
        csvb = 'Nome;Telefone\nCarla;(21) 99999-0000\n'.encode('latin-1')
        up2 = c.post('/api/import/upload', files={'file': ('c.csv', csvb, 'text/csv')}).json()
        assert up2['count'] == 1 and set(up2['mapping'].values()) == {'name', 'phone'}
