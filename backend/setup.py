"""Configurar a conta: um guia só, em etapas, que confere sozinho nos dados o que já foi feito."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from core import db, current_user

router = APIRouter()

STAGES = [
    (1, 'A Nanda atendendo', 'O essencial para ela responder os seus clientes.'),
    (2, 'Vender e agendar', 'Transformar conversa em venda e horário marcado.'),
    (3, 'Seu time', 'Ninguém deixa cliente esperando.'),
    (4, 'Vender mais', 'Diferenciais para quando o básico estiver rodando.'),
]

# chave, etapa, título, por que importa, minutos, pode pular?, texto do "não uso", rota, ação, botão
STEPS = [
    ('sobre', 1, 'Contar sobre o seu negócio', 'O que você faz, onde fica e como atende. É a base de tudo que a Nanda fala.', 3, False, '', 'nanda', '', 'Contar agora'),
    ('precos', 1, 'Ensinar preços e serviços', 'Para ela responder "quanto custa?" certo, sem chamar você.', 5, False, '', 'nanda', '', 'Ensinar a Nanda'),
    ('testar', 1, 'Testar uma conversa', 'Converse como se fosse um cliente e veja como ela responde.', 1, False, '', '', 'comecar', 'Conversar com a Nanda'),
    ('whatsapp', 1, 'Conectar o WhatsApp', 'O número que seus clientes já conhecem, pelo canal oficial da Meta.', 5, False, '', 'empresa', '', 'Conectar'),
    ('modo', 1, 'Escolher como a Nanda trabalha', 'No copiloto ela sugere e você envia. No automático ela responde sozinha.', 1, False, '', 'nanda', 'modo', 'Escolher'),
    ('catalogo', 2, 'Cadastrar produtos ou serviços', 'Preço certo na venda, no comprovante e nas respostas da Nanda.', 5, True, 'Não vendo itens fixos', 'vendas', 'catalogo', 'Abrir o catálogo'),
    ('agenda', 2, 'Ajustar a agenda', 'Horários de atendimento e quem atende. A Nanda marca sozinha.', 5, True, 'Não trabalho com horário marcado', 'agenda', '', 'Ajustar a agenda'),
    ('venda', 2, 'Registrar a primeira venda', 'É assim que os relatórios mostram quanto a Nanda e o time trouxeram.', 1, False, '', '', 'venda', 'Registrar venda'),
    ('instagram', 2, 'Conectar o Instagram', 'As mensagens do Direct chegam junto com as do WhatsApp.', 3, True, 'Não uso Instagram', 'empresa', '', 'Conectar'),
    ('time', 3, 'Chamar o seu time', 'Cada pessoa com o próprio acesso e só o que ela precisa ver.', 2, True, 'Trabalho sozinho', 'equipe', '', 'Adicionar pessoa'),
    ('push', 3, 'Ligar os avisos no celular', 'Mensagem nova e cliente esperando, mesmo com o Oi Nanda fechado.', 1, False, '', '', 'push', 'Ligar avisos'),
    ('aviso_wa', 3, 'Aviso do time no WhatsApp', 'Quando a Nanda passar um cliente, quem vai atender fica sabendo na hora.', 2, True, 'Não preciso', 'equipe', 'aviso', 'Ativar o aviso'),
    ('lembretes', 4, 'Ligar lembretes automáticos', 'Confirmação de horário e retomada de quem sumiu, sem você lembrar.', 2, True, 'Agora não', 'automacoes', '', 'Ver sugestões'),
    ('meta', 4, 'Criar a primeira meta', 'O time vê quanto falta e quanto precisa vender por dia.', 2, True, 'Agora não', 'metas', 'meta', 'Criar meta'),
    ('voz', 4, 'Ligar a voz da Nanda', 'Quando o cliente manda áudio, ela responde em áudio.', 1, True, 'Agora não', 'nanda', '', 'Ouvir e ligar'),
]
KEYS = {s[0] for s in STEPS}

async def status(user):
    cid = user['company_id']; c = user['company']
    from billing import limits
    lim = limits(c); demo = lim.get('state') == 'demo'; feats = set(lim.get('features') or [])
    ai = await db.ai_settings.find_one({'company_id': cid}, {'_id': 0}) or {}
    filled = lambda *k: any(str(ai.get(x) or '').strip() for x in k)
    ag = await db.agenda_settings.find_one({'company_id': cid}, {'_id': 0, 'hours_set': 1, 'pros': 1}) or {}
    wa = await db.wa_numbers.find_one({'company_id': cid}, {'_id': 1})
    team = await db.users.find({'company_id': cid, 'active': True}, {'_id': 0, 'wa_notify': 1, 'wa_phone': 1}).to_list(500)
    done = {
        'sobre': filled('about'),
        'precos': filled('offer', 'knowledge') or bool(await db.products.find_one({'company_id': cid, 'price': {'$gt': 0}}, {'_id': 1})),
        'testar': bool(await db.ai_usage.find_one({'company_id': cid, 'tests': {'$gt': 0}}, {'_id': 1})),
        'whatsapp': bool(wa),
        'modo': 'default_mode' in ai,
        'catalogo': bool(await db.products.find_one({'company_id': cid}, {'_id': 1})),
        'agenda': bool(ag.get('hours_set') or ag.get('pros')),
        'venda': bool(await db.sales.find_one({'company_id': cid}, {'_id': 1})),
        'instagram': bool(await db.ig_accounts.find_one({'company_id': cid}, {'_id': 1})),
        'time': len(team) > 1,
        'push': bool(await db.push_subs.find_one({'user_id': user['id']}, {'_id': 1})),
        'aviso_wa': (c.get('alert_tpl') or {}).get('status') == 'APPROVED' and any(u.get('wa_notify') and u.get('wa_phone') for u in team),
        'lembretes': bool(await db.automations.find_one({'company_id': cid, 'active': True}, {'_id': 1})),
        'meta': bool(await db.goals.find_one({'company_id': cid}, {'_id': 1})),
        'voz': bool(ai.get('voice_reply')),
    }
    skip = set(c.get('setup_skip') or [])
    defs = list(STEPS)
    if demo:  # sem ativar, a Nanda não atende no WhatsApp e não dá para chamar o time
        defs.insert(0, ('ativar', 1, 'Ativar os 14 dias grátis', 'Hoje não é cobrado nada. É o que libera a Nanda no seu WhatsApp e o convite para o time.', 2, False, '', '', 'ativar', 'Ativar agora'))
        done['ativar'] = False
        defs = [d if d[0] != 'time' else (d[0], d[1], d[2], 'Disponível depois de ativar os 14 dias grátis.', d[4], d[5], d[6], d[7], 'ativar', 'Ativar primeiro') for d in defs]
    if 'metas' not in feats: defs = [d for d in defs if d[0] != 'meta']  # metas é do plano Equipe
    steps = [{'key': k, 'stage': st, 'title': t, 'why': w, 'mins': m, 'can_skip': cs, 'skip_label': sl, 'to': to, 'act': act, 'cta': cta,
              'done': done[k], 'skipped': k in skip and not done[k]} for (k, st, t, w, m, cs, sl, to, act, cta) in defs]
    live = [s for s in steps if not s['skipped']]
    stages = []
    for n, name, sub in STAGES:
        ss = [s for s in live if s['stage'] == n]
        stages.append({'n': n, 'name': name, 'sub': sub, 'done': sum(s['done'] for s in ss), 'total': len(ss)})
    nd = sum(s['done'] for s in live)
    return {'steps': steps, 'stages': stages, 'done': nd, 'total': len(live), 'pct': round(100 * nd / len(live)) if live else 100,
            'ready': all(s['done'] for s in live if s['stage'] == 1), 'hidden': bool(c.get('setup_hidden'))}

def managers_only(user):
    if user['role'] not in ('owner', 'manager'): raise HTTPException(403, 'Só o dono ou um gestor configura a conta.')

@router.get('/setup')
async def get_setup(user=Depends(current_user)):
    managers_only(user)
    return await status(user)

class SkipIn(BaseModel):
    key: str
    skip: bool = True

@router.post('/setup/skip')
async def skip_step(body: SkipIn, user=Depends(current_user)):
    managers_only(user)
    if body.key not in KEYS: raise HTTPException(400, 'Passo desconhecido.')
    op = '$addToSet' if body.skip else '$pull'
    await db.companies.update_one({'id': user['company_id']}, {op: {'setup_skip': body.key}})
    user['company'] = await db.companies.find_one({'id': user['company_id']}, {'_id': 0})
    return await status(user)

class HideIn(BaseModel):
    hidden: bool = True

@router.post('/setup/hide')
async def hide(body: HideIn, user=Depends(current_user)):
    managers_only(user)
    await db.companies.update_one({'id': user['company_id']}, {'$set': {'setup_hidden': body.hidden}})
    user['company'] = await db.companies.find_one({'id': user['company_id']}, {'_id': 0})
    return await status(user)
