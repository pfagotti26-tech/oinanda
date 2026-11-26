"""Saúde do número de WhatsApp: percebe na hora quando a Meta restringe, bloqueia ou derruba a nota do número.

Duas fontes, para não depender de uma só:
- Webhook da Meta (account_update, phone_number_quality_update, account_review_update): chega em segundos,
  se esses campos estiverem assinados no app da Meta.
- Conferência de hora em hora: status do número e saúde da conta (health_status) direto na API.

Achou problema: guarda o alerta no número, avisa o dono e os gestores (notificação e e-mail, uma vez por problema)
e mostra no Início com o passo a passo. Quando o número volta ao normal, o alerta some sozinho.
"""
import logging
from datetime import timedelta
from core import db, now

log = logging.getLogger('oinanda')
CHECK_EVERY = timedelta(hours=1)
APPEAL = 'Peça a análise pelo próprio app (botão "Pedir análise") ou no Gerenciador do WhatsApp em business.facebook.com. Enquanto isso, não desinstale o app, não troque de aparelho e não mande mensagens em massa.'

PAY_FIX = ('Falta uma forma de pagamento válida na conta do WhatsApp na Meta. Respostas para quem chamou continuam saindo; lembretes, '
           'automações e campanhas para quem não falou nas últimas 24 h ficam parados. Para resolver: em business.facebook.com, abra '
           'Configurações do negócio › Pagamentos e adicione um cartão na conta do WhatsApp.')

def why_pt(errors):
    """O motivo que a Meta manda vem em inglês: traduz os mais comuns e deixa o resto como veio."""
    out = []
    for e in errors or []:
        t = (e or '').strip().rstrip('.')
        low = t.lower()
        if not t: continue
        if 'payment' in low: out.append('problema na forma de pagamento da conta na Meta')
        elif 'verif' in low: out.append('a empresa ainda não foi verificada na Meta')
        elif 'display name' in low or 'name' in low and 'review' in low: out.append('o nome de exibição do número ainda não foi aprovado')
        elif 'policy' in low or 'violat' in low: out.append('a Meta apontou violação das regras do WhatsApp')
        else: out.append('motivo informado pela Meta: ' + t)
    return '; '.join(dict.fromkeys(out))[:300]

def problem_of(status=None, quality=None, can_send=None, errors=None):
    """Lê o que a Meta devolve e diz se é problema: (tipo, título, detalhe) ou None."""
    st = (status or '').upper()
    if st == 'BANNED': return 'banned', 'O WhatsApp bloqueou o número da empresa', 'A Nanda e o time não conseguem mandar nem receber mensagens por ele. ' + APPEAL
    if st in ('RESTRICTED', 'RATE_LIMITED'): return 'restricted', 'O WhatsApp restringiu o número da empresa', 'Por um tempo ele só responde quem chamou e não pode iniciar conversas. Evite campanhas até a restrição acabar. ' + APPEAL
    if (can_send or '').upper() in ('BLOCKED', 'LIMITED') and any('payment' in (e or '').lower() for e in errors or []):
        return 'payment', 'A Meta pausou as mensagens que a empresa inicia', PAY_FIX
    if (can_send or '').upper() == 'BLOCKED':
        why = why_pt(errors)
        return 'blocked', 'A Meta bloqueou o envio de mensagens deste número', (why[0].upper() + why[1:] + '. ' if why else '') + 'Veja os detalhes no Gerenciador do WhatsApp em business.facebook.com e resolva o que a Meta pede.'
    if st == 'FLAGGED' or (quality or '').upper() == 'RED':
        return 'flagged', 'A nota do número na Meta caiu para vermelho', 'Muita gente bloqueou ou denunciou mensagens recentes. Pause campanhas e automações que iniciam conversa por alguns dias, senão o número pode ser restringido.'
    if (can_send or '').upper() == 'LIMITED':
        why = why_pt(errors)
        return 'limited', 'A Meta limitou o envio deste número', (why[0].upper() + why[1:] + '. ' if why else '') + 'Respostas para quem chamou continuam saindo. Veja o motivo no Gerenciador do WhatsApp em business.facebook.com.'
    if st == 'DISCONNECTED': return 'disconnected', 'O número foi desconectado na Meta', 'As mensagens deixaram de chegar. Reconecte em Configurações › WhatsApp. Se usa o app junto, abra o WhatsApp Business no celular.'
    return None

async def raise_alert(n, kind, title, detail, source):
    cur = n.get('alert') or {}
    if cur.get('kind') == kind: return False  # já avisado
    if {cur.get('kind'), kind} == {'blocked', 'payment'}:  # mesmo problema, só com o texto melhor: troca sem avisar de novo
        await db.wa_numbers.update_one({'company_id': n['company_id']}, {'$set': {'alert.kind': kind, 'alert.title': title, 'alert.detail': detail}})
        return False
    alert = {'kind': kind, 'title': title, 'detail': detail, 'at': now(), 'source': source}
    await db.wa_numbers.update_one({'company_id': n['company_id']}, {'$set': {'alert': alert}})
    await db.wa_alerts.insert_one({'company_id': n['company_id'], 'phone': n.get('display_phone'), **alert})
    from core import audit
    await audit(None, None, 'Alerta do WhatsApp', f'{n.get("display_phone")} · {title}', company_id=n['company_id'])
    await notify(n, title, detail, kind)
    return True

async def clear_alert(n):
    if not n.get('alert'): return False
    await db.wa_numbers.update_one({'company_id': n['company_id']}, {'$unset': {'alert': ''}})
    await db.wa_alerts.insert_one({'company_id': n['company_id'], 'phone': n.get('display_phone'), 'kind': 'ok', 'title': 'Número voltou ao normal', 'at': now()})
    return True

STEPS = {  # o que fazer, em passos, para o e-mail
    'banned': ('A Nanda e o time não conseguem mandar nem receber mensagens por este número até o WhatsApp liberar.',
               [(2, 'Peça a análise no app WhatsApp Business', 'botão "Pedir análise"; a maioria sai em até 24 h'), (0, 'Não desinstale o app nem troque de aparelho', 'isso pode atrasar a análise'), (0, 'Não mande mensagens em massa enquanto isso', 'nem pelo celular, nem por outras ferramentas')]),
    'restricted': ('Por um tempo o número só responde quem chamou e não pode iniciar conversas.',
                   [(2, 'Pause campanhas e automações que iniciam conversa', 'em Automações e Clientes › Campanhas'), (0, 'Veja o motivo no Gerenciador do WhatsApp', 'business.facebook.com'), (0, 'Se discordar, peça a análise por lá', '')]),
    'payment': ('Respostas para quem chamou continuam saindo. Lembretes, automações e campanhas para quem não falou nas últimas 24 h ficam parados.',
                [(2, 'Abra business.facebook.com', 'Configurações do negócio › Pagamentos'), (0, 'Adicione um cartão na conta do WhatsApp', 'é o cartão que a Meta usa para cobrar as mensagens'), (0, 'Pronto: o envio volta sozinho', 'e o aviso some do painel')]),
    'flagged': ('Muita gente bloqueou ou denunciou mensagens recentes. Se continuar, o número pode ser restringido.',
                [(2, 'Pause campanhas e automações que iniciam conversa', 'por alguns dias'), (0, 'Mande só para quem pediu para receber', 'e com mensagem que a pessoa espera'), (0, 'A nota volta a subir com o tempo', 'o Oi Nanda avisa quando normalizar')]),
}

async def notify(n, title, detail, kind=''):
    """Dono e gestores ficam sabendo na hora: notificação no celular/computador e e-mail."""
    try:
        from push import send_user
        from mailer import send, layout, plain
        users = await db.users.find({'company_id': n['company_id'], 'active': True, 'role': {'$in': ['owner', 'manager']}}, {'_id': 0, 'id': 1, 'email': 1, 'name': 1}).to_list(50)
        for u in users:
            try: await send_user(u['id'], '⚠️ ' + title, detail, '/', 'wa-alert')
            except Exception: log.exception('push do alerta do WhatsApp')
            if u.get('email'):
                import html as _h
                lead, steps = STEPS.get(kind, (detail, None))
                paras = [_h.escape(lead)]
                bad = kind in ('banned', 'blocked', 'restricted', 'payment')
                await send(u['email'], title, layout(title, paras, ('Ver no Oi Nanda', 'https://app.oinanda.com.br/empresa'), 'O aviso some sozinho do painel quando a Meta liberar o número. A gente confere de hora em hora.',
                                                     icon='wa-r' if bad else 'wa-a', tone='r' if bad else 'a', eyebrow='WhatsApp da empresa', pre=lead[:120], steps=steps, steps_title='O que fazer agora',
                                                     rows=[('Número', n.get('display_phone') or ''), ('Percebido em', now().astimezone(__import__('zoneinfo').ZoneInfo('America/Sao_Paulo')).strftime('%d/%m às %H:%M'))],
                                                     why='Você recebe este alerta por ser dono ou gestor da conta.'), plain(title, paras))
    except Exception: log.exception('falha ao avisar o alerta do WhatsApp')

async def find_number(entry_id, value):
    n = await db.wa_numbers.find_one({'waba_id': str(entry_id)}, {'_id': 0}) if entry_id else None
    if not n and value.get('display_phone_number'):
        d = ''.join(ch for ch in str(value['display_phone_number']) if ch.isdigit())
        for x in await db.wa_numbers.find({}, {'_id': 0}).to_list(5000):
            if ''.join(ch for ch in (x.get('display_phone') or '') if ch.isdigit()) == d: return x
    return n

async def on_webhook(field, entry_id, value):
    """Avisos que a Meta manda sozinha sobre a conta e o número."""
    n = await find_number(entry_id, value)
    if not n: return
    ev = (value.get('event') or value.get('decision') or '').upper()
    if field == 'account_update':
        if ev in ('DISABLED_UPDATE', 'ACCOUNT_DELETED') or (value.get('ban_info') or {}).get('waba_ban_state') in ('DISABLE', 'SCHEDULE_FOR_DISABLE'):
            return await raise_alert(n, 'banned', 'O WhatsApp bloqueou a conta da empresa', 'A Nanda e o time não conseguem mandar nem receber mensagens. ' + APPEAL, 'webhook')
        if ev in ('ACCOUNT_RESTRICTION', 'ACCOUNT_VIOLATION'):
            info = value.get('violation_info') or value.get('restriction_info') or {}
            why = info.get('violation_type') if isinstance(info, dict) else ''
            return await raise_alert(n, 'restricted', 'O WhatsApp restringiu a conta da empresa', (f'Motivo informado pela Meta: {why}. ' if why else '') + APPEAL, 'webhook')
    elif field == 'phone_number_quality_update':
        if ev == 'FLAGGED' or (value.get('current_limit') or '').upper() == 'TIER_0':
            p = problem_of(status='FLAGGED')
            return await raise_alert(n, p[0], p[1], p[2], 'webhook')
        if ev in ('UNFLAGGED', 'UPGRADE'): return await clear_alert(n)
    elif field == 'account_review_update' and ev == 'REJECTED':
        return await raise_alert(n, 'review', 'A Meta recusou a análise da conta da empresa', 'Veja o motivo no Gerenciador do WhatsApp em business.facebook.com e corrija os dados da empresa para pedir de novo.', 'webhook')

async def check(n):
    """Pergunta à Meta como está o número e a conta."""
    import whatsapp
    tok = whatsapp.decrypt(n['token_enc'])
    info = await whatsapp.graph('GET', f'/{n["phone_number_id"]}', tok, params={'fields': 'status,quality_rating,name_status,messaging_limit_tier'})
    hs = {}
    try: hs = (await whatsapp.graph('GET', f'/{n["waba_id"]}', tok, params={'fields': 'health_status'})).get('health_status') or {}
    except Exception: pass
    errs = [e.get('error_description') for ent in hs.get('entities') or [] for e in ent.get('errors') or []]
    snap = {'status': info.get('status'), 'quality': info.get('quality_rating'), 'can_send': hs.get('can_send_message'), 'errors': errs[:5], 'at': now()}
    await db.wa_numbers.update_one({'company_id': n['company_id']}, {'$set': {'checkup': snap, 'quality': info.get('quality_rating') or n.get('quality')}})
    p = problem_of(snap['status'], snap['quality'], snap['can_send'], errs)
    if p: await raise_alert(n, *p, 'conferencia')
    else: await clear_alert(n)
    return snap

async def tick():
    """De hora em hora, um número de cada vez (no máximo 30 por volta)."""
    cut = now() - CHECK_EVERY
    for n in await db.wa_numbers.find({'status': 'connected', '$or': [{'checkup.at': {'$lt': cut}}, {'checkup': {'$exists': False}}]}, {'_id': 0}).to_list(30):
        try: await check(n)
        except Exception as e:
            log.warning('conferir saúde do número %s: %s', n.get('display_phone'), e)
            await db.wa_numbers.update_one({'company_id': n['company_id']}, {'$set': {'checkup.at': now(), 'checkup.error': str(e)[:200]}})
