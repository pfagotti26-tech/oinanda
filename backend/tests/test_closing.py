"""Encerrar com motivo, pesquisa de 0 a 10 pelo WhatsApp e relatório."""
import os
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp
from main import app
from tests.test_origins import inbound


def test_motivo_pesquisa_e_relatorio(monkeypatch):
    sent = []
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): sent.append(payload); return {'messages': [{'id': 'o%d' % len(sent)}]}
        return {'success': True, 'display_phone_number': '+55 11 97575-4765'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona C', 'email': 'dc@x.com', 'password': 'senha-forte-1', 'company': 'Studio C', 'segment': 'studio'})
        c.post('/api/wa/connect', json={'phone_number_id': '6660011', 'waba_id': '6660012', 'token': 'EAA' + 'q' * 30})
        c.put('/api/ai/settings', json={'default_mode': 'off'})
        inbound(c, '5511977770001', 'Marina Alves', 'Oi, quero saber o preço')
        cid = next(x for x in c.get('/api/conversations?filter=all').json() if x['name'] == 'Marina Alves')['id']
        s = c.get('/api/closing/settings').json()
        assert 'Comprou' in s['reasons'] and s['csat'] is False
        s = c.put('/api/closing/settings', json={'csat': True, 'reasons': ['Comprou', 'Agendou', ' ', 'Comprou']}).json()
        assert s['csat'] and s['reasons'] == ['Comprou', 'Agendou']
        n0 = len(sent)
        r = c.post(f'/api/conversations/{cid}/resolve', json={'resolved': True, 'reason': 'Comprou', 'survey': True}).json()
        assert r['survey'] == 'enviada' and len(sent) == n0 + 1 and 'nota de 0 a 10' in sent[-1]['text']['body'] and 'Marina' in sent[-1]['text']['body']
        inbound(c, '5511977770001', 'Marina Alves', '9 adorei o atendimento')
        assert 'Obrigada pela nota, Marina' in sent[-1]['text']['body']
        inbound(c, '5511977770001', 'Marina Alves', '3')  # depois da nota, número volta a ser conversa normal
        rep = c.get('/api/reports/closing?days=30').json()
        assert rep['ratings'] == 1 and rep['avg'] == 9 and rep['nps'] == 100 and rep['sent'] == 1
        assert rep['reasons'] == [{'reason': 'Comprou', 'n': 1}] and rep['recent'][0]['comment'] == 'adorei o atendimento'
        msgs = c.get(f'/api/conversations/{cid}').json()
        ms = msgs if isinstance(msgs, list) else (msgs.get('items') or msgs.get('messages') or [])
        txt = ' '.join(m.get('text') or '' for m in ms)
        assert 'encerrou: Comprou' in txt and 'Nota do cliente: 9 de 10' in txt
        # sem pesquisa ligada: só motivo
        c.put('/api/closing/settings', json={'csat': False})
        assert c.post(f'/api/conversations/{cid}/resolve', json={'resolved': True, 'reason': 'Agendou', 'survey': True}).json()['survey'] is None
