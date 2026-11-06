import os, json, hmac, hashlib, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, roi
from main import app

def hook(c, wa, name, text, ad=None):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    if ad: m['referral'] = ad
    raw = json.dumps({'entry': [{'changes': [{'field': 'messages', 'value': {'metadata': {'phone_number_id': '7770011'}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]}}]}]}).encode()
    c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def test_parse():
    assert roi.num('R$ 1.234,56') == 1234.56 and roi.num('1,234.56') == 1234.56 and roi.num('89,9') == 89.9 and roi.num('') is None
    assert roi.day('05/11/2026') == '2026-11-05' and roi.day('2026-11-05') == '2026-11-05'
    s = {'start': '2026-11-01', 'end': '2026-11-10', 'amount': 100}
    from datetime import date
    assert roi.overlap(s, date(2026, 11, 6), date(2026, 11, 30)) == 50 and roi.overlap(s, date(2026, 12, 1), date(2026, 12, 2)) == 0

def test_retorno(monkeypatch):
    async def fake_graph(method, path, token, payload=None, params=None):
        if path.endswith('/messages'): return {'messages': [{'id': 'o1'}]}
        return {'success': True, 'display_phone_number': '+55'}
    monkeypatch.setattr(whatsapp, 'graph', fake_graph); monkeypatch.setattr(whatsapp, 'ON_INBOUND', None)
    with TestClient(app, base_url='https://testserver') as c:
        c.post('/api/auth/signup', json={'name': 'Dona R', 'email': 'dr@x.com', 'password': 'senha-forte-1', 'company': 'Clínica R', 'segment': 'clinica'})
        c.post('/api/wa/connect', json={'phone_number_id': '7770011', 'waba_id': '7770012', 'token': 'EAA' + 'q' * 30})
        A = {'source_type': 'ad', 'source_id': '120001', 'headline': 'Botox novembro'}
        B = {'source_type': 'ad', 'source_id': '120002', 'headline': 'Limpeza de pele'}
        hook(c, '5511980000001', 'Ana', 'oi', A); hook(c, '5511980000002', 'Bia', 'oi', A); hook(c, '5511980000003', 'Cris', 'oi', B)
        hook(c, '5511980000004', 'Duda', 'oi')
        cv = {x['name']: x['id'] for x in c.get('/api/conversations').json()}
        c.put(f"/api/contacts/{cv['Duda']}/origin", json={'origin': 'evento', 'detail': 'Feira Saúde'})
        today = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
        d = lambda n: (today - timedelta(days=n)).isoformat()
        c.post('/api/sales', json={'contact_id': cv['Ana'], 'date': d(0), 'items': [{'name': 'Botox', 'price': 1200}]})
        c.post('/api/sales', json={'contact_id': cv['Duda'], 'date': d(0), 'items': [{'name': 'Avaliação', 'price': 300}]})
        op = c.get('/api/spend/options').json()
        assert {a['id'] for a in op['ads']} == {'120001', '120002'} and 'Feira Saúde' in op['details']['evento']
        assert c.post('/api/spend', json={'origin': 'anuncio', 'ref_kind': 'ad', 'ref': '120001', 'label': 'Botox novembro', 'start': d(9), 'end': d(0), 'amount': 400}).status_code == 200
        c.post('/api/spend', json={'origin': 'evento', 'ref_kind': 'detail', 'ref': 'feira saúde', 'start': d(0), 'end': d(0), 'amount': 600})
        assert c.post('/api/spend', json={'origin': 'anuncio', 'start': d(0), 'end': d(3), 'amount': 10}).status_code == 422  # fim antes do início
        csv = 'Nome do anúncio;ID do anúncio;Valor usado (BRL);Início dos relatórios;Término dos relatórios\nLimpeza de pele;120002;"150,00";' + d(4) + ';' + d(0) + '\nSem gasto;120003;0;' + d(4) + ';' + d(0) + '\n'
        r = c.post('/api/spend/import', files={'file': ('meta.csv', csv.encode(), 'text/csv')}, data={'origin': 'anuncio'}).json()
        assert r == {'created': 1, 'skipped': 1}
        assert c.post('/api/spend/import', files={'file': ('meta.csv', csv.encode(), 'text/csv')}, data={'origin': 'anuncio'}).json()['created'] == 0  # não duplica
        rp = c.get(f'/api/reports/roi?start={d(29)}&end={d(0)}').json()
        an = next(x for x in rp['rows'] if x['key'] == 'anuncio')
        assert an['leads'] == 3 and an['clients'] == 1 and an['revenue'] == 1200 and an['spend'] == 550 and an['cpl'] == round(550 / 3, 2) and an['cac'] == 550 and an['roas'] == round(1200 / 550, 2)
        bot = next(x for x in an['children'] if x['key'] == 'ad:120001')
        assert bot['name'] == 'Botox novembro' and bot['leads'] == 2 and bot['spend'] == 400 and bot['roas'] == 3
        ev = next(x for x in rp['rows'] if x['key'] == 'evento')
        assert ev['spend'] == 600 and ev['revenue'] == 300 and ev['result'] == -300 and ev['children'][0]['leads'] == 1
        assert rp['total']['spend'] == 1150 and rp['total']['leads'] == 4 and rp['paid']['revenue'] == 1500
        # período que pega só metade do investimento do botox
        rp2 = c.get(f'/api/reports/roi?start={d(4)}&end={d(0)}').json()
        assert next(x for x in rp2['rows'] if x['key'] == 'anuncio')['children'][0]['spend'] in (200, 150)
        sid = c.get('/api/spend').json()[0]['id']
        assert c.delete(f'/api/spend/{sid}').status_code == 200
