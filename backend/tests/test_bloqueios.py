import os
from datetime import date, timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app

def mon():
    d = date.today() + timedelta(days=7)
    return d - timedelta(days=d.weekday())

def test_bloqueios():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Rita Dona', 'email': 'rita@bl.com', 'password': 'senha-forte-1', 'company': 'Salão Rita', 'segment': 'clinica'})
        s = c.get('/api/agenda/settings').json(); sv = s['services'][0]; m = mon()
        sl = lambda d, **k: c.get('/api/agenda/slots', params={'service_id': sv['id'], 'date': str(d), **k}).json()
        assert '12:00' in sl(m)
        # almoço a semana toda, 12h às 13h
        r = c.post('/api/agenda/blocks', json={'date': str(m), 'until': str(m + timedelta(days=4)), 'start_time': '12:00', 'end_time': '13:00', 'reason': 'Almoço'})
        assert r.status_code == 200 and r.json()['blocks'] == 5
        for i in range(5): assert '12:00' not in sl(m + timedelta(days=i)) and '12:30' not in sl(m + timedelta(days=i)) and '11:30' in sl(m + timedelta(days=i))
        # feriado: dia todo fechado
        c.post('/api/agenda/blocks', json={'date': str(m + timedelta(days=2)), 'reason': 'Feriado'})
        assert sl(m + timedelta(days=2)) == []
        cid = c.post('/api/contacts', json={'name': 'Lia', 'phone': '11955550000'}).json()['id']
        assert c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(m + timedelta(days=2)), 'time': '09:00'}).status_code == 409
        # profissionais: férias só da Júlia
        c.put('/api/agenda/settings', json={'pros': [{'name': 'Camila'}, {'name': 'Júlia'}]})
        pros = c.get('/api/agenda/settings').json()['pros']; ju = next(p for p in pros if p['name'] == 'Júlia')
        c.post('/api/agenda/blocks', json={'pro_id': ju['id'], 'date': str(m + timedelta(days=7)), 'until': str(m + timedelta(days=11)), 'reason': 'Férias'})
        assert sl(m + timedelta(days=7), pro_id=ju['id']) == []
        assert '09:00' in sl(m + timedelta(days=7))  # a Camila atende
        r = c.post('/api/appointments', json={'contact_id': cid, 'service_id': sv['id'], 'date': str(m + timedelta(days=7)), 'time': '09:00'})
        assert r.status_code == 200 and r.json()['pro_name'] == 'Camila'
        bl = c.get('/api/agenda/blocks', params={'start': str(m), 'end': str(m + timedelta(days=13))}).json()
        assert len(bl) == 7 and any(b['pro_name'] == 'Júlia' and b['all_day'] for b in bl)
        # liberar o almoço inteiro de uma vez
        al = next(b for b in bl if b['reason'] == 'Almoço')
        assert c.delete(f"/api/agenda/blocks/{al['id']}", params={'all': True}).status_code == 200
        assert '12:00' in sl(m)
