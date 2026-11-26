import os
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
from main import app
from core import db, now, uid

def test_filtros_por_dia_vendedor_e_situacao():
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Bia Dona', 'email': 'filt@cl.com', 'password': 'senha-forte-1', 'company': 'Clínica Filtro', 'segment': 'clinica'})
        me = c.get('/api/auth/me').json(); cid = me['company']['id']; vend = uid()
        t0 = now() - timedelta(days=3)
        rows = [('Ana', vend, t0, {'unread': 2, 'last_dir': 'in', 'last_in_at': t0}), ('Bruno', None, now(), {'needs_human': True, 'last_dir': 'in', 'last_in_at': now()}), ('Caio', vend, now(), {'sales_count': 1, 'last_dir': 'out'})]
        async def seed():
            for n, a, at, ex in rows:
                k = uid()
                await db.contacts.insert_one({'id': k, 'company_id': cid, 'name': n, 'wa_id': '5511' + str(len(n)) * 9, 'assigned_to': a, 'last_at': at, 'created_at': at, **ex})
                await db.messages.insert_one({'id': uid(), 'company_id': cid, 'contact_id': k, 'dir': 'in', 'text': 'oi', 'at': at})
        c.portal.call(seed)
        names = lambda **p: sorted(x['name'] for x in c.get('/api/conversations', params={'filter': 'all', **p}).json())
        from zoneinfo import ZoneInfo
        d = t0.astimezone(ZoneInfo('America/Sao_Paulo')).date().isoformat()
        assert names(d1=d, d2=d) == ['Ana']
        assert names(pro=vend) == ['Ana', 'Caio']
        assert names(pro='_none') == ['Bruno']
        assert names(sit='unread') == ['Ana'] and names(sit='human') == ['Bruno'] and names(sit='sold') == ['Caio']
        assert names(sit='stale') == ['Ana']
        assert names(pro=vend, d1=d) == ['Ana']
