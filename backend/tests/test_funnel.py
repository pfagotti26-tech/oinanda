import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from fastapi.testclient import TestClient
from main import app

def test_funil():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Gil Dono', 'email': 'gil@st.com', 'password': 'senha-forte-1', 'company': 'Studio Gil', 'segment': 'studio'})
        f = c.get('/api/funnel').json()
        names = [s['name'] for s in f['stages']]
        assert names[0] == 'Novo contato' and 'Matriculou' in names and f['cards'] == []
        r = c.post('/api/contacts', json={'name': 'Joana', 'phone': '(11) 98888-7777', 'value': 289})
        assert r.status_code == 200 and r.json()['wa_id'] == '5511988887777'
        assert c.post('/api/contacts', json={'name': 'Joana 2', 'phone': '11988887777'}).status_code == 409
        cid = r.json()['id']
        won = next(s for s in f['stages'] if s['kind'] == 'won')
        p = c.patch(f'/api/contacts/{cid}', json={'stage': won['id'], 'notes': 'Plano mensal', 'tags': ['manhã', ' manhã ', 'pilates']})
        assert p.status_code == 200 and p.json()['tags'] == ['manhã', 'pilates'] and p.json()['won_at']
        assert c.patch(f'/api/contacts/{cid}', json={'stage': 'nao-existe'}).status_code == 400
        f = c.get('/api/funnel').json()
        assert f['totals'][won['id']] == {'count': 1, 'value': 289}
        s = c.get('/api/funnel/summary').json()
        assert s['won'] == 1 and s['won_value'] == 289 and s['new_contacts'] == 1
        d = c.get(f'/api/conversations/{cid}').json()
        assert any('moveu para Matriculou' in m['text'] for m in d['messages'])
        # editar etapas: apagar a etapa ganha devolve o contato para a primeira
        keep = [x for x in f['stages'] if x['id'] != won['id']] + [{'name': 'Matrícula fechada', 'kind': 'won'}]
        r = c.put('/api/funnel/stages', json={'stages': keep})
        assert r.status_code == 200
        f = c.get('/api/funnel').json()
        assert f['cards'][0]['stage'] == f['stages'][0]['id'] and f['stages'][-1]['name'] == 'Matrícula fechada'
        assert c.put('/api/funnel/stages', json={'stages': [{'name': 'A', 'kind': 'won'}, {'name': 'B', 'kind': 'lost'}]}).status_code == 400
        # atendente não edita etapas e só vê as dele + sem dono
        inv = c.post('/api/team', json={'name': 'Ana Atende', 'email': 'ana@st.com', 'role': 'agent'}).json()
    with TestClient(app, base_url='https://testserver') as a:
        a.post('/api/auth/login', json={'email': 'ana@st.com', 'password': inv['temp_password']})
        a.post('/api/auth/password', json={'current_password': inv['temp_password'], 'new_password': 'nova-senha-forte'})
        assert a.put('/api/funnel/stages', json={'stages': keep}).status_code == 403
        assert len(a.get('/api/funnel').json()['cards']) == 1
