"""Avisos para o dono do Oi Nanda: a jornada de cada empresa, de lead a cliente pagante, e os pagamentos.

Por enquanto só por e-mail (o WhatsApp entra quando o número novo estiver conectado). Cada aviso liga e desliga em
Admin › Avisos para mim. O ciclo das automações chama tick() a cada volta: ele compara a situação de cada empresa com
a última vista (owner_state) e manda um e-mail por novidade. Na primeira volta só guarda a foto, sem disparar nada.
Contas de teste e a própria conta do admin ficam de fora. Os novos contatos do WhatsApp/Instagram do Oi Nanda
(a conta do admin) chegam juntos, no máximo um e-mail a cada 15 minutos.
"""
import os, html
from datetime import timedelta, datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel, Field
from core import db, now, uid, audit, admin, ADMIN_EMAIL
from whatsapp import aware
import mailer

router = APIRouter()
SP = ZoneInfo('America/Sao_Paulo')
SEG = {'clinica': 'Clínica e estética', 'salao': 'Salão e barbearia', 'studio': 'Studio e academia', 'escola': 'Escola e cursos', 'servicos': 'Serviços',
       'imobiliaria': 'Imobiliária', 'loja': 'Loja', 'restaurante': 'Restaurante', 'lancamento': 'Infoproduto e lançamento', 'outro': 'Outro'}
PLAN = {'essencial': 'Essencial', 'profissional': 'Profissional', 'equipe': 'Equipe'}

# chave, grupo, nome, quando, ligado de saída
EVENTS = [
    ('lead_conta', 'Leads', 'Nova conta criada', 'Alguém criou a conta e começou o teste grátis. Na hora.', True),
    ('lead_contato', 'Leads', 'Novo contato no WhatsApp ou Instagram do Oi Nanda', 'Juntos, no máximo um e-mail a cada 15 minutos.', True),
    ('ativou_wa', 'Ativação', 'Conectou o WhatsApp', 'O passo que mais leva à assinatura. Na hora.', True),
    ('travado', 'Ativação', 'Parado no começo', 'Criou a conta e não ensinou a Nanda em 48 h, ou está há 5 dias sem entrar no teste.', True),
    ('teste_3d', 'Ativação', 'Teste acaba em 3 dias', 'Ainda não assinou. Bom momento para uma conversa.', True),
    ('teste_fim', 'Ativação', 'Teste acabou sem assinar', 'A conta ficou só para consulta.', True),
    ('assinou', 'Virou cliente', 'Assinou um plano', 'Plano e valor. Na hora.', True),
    ('trocou', 'Virou cliente', 'Trocou de plano', 'Subiu ou desceu de plano.', True),
    ('recusado', 'Pagamentos', 'Pagamento recusado', 'Com o motivo do cartão, quando o Mercado Pago informa.', True),
    ('atraso', 'Pagamentos', 'Conta parou por falta de pagamento', 'Passou o prazo e a conta ficou só para consulta.', True),
    ('cancelou', 'Pagamentos', 'Cancelou a assinatura', 'Vale perguntar o motivo.', True),
    ('resumo_dia', 'Resumos', 'Resumo do dia', 'Todo dia às 8h: o que aconteceu e com quem falar hoje.', True),
    ('resumo_semana', 'Resumos', 'Resumo da semana', 'Segunda às 8h: funil da semana, conversão e faturamento mensal.', True),
]
KEYS = [e[0] for e in EVENTS]
NAME = {e[0]: e[2] for e in EVENTS}

async def cfg():
    c = await db.owner_alerts.find_one({'id': 'cfg'}, {'_id': 0})
    if not c:
        c = {'id': 'cfg', 'emails': [ADMIN_EMAIL] if ADMIN_EMAIL else [], 'off': [], 'started_at': now(), 'contacts_since': now()}
        await db.owner_alerts.insert_one(c.copy())
    return c

def link(cid=None): return f'{mailer.APP}/admin/clientes/{cid}' if cid else f'{mailer.APP}/admin/jornada'
def brl(v): return mailer.money(v or 0)
def dt(d): return aware(d).astimezone(SP).strftime('%d/%m %H:%M') if d else ''
def wa_link(phone):
    d = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if not d: return ''
    if len(d) <= 11: d = '55' + d
    return f'https://wa.me/{d}'

async def send(key, subject, card, cid=None, once=None, force=False):
    """Um e-mail para cada destinatário. once: não repete o mesmo aviso. force: o teste do painel (ignora liga/desliga)."""
    c = await cfg()
    if not force and key in (c.get('off') or []): return False
    if once and await db.owner_log.find_one({'once': once}, {'_id': 1}): return False
    card = {'tone': 'i', 'why': 'Aviso interno do Oi Nanda para a administração. Você liga e desliga cada um em Admin › Avisos para mim.', **card}
    if 'button' not in card: card['button'] = ('Abrir no Admin', link(cid))
    body = mailer.render(card)
    text = '\n'.join([card.get('title', '')] + [html.unescape(p) for p in card.get('body') or []] + [f'{a}: {b}' for a, b in card.get('rows') or []] + [card['button'][1]])
    ok = False
    for to in c.get('emails') or []:
        ok = await mailer.send(to, subject, body, text) or ok
    await db.owner_log.insert_one({'id': uid(), 'key': key, 'name': NAME.get(key, key), 'subject': subject, 'company_id': cid, 'once': once,
                                   'to': c.get('emails') or [], 'ok': ok, 'test': force, 'at': now()})
    return True

def who_rows(r):
    """Linhas padrão de quem é a empresa."""
    c = r['c']; o = r.get('owner') or {}
    return [('Empresa', c.get('name') or ''), ('Ramo', SEG.get(c.get('segment') or 'outro', 'Outro')), ('Responsável', o.get('name') or ''),
            ('E-mail', o.get('email') or ''), ('Telefone', c.get('phone') or ''), ('Conta criada', dt(c.get('created_at')))]

def contact_btn(r):
    w = wa_link(r['c'].get('phone'))
    return {'second': ('Chamar no WhatsApp', w)} if w else {}

async def taught(cid):
    s = await db.ai_settings.find_one({'company_id': cid}, {'_id': 0, 'about': 1, 'offer': 1}) or {}
    if (s.get('about') or '').strip() or (s.get('offer') or '').strip(): return True
    return bool(await db.products.find_one({'company_id': cid, 'active': True, 'price': {'$gt': 0}}, {'_id': 1}))

# ---------- o ciclo ----------
async def tick():
    from adminapi import base
    c = await cfg(); n = now(); start = aware(c.get('started_at')) or n
    rows = await base()
    states = {s['company_id']: s for s in await db.owner_state.find({}, {'_id': 0}).to_list(10000)}
    own = next((r['c']['id'] for r in rows if r['own']), None)
    for r in rows:
        co = r['c']; cid = co['id']
        if r['internal'] or co.get('status') == 'suspended': continue
        b = co.get('billing') or {}
        cur = {'company_id': cid, 'kind': r['kind'], 'plan': r['plan'], 'wa': bool(r['wa'] and r['wa'].get('status') == 'connected'), 'at': n}
        old = states.get(cid)
        if old is None:
            await db.owner_state.insert_one(cur.copy())
            if aware(co.get('created_at')) and aware(co['created_at']) >= start:  # conta nova depois de ligar os avisos
                await send('lead_conta', f'Nova conta: {co.get("name")}', {'eyebrow': 'Lead novo', 'title': f'{co.get("name")} começou o teste grátis',
                           'body': ['Acabou de criar a conta no Oi Nanda. Os primeiros dias decidem: quem conecta o WhatsApp logo costuma assinar.'],
                           'rows': who_rows(r), **contact_btn(r)}, cid, once=f'lead_conta:{cid}')
            continue
        # conectou o WhatsApp
        if cur['wa'] and not old.get('wa'):
            await send('ativou_wa', f'{co.get("name")} conectou o WhatsApp', {'tone': 'g', 'eyebrow': 'Ativação', 'title': f'{co.get("name")} conectou o WhatsApp',
                       'body': ['As conversas dos clientes já chegam no Oi Nanda e a Nanda pode atender. É o passo que mais leva à assinatura.'],
                       'rows': who_rows(r) + [('Número', (r['wa'] or {}).get('display_phone') or '')]}, cid, once=f'ativou_wa:{cid}')
        # assinou / trocou de plano
        if r['kind'] == 'paying' and old.get('kind') != 'paying':
            await send('assinou', f'Novo cliente: {co.get("name")} · {PLAN.get(r["plan"], r["plan"])}', {'tone': 'g', 'eyebrow': 'Virou cliente',
                       'title': f'{co.get("name")} assinou o {PLAN.get(r["plan"], r["plan"])}', 'fig': (brl(b.get('amount')), 'por mês' if (b.get('cycle') or 'monthly') == 'monthly' else 'por ano'),
                       'body': ['A assinatura foi confirmada pelo Mercado Pago.'], 'rows': who_rows(r)}, cid, once=f'assinou:{cid}:{b.get("sub_id")}')
        elif r['kind'] == 'paying' and old.get('kind') == 'paying' and r['plan'] != old.get('plan'):
            order = list(PLAN)
            up = order.index(r['plan']) > order.index(old['plan']) if old.get('plan') in PLAN and r['plan'] in PLAN else True
            await send('trocou', f'{co.get("name")} {"subiu" if up else "mudou"} para o {PLAN.get(r["plan"], r["plan"])}', {'tone': 'g' if up else 'a', 'eyebrow': 'Troca de plano',
                       'title': f'{co.get("name")}: {PLAN.get(old.get("plan"), old.get("plan"))} → {PLAN.get(r["plan"], r["plan"])}', 'fig': (brl(b.get('amount')), 'novo valor'),
                       'rows': who_rows(r)}, cid, once=f'trocou:{cid}:{old.get("plan")}:{r["plan"]}:{n:%Y%m%d}')
        # parou por falta de pagamento
        if r['kind'] == 'readonly' and old.get('kind') == 'paying':
            await send('atraso', f'Conta parada por falta de pagamento: {co.get("name")}', {'tone': 'r', 'eyebrow': 'Pagamento em atraso',
                       'title': f'{co.get("name")} ficou só para consulta', 'body': ['O prazo do pagamento passou e a conta parou. A Nanda não atende os clientes dela até regularizar.'],
                       'rows': who_rows(r) + [('Plano', PLAN.get(old.get('plan'), old.get('plan') or ''))], **contact_btn(r)}, cid, once=f'atraso:{cid}:{n:%Y%m}')
        # teste acabou sem assinar
        if r['kind'] == 'readonly' and old.get('kind') == 'trial':
            await send('teste_fim', f'Teste acabou sem assinar: {co.get("name")}', {'tone': 'a', 'eyebrow': 'Teste acabou',
                       'title': f'{co.get("name")} não assinou', 'body': ['Os 14 dias acabaram e a conta ficou só para consulta. ' + ('Chegou a conectar o WhatsApp.' if cur['wa'] else 'Não chegou a conectar o WhatsApp.')],
                       'rows': who_rows(r), **contact_btn(r)}, cid, once=f'teste_fim:{cid}')
        await db.owner_state.update_one({'company_id': cid}, {'$set': cur})
        # cancelou
        ca = aware(b.get('cancelled_at'))
        if b.get('status') == 'cancelled' and ca and ca >= start:
            await send('cancelou', f'Cancelou: {co.get("name")}', {'tone': 'r', 'eyebrow': 'Cancelamento', 'title': f'{co.get("name")} cancelou a assinatura',
                       'body': [f'Continua usando até {aware(b.get("paid_until")).astimezone(SP):%d/%m}.' if b.get('paid_until') else 'Vale perguntar o motivo.'],
                       'rows': who_rows(r) + [('Plano', PLAN.get(r['plan'], r['plan'] or ''))], **contact_btn(r)}, cid, once=f'cancelou:{cid}:{b.get("sub_id")}')
        # teste: 3 dias para acabar / parado
        te = aware(co.get('trial_ends_at'))
        if r['kind'] == 'trial' and te and n < te <= n + timedelta(days=3):
            await send('teste_3d', f'Teste acaba em 3 dias: {co.get("name")}', {'tone': 'a', 'eyebrow': 'Teste acabando', 'title': f'O teste de {co.get("name")} acaba em {te.astimezone(SP):%d/%m}',
                       'body': ['Ainda não assinou. ' + ('Já conectou o WhatsApp: vale uma conversa sobre o plano.' if cur['wa'] else 'Ainda sem WhatsApp conectado: vale ajudar a conectar.')],
                       'rows': who_rows(r), **contact_btn(r)}, cid, once=f'teste_3d:{cid}')
        ca0 = aware(co.get('created_at'))
        if r['kind'] == 'trial' and ca0 and n - timedelta(days=4) < ca0 <= n - timedelta(hours=48) and not await taught(cid):
            await send('travado', f'Parado no começo: {co.get("name")}', {'tone': 'a', 'eyebrow': 'Precisa de ajuda', 'title': f'{co.get("name")} ainda não ensinou a Nanda',
                       'body': ['Criou a conta há 2 dias e não preencheu preços nem informações. Sem isso, a Nanda não consegue atender. Uma mensagem sua agora faz diferença.'],
                       'rows': who_rows(r), **contact_btn(r)}, cid, once=f'travado48:{cid}')
        lu = r.get('last_use')
        if r['kind'] == 'trial' and lu and lu <= n - timedelta(days=5) and ca0 and ca0 <= n - timedelta(days=5):
            await send('travado', f'Sumiu do teste: {co.get("name")}', {'tone': 'a', 'eyebrow': 'Precisa de ajuda', 'title': f'{co.get("name")} não entra há {(n - lu).days} dias',
                       'body': ['Está no teste grátis e parou de usar. Vale perguntar se travou em algum passo.'],
                       'rows': who_rows(r) + [('Último acesso', dt(lu))], **contact_btn(r)}, cid, once=f'travado_idle:{cid}')
    # pagamentos recusados (um aviso por cobrança)
    for p in await db.payments.find({'status': 'refused', 'due': {'$gte': max(start, n - timedelta(days=10)) - timedelta(days=3)}}, {'_id': 0}).to_list(500):
        r = next((x for x in rows if x['c']['id'] == p['company_id']), None)
        if not r or r['internal']: continue
        from adminapi import reason
        why = reason(p.get('detail'))
        await send('recusado', f'Pagamento recusado: {r["c"].get("name")} · {brl(p.get("amount"))}', {'tone': 'r', 'eyebrow': 'Pagamento recusado',
                   'title': f'O pagamento de {r["c"].get("name")} não passou', 'fig': (brl(p.get('amount')), why or 'recusado pelo cartão'),
                   'body': ['O Mercado Pago tenta de novo e o cliente recebe o aviso para trocar o cartão. Se não resolver no prazo, a conta para.'],
                   'rows': who_rows(r), **contact_btn(r)}, p['company_id'], once=f'recusado:{p["id"]}')
    # novos contatos no WhatsApp/Instagram do Oi Nanda (juntos, no máximo um e-mail a cada 15 min)
    if own:
        since = aware(c.get('contacts_since')) or n
        last = await db.owner_log.find_one({'key': 'lead_contato'}, {'_id': 0, 'at': 1}, sort=[('at', -1)])
        if not last or aware(last['at']) <= n - timedelta(minutes=15):
            new = await db.contacts.find({'company_id': own, 'created_at': {'$gt': since}, 'internal': {'$ne': True}}, {'_id': 0}).sort('created_at', 1).to_list(50)
            if new:
                rows_ = [(x.get('name') or 'Sem nome', ('Instagram' if x.get('ig_id') and not x.get('wa_id') else 'WhatsApp') + ' · ' + dt(x.get('created_at')) + (f" · {x.get('last_text')[:60]}" if x.get('last_text') else '')) for x in new[:15]]
                await send('lead_contato', f'{len(new)} {"novo contato" if len(new) == 1 else "novos contatos"} no Oi Nanda', {'eyebrow': 'Lead novo',
                           'title': f'{len(new)} {"pessoa chamou" if len(new) == 1 else "pessoas chamaram"} o Oi Nanda', 'body': ['Chegaram pelo WhatsApp ou Instagram do Oi Nanda.'],
                           'rows': rows_, 'button': ('Abrir as conversas', f'{mailer.APP}/conversas')})
                await db.owner_alerts.update_one({'id': 'cfg'}, {'$set': {'contacts_since': aware(new[-1]['created_at'])}})
    await digests(rows)

async def digests(rows):
    """Resumo diário (8h) e semanal (segunda, 8h), no horário de São Paulo."""
    n = now(); sp = n.astimezone(SP)
    if sp.hour < 8: return
    paying = [r for r in rows if not r['internal'] and r['kind'] == 'paying']
    mrr = sum(r.get('mrr') or 0 for r in paying)
    def made(r, since): return aware(r['c'].get('created_at')) and aware(r['c']['created_at']) >= since
    # com quem falar: teste acabando, parado, recusado, cancelou
    talk = []
    for r in rows:
        if r['internal']: continue
        co = r['c']; te = aware(co.get('trial_ends_at'))
        if r.get('refused'): talk.append((co.get('name'), 'pagamento recusado'))
        elif r['kind'] == 'trial' and te and te <= n + timedelta(days=3): talk.append((co.get('name'), f'teste acaba {te.astimezone(SP):%d/%m}' + ('' if r['wa'] else ', sem WhatsApp')))
        elif r['kind'] == 'trial' and r.get('last_use') and r['last_use'] <= n - timedelta(days=5): talk.append((co.get('name'), f'sem entrar há {(n - r["last_use"]).days} dias'))
    day0 = n - timedelta(hours=24)
    paid = await db.payments.find({'status': 'paid', 'paid_at': {'$gte': day0}}, {'_id': 0, 'amount': 1}).to_list(1000)
    own = next((r['c']['id'] for r in rows if r['own']), None)
    contacts = await db.contacts.count_documents({'company_id': own, 'created_at': {'$gte': day0}}) if own else 0
    newc = [r for r in rows if not r['internal'] and made(r, day0)]
    await send('resumo_dia', f'Oi Nanda hoje: {len(newc)} {"conta nova" if len(newc) == 1 else "contas novas"} · {len(talk)} para conversar', {'eyebrow': f'Resumo de {sp:%d/%m}',
               'title': 'Como foi o dia no Oi Nanda', 'fig': (brl(mrr), f'faturamento mensal · {len(paying)} {"cliente" if len(paying) == 1 else "clientes"} pagando'),
               'rows': [('Contas novas', str(len(newc))), ('Contatos no WhatsApp/Instagram', str(contacts)),
                        ('Recebido nas últimas 24 h', brl(sum(p.get('amount') or 0 for p in paid))), ('Em teste agora', str(sum(1 for r in rows if not r['internal'] and r['kind'] == 'trial')))]
                       + [(f'Falar com {a}', b) for a, b in talk[:10]],
               'body': ['Ninguém precisa de contato hoje.'] if not talk else [f'<b>{len(talk)}</b> {"empresa precisa" if len(talk) == 1 else "empresas precisam"} de uma conversa hoje. Estão na lista abaixo.'],
               'button': ('Abrir a jornada dos clientes', link())}, once=f'resumo_dia:{sp:%Y-%m-%d}')
    if sp.weekday() == 0:
        wk = n - timedelta(days=7)
        made_w = [r for r in rows if not r['internal'] and made(r, wk)]
        act = sum(1 for r in made_w if r['wa'] and r['wa'].get('status') == 'connected')
        paid_w = sum(1 for r in made_w if r['kind'] == 'paying')
        canc = sum(1 for r in rows if not r['internal'] and aware((r['c'].get('billing') or {}).get('cancelled_at')) and aware(r['c']['billing']['cancelled_at']) >= wk)
        pct = lambda a, b: f'{round(100 * a / b)}%' if b else '—'
        await send('resumo_semana', f'Semana do Oi Nanda: {len(made_w)} contas · {paid_w} {"assinatura" if paid_w == 1 else "assinaturas"}', {'eyebrow': 'Resumo da semana',
                   'title': 'O funil dos últimos 7 dias', 'fig': (brl(mrr), 'faturamento mensal recorrente'),
                   'steps': [(1, f'{len(made_w)} contas criadas', 'começaram o teste grátis'), (1, f'{act} conectaram o WhatsApp', pct(act, len(made_w)) + ' das contas novas'),
                             (1, f'{paid_w} assinaram', pct(paid_w, len(made_w)) + ' das contas novas'), (0, f'{canc} cancelaram', 'na semana')],
                   'button': ('Abrir a jornada dos clientes', link())}, once=f'resumo_semana:{sp:%Y-%m-%d}')

# ---------- painel ----------
class AlertsIn(BaseModel):
    emails: Optional[List[str]] = Field(default=None, max_length=5)
    off: Optional[List[str]] = None

@router.get('/admin/alerts')
async def get_alerts(user=Depends(admin)):
    c = await cfg()
    log = await db.owner_log.find({}, {'_id': 0}).sort('at', -1).to_list(30)
    counts = {}
    for l in await db.owner_log.find({'at': {'$gte': now() - timedelta(days=30)}, 'test': {'$ne': True}}, {'_id': 0, 'key': 1}).to_list(5000): counts[l['key']] = counts.get(l['key'], 0) + 1
    return {'emails': c.get('emails') or [], 'events': [{'key': k, 'group': g, 'name': nm, 'when': w, 'on': k not in (c.get('off') or []), 'sent30': counts.get(k, 0)} for k, g, nm, w, _ in EVENTS],
            'log': log, 'mail_ready': mailer.ready(), 'wa_ready': False}

@router.put('/admin/alerts')
async def put_alerts(body: AlertsIn, request: Request, user=Depends(admin)):
    upd = {}
    if body.emails is not None:
        em = []
        for e in body.emails:
            e = e.strip().lower()
            if not e: continue
            if '@' not in e or '.' not in e.split('@')[-1] or len(e) > 120: raise HTTPException(400, f'E-mail inválido: {e}')
            if e not in em: em.append(e)
        if not em: raise HTTPException(400, 'Deixe pelo menos um e-mail para receber os avisos.')
        upd['emails'] = em
    if body.off is not None: upd['off'] = [k for k in body.off if k in KEYS]
    await cfg()
    if upd: await db.owner_alerts.update_one({'id': 'cfg'}, {'$set': upd})
    await audit(request, user, 'Ajustou os avisos para o admin', ', '.join(upd))
    return await get_alerts(user)

@router.post('/admin/alerts/test')
async def test_alert(user=Depends(admin)):
    await send('lead_conta', 'Teste: assim chega um aviso do Oi Nanda', {'eyebrow': 'Teste', 'title': 'Os avisos estão funcionando',
               'body': ['Este é um e-mail de teste. Quando uma conta nova for criada, um cliente assinar ou um pagamento for recusado, chega assim.'],
               'rows': [('Empresa', 'Clínica Exemplo'), ('Ramo', 'Clínica e estética'), ('Telefone', '(11) 99999-0000')]}, force=True)
    return {'ok': True}

# ---------- jornada (funil) ----------
STAGES = [('lead', 'Leads'), ('testando', 'Testando'), ('ativou', 'Ativou'), ('pagante', 'Pagante'), ('risco', 'Em risco'), ('saiu', 'Saiu')]

@router.get('/admin/journey')
async def journey(user=Depends(admin)):
    from adminapi import base
    rows = await base(); n = now(); out = {k: [] for k, _ in STAGES}
    for r in rows:
        if r['internal'] or r['c'].get('status') == 'suspended': continue
        co = r['c']; b = co.get('billing') or {}; wa = bool(r['wa'] and r['wa'].get('status') == 'connected'); te = aware(co.get('trial_ends_at'))
        ca = aware(co.get('created_at')); lu = r.get('last_use'); since = None; hint = ''
        if r['kind'] == 'paying':
            if r.get('refused'): st, hint = 'risco', 'Pagamento recusado'
            elif b.get('status') == 'cancelled': st, hint = 'risco', 'Cancelou, usa até ' + (aware(b['paid_until']).astimezone(SP).strftime('%d/%m') if b.get('paid_until') else 'o fim do período')
            else: st, hint = 'pagante', PLAN.get(r['plan'], r['plan'] or '') + ' · ' + brl(b.get('amount'))
            since = aware(b.get('since')) or ca
        elif r['kind'] in ('trial', 'courtesy'):
            st = 'ativou' if wa else 'testando'; since = ca
            if te and te <= n + timedelta(days=3): hint = f'Teste acaba {te.astimezone(SP):%d/%m}'
            elif lu and lu <= n - timedelta(days=5): hint = f'Sem entrar há {(n - lu).days} dias'
            elif not wa: hint = 'Ajudar a conectar o WhatsApp'
            else: hint = 'Falar sobre o plano'
            if r['kind'] == 'trial' and te and te <= n + timedelta(days=3) and not wa: st = 'risco'
        elif r['kind'] == 'readonly':
            st = 'saiu'; since = te or ca; hint = 'Parou de pagar' if b.get('sub_id') else 'Teste acabou sem assinar'
        else:
            st = 'lead'; since = ca; hint = 'Criou a conta, falta começar o teste'
        out[st].append({'id': co['id'], 'name': co.get('name'), 'segment': SEG.get(co.get('segment') or 'outro', 'Outro'), 'phone': co.get('phone'), 'wa_link': wa_link(co.get('phone')),
                        'days': (n - since).days if since else None, 'hint': hint, 'mrr': r.get('mrr') or 0, 'wa': wa, 'created_at': ca})
    own = next((r['c']['id'] for r in rows if r['own']), None)
    leads = []
    if own:  # quem chamou o Oi Nanda nos últimos 30 dias e ainda não tem conta (pelo telefone)
        phones = {''.join(ch for ch in str(r['c'].get('phone') or '') if ch.isdigit())[-8:] for r in rows if r['c'].get('phone')}
        for x in await db.contacts.find({'company_id': own, 'created_at': {'$gte': n - timedelta(days=30)}, 'internal': {'$ne': True}}, {'_id': 0}).sort('created_at', -1).to_list(100):
            ph = ''.join(ch for ch in str(x.get('wa_id') or x.get('phone') or '') if ch.isdigit())
            if ph and ph[-8:] in phones: continue
            leads.append({'id': x['id'], 'name': x.get('name') or 'Sem nome', 'segment': 'Instagram' if x.get('ig_id') and not x.get('wa_id') else 'WhatsApp', 'contact': True,
                          'days': (n - aware(x['created_at'])).days if x.get('created_at') else None, 'hint': (x.get('last_text') or '')[:70], 'wa_link': wa_link(x.get('wa_id')) if x.get('wa_id') else ''})
    out['lead'] = leads + out['lead']
    for k in out: out[k].sort(key=lambda x: -(x['days'] or 0) if k in ('testando', 'ativou', 'risco') else (x['days'] or 0))
    tot = {k: len(v) for k, v in out.items()}
    made30 = [r for r in rows if not r['internal'] and aware(r['c'].get('created_at')) and aware(r['c']['created_at']) >= n - timedelta(days=30)]
    act30 = sum(1 for r in made30 if r['wa'] and r['wa'].get('status') == 'connected'); pay30 = sum(1 for r in made30 if r['kind'] == 'paying')
    return {'stages': [{'key': k, 'name': nm, 'items': out[k][:60], 'total': tot[k]} for k, nm in STAGES],
            'kpi': {'mrr': sum(r.get('mrr') or 0 for r in rows if not r['internal']), 'made30': len(made30), 'act30': act30, 'pay30': pay30}}
