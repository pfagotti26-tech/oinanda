import os, asyncio
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from core import db, now
from main import app
import teams

def test_comissao_por_vendedor_e_turno_no_rodizio():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dono', 'email': 'd@cm.com', 'password': 'senha-forte-1', 'company': 'Lanç', 'segment': 'lancamento'})
        cid = c.get('/api/auth/me').json()['company']['id']
        a = c.post('/api/team', json={'name': 'Ana SDR', 'email': 'a@cm.com', 'role': 'agent'}).json()['member']
        b = c.post('/api/team', json={'name': 'Beto Closer', 'email': 'b@cm.com', 'role': 'agent'}).json()['member']
        assert c.put(f"/api/team/{a['id']}/work", json={'commission_pct': 10}).json()['commission_pct'] == 10
        assert c.put(f"/api/team/{b['id']}/work", json={'commission_pct': 150}).status_code == 422
        c.put(f"/api/team/{b['id']}/work", json={'commission_pct': 5})
        ct = c.post('/api/contacts', json={'name': 'Cliente Um', 'phone': '11911112222'}).json()
        c.post('/api/sales', json={'contact_id': ct['id'], 'items': [{'name': 'Curso', 'qty': 1, 'price': 1000}], 'payment': 'pix',
                                   'sellers': [{'user_id': a['id'], 'pct': 50}, {'user_id': b['id'], 'pct': 50}]})
        c.post('/api/sales', json={'contact_id': ct['id'], 'items': [{'name': 'Mentoria', 'qty': 1, 'price': 2000}], 'payment': 'pix', 'sellers': [{'user_id': a['id'], 'pct': 100}]})
        r = c.get('/api/sales/commissions').json()
        ra = next(x for x in r['rows'] if x['user_id'] == a['id']); rb = next(x for x in r['rows'] if x['user_id'] == b['id'])
        assert ra['value'] == 2500 and ra['commission'] == 250 and ra['count'] == 2
        det = c.get(f"/api/sales/commissions?detail={a['id']}").json()['detail']
        assert len(det) == 2 and sum(l['commission'] for l in det) == 250 and {l['share_pct'] for l in det} == {50, 100}
        csv = c.get('/api/sales/commissions?fmt=csv'); assert csv.status_code == 200 and 'Ana SDR' in csv.text and '250,00' not in csv.text and '200,00' in csv.text
        assert rb['value'] == 500 and rb['commission'] == 25 and r['total_commission'] == 275
        # turno: fora do horário, a pessoa não recebe conversa do rodízio
        from zoneinfo import ZoneInfo
        h = now().astimezone(ZoneInfo('America/Sao_Paulo')).hour
        fora = {'days': list(range(7)), 'start': f'{(h + 2) % 24:02d}:00', 'end': f'{(h + 3) % 24:02d}:00'}
        dentro = {'days': list(range(7)), 'start': f'{h:02d}:00', 'end': f'{(h + 1) % 24:02d}:00' if h < 23 else '23:59'}
        c.put(f"/api/team/{a['id']}/work", json={'shift': fora}); c.put(f"/api/team/{b['id']}/work", json={'shift': dentro})
        team = {'id': 'tm1', 'company_id': cid, 'member_ids': [a['id'], b['id']]}
        c.portal.call(lambda: db.teams.insert_one(dict(team)))
        assert c.portal.call(lambda: teams.pick_member(team)) == b['id']
        c.put(f"/api/team/{a['id']}/work", json={'clear_shift': True})
        picks = {c.portal.call(lambda: teams.pick_member({**team, 'rr': i})) for i in range(2)}
        assert picks == {a['id'], b['id']}
        assert c.put(f"/api/team/{a['id']}/work", json={'shift': {'days': [], 'start': '08:00', 'end': '12:00'}}).status_code == 400
