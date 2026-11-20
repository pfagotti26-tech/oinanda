"""Com dois workers, só um roda o ciclo de automações, campanhas e avisos."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from datetime import timedelta
from fastapi.testclient import TestClient
import automations
from core import db, now
from main import app

def test_um_ciclo_so(monkeypatch):
    with TestClient(app, base_url='https://testserver') as c:
        a = c.portal.call(automations.lead)
        monkeypatch.setattr(automations, 'LOOP_ID', 'outro-worker')
        b = c.portal.call(automations.lead)
        assert a is True and b is False
        async def expira(): await db.locks.update_one({'_id': 'loop'}, {'$set': {'until': now() - timedelta(seconds=1)}})
        c.portal.call(expira)
        assert c.portal.call(automations.lead) is True  # o primeiro caiu: o outro assume
