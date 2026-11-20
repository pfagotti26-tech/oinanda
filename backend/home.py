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
    # aviso do time no WhatsApp
    if await db.wa_numbers.find_one({'company_id': cid}, {'_id': 1}):
        team = await db.users.find({'company_id': cid, 'active': True}, {'_id': 0, 'wa_notify': 1, 'wa_phone': 1}).to_list(500)
        on = sum(1 for u in team if u.get('wa_notify') and u.get('wa_phone'))
        tpl = (user['company'].get('alert_tpl') or {}).get('status')
        if not tpl or not on:
            out.append({'k': 'alert', 'tone': 'info', 'n': len(team) - on, 'to': 'equipe', 'cta': 'Ativar',
                        'title': 'O time ainda não recebe aviso no WhatsApp',
                        'sub': 'Quando a Nanda passar um cliente, quem vai atender fica sabendo no celular, mesmo com o Oi Nanda fechado.'})
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
    order = {'bad': 0, 'warn': 1, 'info': 2}
    return sorted(out, key=lambda x: order[x['tone']])
