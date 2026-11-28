"""O que precisa do dono ou do gestor agora: junta no Início as pendências que ficavam espalhadas em cada página."""
from datetime import timedelta
from fastapi import APIRouter, Depends
from core import db, now, managers
from whatsapp import aware

router = APIRouter()

def plural(n, um, varios): return um if n == 1 else varios

@router.get('/home/todo')
async def todo(user=Depends(managers)):
    cid = user['company_id']; out = []
    n = await db.wa_numbers.find_one({'company_id': cid}, {'_id': 0, 'alert': 1})
    if n and n.get('alert'):
        a = n['alert']
        out.append({'k': 'wa_alert', 'tone': 'bad', 'n': 1, 'to': 'empresa', 'cta': 'Ver o que fazer', 'title': a['title'], 'sub': a['detail']})
    # perguntas que a Nanda não soube responder
    gaps = await db.ai_gaps.count_documents({'company_id': cid, 'status': 'open'})
    if gaps:
        top = await db.ai_gaps.find_one({'company_id': cid, 'status': 'open'}, {'_id': 0, 'q': 1}, sort=[('count', -1), ('last_at', -1)])
        out.append({'k': 'gaps', 'tone': 'warn', 'n': gaps, 'to': 'nanda', 'cta': 'Ensinar',
                    'title': f'{gaps} {plural(gaps, "pergunta que a Nanda não soube responder", "perguntas que a Nanda não soube responder")}',
                    'sub': f'Ex.: "{top["q"]}". Responda uma vez e ela passa a saber.' if top else 'Responda uma vez e ela passa a saber.'})
    # automações esperando modelo da Meta
    autos = await db.automations.find({'company_id': cid, 'active': True, 'template_id': {'$ne': None}}, {'_id': 0, 'name': 1, 'template_id': 1}).to_list(200)
    tpls = {t['id']: t for t in await db.templates.find({'company_id': cid, 'id': {'$in': [a['template_id'] for a in autos]}}, {'_id': 0}).to_list(200)}
    bad = [a for a in autos if (tpls.get(a['template_id']) or {}).get('status') == 'REJECTED']
    late = [a for a in autos if (tpls.get(a['template_id']) or {}).get('status') == 'PENDING' and aware(tpls[a['template_id']].get('created_at')) and now() - aware(tpls[a['template_id']]['created_at']) > timedelta(hours=24)]
    if bad:
        out.append({'k': 'tpl_bad', 'tone': 'bad', 'n': len(bad), 'to': 'automacoes', 'cta': 'Ajustar',
                    'title': f'A Meta recusou a mensagem de "{bad[0]["name"]}"' + (f' e de mais {len(bad) - 1}' if len(bad) > 1 else ''),
                    'sub': 'Enquanto isso ela só sai para quem falou com você nas últimas 24 h. Ajuste o texto e peça de novo.'})
    if late:
        out.append({'k': 'tpl_late', 'tone': 'warn', 'n': len(late), 'to': 'automacoes', 'cta': 'Ver',
                    'title': f'"{late[0]["name"]}" espera a Meta há mais de um dia' + (f' (e mais {len(late) - 1})' if len(late) > 1 else ''),
                    'sub': 'Costuma sair em horas. Enquanto isso ela só sai para quem falou com você nas últimas 24 h.'})
    # aviso do time no WhatsApp agora fica no guia Configurar a conta (setup.py): aqui só o que é problema
    # conversas que parecem robô de outro perfil
    from inbox import looks_auto
    since = now() - timedelta(days=30)
    cand = [c['id'] for c in await db.contacts.find({'company_id': cid, 'last_at': {'$gte': since}, 'internal': {'$ne': True}, 'bot_ok': {'$ne': True},
                                                      'sales_count': {'$not': {'$gt': 0}}}, {'_id': 0, 'id': 1}).to_list(2000)]
    by = {}
    for m in await db.messages.find({'company_id': cid, 'contact_id': {'$in': cand}, 'dir': {'$in': ['in', 'out']}}, {'_id': 0, 'contact_id': 1, 'dir': 1, 'text': 1, 'type': 1, 'by_id': 1}).to_list(20000):
        by.setdefault(m['contact_id'], []).append(m)
    auto = sum(1 for k, v in by.items() if looks_auto(v))
    if auto:
        out.append({'k': 'auto', 'tone': 'info', 'n': auto, 'to': 'conversas', 'cta': 'Revisar',
                    'title': f'{auto} {plural(auto, "conversa parece", "conversas parecem")} mensagem automática de outro perfil',
                    'sub': 'Tire da base para não contar como cliente esperando nem atrapalhar os relatórios.'})
    # cliente sem resposta com a janela de 24 h fechada, e negociação parada (antes ficavam em Relatórios)
    from inbox import waiting as _is_waiting
    from channels import view, window_of
    closed = 0
    for c in await db.contacts.find({'company_id': cid, 'last_dir': 'in', 'last_in_at': {'$lte': now() - timedelta(hours=1)}, 'needs_human': {'$ne': True}}, {'_id': 0}).to_list(5000):
        if not _is_waiting(c): continue
        v = view(c); w = window_of(v.get('channel') or 'whatsapp'); li = aware(v.get('last_in_at'))
        if w and li and now() - li >= w: closed += 1
    if closed:
        out.append({'k': 'closed', 'tone': 'bad', 'n': closed, 'to': 'conversas', 'filter': 'waiting', 'cta': 'Ver',
                    'title': f'{closed} {plural(closed, "cliente ficou", "clientes ficaram")} sem resposta e a janela de 24 h fechou',
                    'sub': 'No WhatsApp, só com modelo aprovado pela Meta. No Instagram, só se a pessoa escrever de novo.'})
    from funnel import stages_for
    open_ids = [x['id'] for x in await stages_for(user['company']) if x['kind'] == 'open']
    stale = await db.contacts.count_documents({'company_id': cid, 'stage': {'$in': open_ids[1:]}, 'last_at': {'$lte': now() - timedelta(days=3)}}) if len(open_ids) > 1 else 0
    if stale:
        out.append({'k': 'stale', 'tone': 'warn', 'n': stale, 'to': 'funil', 'filter': 'stale', 'cta': 'Ver',
                    'title': f'{stale} {plural(stale, "negociação parada", "negociações paradas")} há 3 dias ou mais',
                    'sub': 'Já passaram da primeira etapa do funil e ninguém falou com elas.'})
    order = {'bad': 0, 'warn': 1, 'info': 2}
    return sorted(out, key=lambda x: order[x['tone']])
