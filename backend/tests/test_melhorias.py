import os, json, hmac, hashlib, time
from datetime import timedelta
os.environ.update(MONGO_URL='mock', ADMIN_EMAIL='admin@oinanda.com.br', META_APP_SECRET='segredo', WA_VERIFY_TOKEN='verif')
from cryptography.fernet import Fernet
os.environ.setdefault('SECRET_KEY', Fernet.generate_key().decode())
from fastapi.testclient import TestClient
import whatsapp, ai, automations, reports
from core import db, now
from main import app

def hook(c, value, field='messages'):
    raw = json.dumps({'entry': [{'changes': [{'field': field, 'value': value}]}]}).encode()
    return c.post('/api/wa/webhook', content=raw, headers={'x-hub-signature-256': 'sha256=' + hmac.new(b'segredo', raw, hashlib.sha256).hexdigest()})

def inbound(c, pnid, wa, text, name='Cliente', referral=None):
    m = {'from': wa, 'id': 'w' + str(time.time_ns()), 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}
    if referral: m['referral'] = referral
    return hook(c, {'metadata': {'phone_number_id': pnid}, 'contacts': [{'wa_id': wa, 'profile': {'name': name}}], 'messages': [m]})

class FakeMeta:
    def __init__(self): self.sent, self.created, self.calls = [], [], []
    async def __call__(self, method, path, token, payload=None, params=None):
        self.calls.append((method, path, payload, params))
        if path.endswith('/messages'):
            self.sent.append(payload); return {'messages': [{'id': f'out{len(self.sent)}'}]}
        if path.endswith('/message_templates') and method == 'POST':
            self.created.append(payload); return {'id': 'tpl1', 'status': 'PENDING', 'category': payload['category']}
        if path.endswith('/message_templates') and method == 'GET':
            return {'data': [{'name': c['name'], 'status': 'APPROVED', 'language': 'pt_BR', 'category': c['category'], 'id': 'tpl1', 'components': c['components']} for c in self.created]
                    + [{'name': 'boas_vindas', 'status': 'APPROVED', 'language': 'pt_BR', 'category': 'MARKETING', 'id': 'x9', 'components': [{'type': 'BODY', 'text': 'Olá {{1}}, tudo bem?'}]}]}
        if 'subscribed_apps' in path: return {'success': True}
        return {'display_phone_number': '+55 11 90000-0000', 'verified_name': 'Teste'}

def setup(c, email, segment, pnid):
    c.post('/api/auth/signup', json={'name': 'Dona Teste', 'email': email, 'password': 'senha-forte-1', 'company': 'Imob Centro', 'segment': segment})
    c.post('/api/wa/connect', json={'phone_number_id': pnid, 'waba_id': pnid + '9', 'token': 'EAA' + 'q' * 30})

def test_nichos_e_automacoes(monkeypatch):
    meta = FakeMeta()
    monkeypatch.setattr(whatsapp, 'graph', meta)
    monkeypatch.setattr(automations, 'in_quiet', lambda company: False)
    with TestClient(app, base_url='https://testserver') as c:
        keys = [n['key'] for n in c.get('/api/niches').json()]
        assert {'imobiliaria', 'salao', 'loja', 'restaurante', 'servicos', 'escola'} <= set(keys)
        setup(c, 'imob@x.com', 'imobiliaria', '7770001')
        stages = c.get('/api/funnel/stages').json()
        assert [s['name'] for s in stages][:3] == ['Novo lead', 'Qualificado', 'Visita marcada']
        assert c.get('/api/agenda/settings').json()['services'][0]['name'] == 'Visita ao imóvel'
        assert 'bairro' in c.get('/api/ai/example').json()['qualify'].lower()
        # sugestões do nicho e ativação
        d = c.get('/api/automations').json()
        rec = {r['key']: r for r in d['recipes']}
        assert {'lembrete_24h', 'esfriou_1', 'pos_visita'} <= set(rec)
        a1 = c.post('/api/automations', json={'recipe': 'esfriou_1'}).json()
        assert a1['active'] and a1['trigger'] == 'no_reply'
        assert c.post('/api/automations', json={'recipe': 'esfriou_1'}).status_code == 409
        # cliente chega por anúncio, o time responde, o cliente some
        inbound(c, '7770001', '5511911110000', 'Oi, vi o anúncio do apartamento', 'Marina Lopes',
                referral={'source_type': 'ad', 'source_id': '12345', 'headline': 'Apê 2 quartos Centro', 'ctwa_clid': 'abc'})
        conv = c.get('/api/conversations').json()[0]
        assert conv['source'] == 'anuncio' and conv['ad']['headline'] == 'Apê 2 quartos Centro'
        c.post(f'/api/conversations/{conv["id"]}/send', json={'text': 'Oi Marina! Qual bairro você procura?'})
        async def age(hours):  # volta o relógio da conversa
            past = now() - timedelta(hours=hours)
            await db.contacts.update_one({'id': conv['id']}, {'$set': {'last_in_at': past, 'stage_at': past, 'last_at': past}})
        c.portal.call(age, 10)
        assert c.portal.call(automations.run_once) == 0  # ainda não deu 24 h
        c.portal.call(age, 23.5)  # janela aberta ainda: vai mensagem normal
        assert c.portal.call(automations.run_once) == 0
        c.portal.call(age, 24.5)  # passou de 24 h: janela fechada e sem modelo, a vez é pulada
        n0 = len(meta.sent)
        assert c.portal.call(automations.run_once) == 1 and len(meta.sent) == n0
        runs = c.get('/api/automations/runs').json()
        assert runs[0]['status'] == 'skipped' and 'sem modelo' in runs[0]['detail'] and runs[0]['contact_name'] == 'Marina Lopes'
        assert c.portal.call(automations.run_once) == 0  # não repete
        # com modelo aprovado sai pelo modelo
        t = c.post(f'/api/automations/{a1["id"]}/template').json()
        body = meta.created[-1]['components'][0]
        assert body['text'].startswith('Oi, {{1}}!') and body['example']['body_text'] == [['Ana']] and t['status'] == 'PENDING'
        tpls = c.get('/api/templates?sync=1').json()
        assert {x['name'] for x in tpls} >= {t['name'], 'boas_vindas'} and all(x['status'] == 'APPROVED' for x in tpls)
        async def clear(): await db.automation_runs.delete_many({})
        c.portal.call(clear)
        assert c.portal.call(automations.run_once) == 1
        last = meta.sent[-1]
        assert last['type'] == 'template' and last['template']['components'][0]['parameters'][0]['text'] == 'Marina'
        msgs = c.get(f'/api/conversations/{conv["id"]}').json()['messages']
        assert msgs[-1]['text'].startswith('Oi, Marina!') and msgs[-1]['by'].startswith('Automação')
        # o time chama de volta com modelo externo (variáveis por posição)
        ext = next(x for x in tpls if x['name'] == 'boas_vindas')
        r = c.post(f'/api/conversations/{conv["id"]}/template', json={'template_id': ext['id'], 'values': ['Marina']})
        assert r.status_code == 200 and r.json()['text'] == 'Olá Marina, tudo bem?'
        # modelo começando com variável é barrado
        assert c.post('/api/templates', json={'label': 'Errado', 'body': '{primeiro_nome}, oi tudo bem com você'}).status_code == 400
        # cliente pede para parar
        inbound(c, '7770001', '5511911110000', 'PARAR')
        assert c.get(f'/api/conversations/{conv["id"]}').json()['contact']['opt_out']
        assert c.post(f'/api/conversations/{conv["id"]}/template', json={'template_id': ext['id']}).status_code == 400
        # lembrete antes da visita
        a2 = c.post('/api/automations', json={'recipe': 'lembrete_24h'}).json()
        c.post(f'/api/automations/{a2["id"]}/template'); c.get('/api/templates?sync=1')
        cid2 = c.post('/api/contacts', json={'name': 'Rui Prado', 'phone': '11922223333'}).json()['id']
        async def appt():
            from agenda import ACTIVE
            st = now() + timedelta(hours=20)
            await db.appointments.insert_one({'id': 'ap1', 'company_id': conv['company_id'], 'contact_id': cid2, 'service_id': 'x', 'service_name': 'Visita ao imóvel',
                                              'start': st, 'end': st + timedelta(hours=1), 'status': 'scheduled', 'created_at': now() - timedelta(days=3)})
        c.portal.call(appt)
        c.portal.call(automations.run_once)
        rem = meta.sent[-1]
        assert rem['to'] == '5511922223333' and [p['text'] for p in rem['template']['components'][0]['parameters']][0] == 'Rui'
        # automação própria e desligar
        stg = c.get('/api/funnel/stages').json()
        own = c.post('/api/automations', json={'name': 'Qualificado: mandar fotos', 'trigger': 'stage_enter', 'hours': 0, 'stage_id': stg[1]['id'],
                                               'message': 'Oi, {primeiro_nome}! Separei fotos de imóveis no seu perfil.'}).json()
        assert own['active']
        assert c.patch(f'/api/automations/{own["id"]}', json={'active': False}).json()['active'] is False
        assert c.post('/api/automations', json={'name': 'Sem etapa', 'trigger': 'stage_enter', 'hours': 1, 'message': 'Oi, tudo bem por aí?'}).status_code == 400

def test_nanda_blocos_testes_e_relatorios(monkeypatch):
    meta = FakeMeta(); seen = {}
    monkeypatch.setattr(whatsapp, 'graph', meta)
    async def fake_llm(system, messages, max_tokens=500):
        seen['system'] = system
        last = messages[-1]['content'].lower()
        if 'TIME ·' in messages[-1]['content']:
            return json.dumps({'score': 7.5, 'summary': 'Respondeu rápido, mas não marcou a visita.', 'good': ['Cordial'], 'improve': ['Oferecer horário'], 'lost_opportunity': True}), {'input_tokens': 10}
        if 'desconto' in last: return json.dumps({'reply': 'Vou chamar o time!', 'handoff': True, 'reason': 'desconto'}), {}
        return json.dumps({'reply': 'O corte custa R$ 50.', 'handoff': False}), {}
    monkeypatch.setattr(ai, 'llm', fake_llm); monkeypatch.setenv('ANTHROPIC_API_KEY', 'x')
    with TestClient(app, base_url='https://testserver') as c:
        setup(c, 'barba@x.com', 'salao', '8880001')
        c.put('/api/ai/settings', json={'about': 'Barbearia do Zé', 'offer': 'Corte R$ 50', 'qualify': 'Qual serviço?', 'rules': 'Não dar desconto', 'goal': 'Marcar horário'})
        s = c.get('/api/ai/settings').json(); assert s['taught']
        c.post('/api/ai/test', json={'messages': [{'dir': 'in', 'text': 'quanto é?'}]})
        for part in ('Sobre o negócio:\nBarbearia do Zé', 'O que vende e preços:\nCorte R$ 50', 'SEU OBJETIVO NA CONVERSA: Marcar horário', 'Qual serviço?', 'REGRAS DESTE NEGÓCIO:\nNão dar desconto'):
            assert part in seen['system']
        c.post('/api/ai/tests', json={'name': 'Preço', 'messages': [{'dir': 'in', 'text': 'quanto custa o corte?'}], 'expect': 'reply', 'contains': 'R$ 50'})
        c.post('/api/ai/tests', json={'name': 'Desconto', 'messages': [{'dir': 'in', 'text': 'faz desconto?'}], 'expect': 'handoff'})
        c.post('/api/ai/tests', json={'name': 'Errado', 'messages': [{'dir': 'in', 'text': 'quanto é?'}], 'expect': 'reply', 'not_contains': 'R$ 50'})
        r = c.post('/api/ai/tests/run').json()
        assert r['total'] == 3 and r['passed'] == 2
        assert next(t for t in r['tests'] if t['name'] == 'Errado')['last']['why'].startswith('Falou o que não devia')
        # relatório e nota
        inbound(c, '8880001', '5511933334444', 'Oi, tem horário amanhã?', 'Caio Mendes')
        conv = c.get('/api/conversations').json()[0]
        c.post(f'/api/conversations/{conv["id"]}/send', json={'text': 'Tem sim, Caio! 10h ou 15h?'})
        won = next(s for s in c.get('/api/funnel/stages').json() if s['kind'] == 'won')
        c.patch(f'/api/contacts/{conv["id"]}', json={'stage': won['id'], 'value': 75})
        q = c.post(f'/api/quality/{conv["id"]}').json()
        assert q['score'] == 7.5 and q['lost_opportunity'] and len(q['agents']) == 1
        rep = c.get('/api/reports/sales?days=30').json()
        t = rep['totals']
        assert t['new_contacts'] == 1 and t['won'] == 1 and t['won_value'] == 75 and t['conversion'] == 100.0 and t['first_reply_min'] is not None
        assert rep['funnel'][0]['count'] == 1 and rep['funnel'][-1]['count'] == 1
        me = next(p for p in rep['people'] if p['name'] == 'Dona Teste')
        assert me['won'] == 1 and me['value'] == 75 and me['score'] == 7.5
        assert rep['origin'][0]['name'] == 'Chamou no WhatsApp' and rep['origin'][0]['won'] == 1
        assert len(rep['series']) == 30 and sum(x['new'] for x in rep['series']) == 1 and sum(x['value'] for x in rep['series']) == 75
        assert rep['prev']['new_contacts'] == 0 and rep['funnel'][1]['rate'] == 100 and isinstance(rep['alerts'], list)
        assert len(c.get('/api/reports/sales?days=90').json()['series']) == 13
        ql = c.get('/api/quality').json(); assert ql['avg'] == 7.5 and ql['lost'] == 1
        assert c.post('/api/quality-run').json()['scored'] == 0  # já avaliada e sem mensagem nova
