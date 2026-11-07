"""E-mails do Oi Nanda: nova senha, boas-vindas, convite de equipe e aviso de fim do teste.

Envia pela API do Resend (RESEND_API_KEY). Sem a chave, nada quebra: o e-mail não sai e o log diz para quem seria.
Links de senha nunca vão para o log.
"""
import os, html, logging, secrets
from datetime import timedelta
import httpx
from core import db, now, digest

log = logging.getLogger('mailer')
APP = os.environ.get('APP_PUBLIC_URL', 'https://app.oinanda.com.br').rstrip('/')

def ready(): return bool(os.environ.get('RESEND_API_KEY'))
def sender(): return os.environ.get('MAIL_FROM') or 'Oi Nanda <nanda@oinanda.com.br>'

SENT = []  # nos testes: o que teria sido enviado

async def send(to, subject, body_html, text):
    if os.environ.get('MONGO_URL') == 'mock':
        SENT.append({'to': to, 'subject': subject, 'html': body_html, 'text': text}); return True
    if not ready():
        log.info('e-mail não enviado (RESEND_API_KEY ausente): "%s" para %s', subject, to); return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post('https://api.resend.com/emails', headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY']},
                             json={'from': sender(), 'to': [to], 'subject': subject, 'html': body_html, 'text': text,
                                   'reply_to': os.environ.get('MAIL_REPLY_TO') or 'contato@altaagencydigital.com'})
        if r.status_code >= 300:
            log.warning('Resend recusou o e-mail "%s" (%s): %s', subject, r.status_code, r.text[:300]); return False
        return True
    except Exception:
        log.exception('falha ao enviar e-mail "%s"', subject); return False

def layout(title, paragraphs, button=None, foot=None):
    """HTML simples, que funciona em qualquer cliente de e-mail e no celular."""
    p = ''.join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55;color:#2B2622">{x}</p>' for x in paragraphs)
    b = (f'<p style="margin:22px 0"><a href="{html.escape(button[1])}" style="display:inline-block;background:#E8603F;color:#fff;'
         f'text-decoration:none;font-weight:600;font-size:15px;padding:12px 22px;border-radius:10px">{html.escape(button[0])}</a></p>'
         f'<p style="margin:0 0 14px;font-size:12.5px;color:#8A817A">Se o botão não abrir, copie este endereço: <br>{html.escape(button[1])}</p>') if button else ''
    f = f'<p style="margin:18px 0 0;font-size:12.5px;color:#8A817A">{foot}</p>' if foot else ''
    return (f'<!doctype html><html><body style="margin:0;background:#F7F2EC;font-family:Helvetica,Arial,sans-serif">'
            f'<div style="max-width:520px;margin:0 auto;padding:28px 18px">'
            f'<div style="font-weight:800;font-size:20px;color:#E8603F;margin-bottom:18px">Oi Nanda</div>'
            f'<div style="background:#fff;border-radius:14px;padding:26px 24px;border:1px solid #EDE5DC">'
            f'<h1 style="margin:0 0 16px;font-size:20px;color:#2B2622">{html.escape(title)}</h1>{p}{b}{f}</div>'
            f'<p style="font-size:12px;color:#A39A92;margin:16px 4px">Oi Nanda · app.oinanda.com.br</p></div></body></html>')

def plain(title, paragraphs, button=None):
    import re
    t = [title, ''] + [re.sub(r'<[^>]+>', '', x) for x in paragraphs]
    if button: t += ['', f'{button[0]}: {button[1]}']
    return '\n'.join(t)

# ---------- links de senha ----------
async def password_link(user, minutes=30, kind='reset'):
    token = secrets.token_urlsafe(32)
    await db.password_resets.insert_one({'token_hash': digest(token), 'user_id': user['id'], 'kind': kind, 'used': False,
                                         'created_at': now(), 'expires_at': now() + timedelta(minutes=minutes)})
    return f'{APP}/nova-senha?t={token}'

def first(name): return html.escape((name or '').split(' ')[0])

async def send_reset(user):
    link = await password_link(user)
    paras = [f'Oi, {first(user.get("name"))}!', 'Recebemos um pedido para criar uma senha nova no Oi Nanda. O link vale por 30 minutos e só funciona uma vez.']
    foot = 'Se não foi você, pode ignorar este e-mail. Sua senha atual continua a mesma.'
    return await send(user['email'], 'Crie sua nova senha do Oi Nanda', layout('Nova senha', paras, ('Criar nova senha', link), foot),
                      plain('Nova senha', paras + [foot], ('Criar nova senha', link)))

async def send_invite(member, by_name, company_name):
    link = await password_link(member, minutes=72 * 60, kind='invite')
    paras = [f'Oi, {first(member.get("name"))}!', f'{html.escape(by_name)} chamou você para atender os clientes da <b>{html.escape(company_name)}</b> no Oi Nanda.',
             'Crie sua senha pelo botão abaixo. O link vale por 3 dias.']
    return await send(member['email'], f'{by_name} chamou você para o Oi Nanda', layout('Você foi convidado', paras, ('Criar minha senha', link)),
                      plain('Você foi convidado', paras, ('Criar minha senha', link)))

async def send_welcome(user, company):
    paras = [f'Oi, {first(user.get("name"))}! Que bom ter a <b>{html.escape(company.get("name") or "")}</b> aqui.',
             'Você tem 14 dias grátis, sem cartão. O primeiro passo é conectar o WhatsApp da empresa: leva uns 5 minutos e as conversas começam a aparecer na hora.',
             'Depois disso, vale ensinar a Nanda sobre o seu negócio (preços, horários, serviços) em <b>Nanda (IA)</b>. Ela usa isso para sugerir as respostas.']
    return await send(user['email'], 'Bem-vindo ao Oi Nanda', layout('Bem-vindo ao Oi Nanda', paras, ('Conectar o WhatsApp', APP + '/empresa'),
                      'Qualquer dúvida, é só responder este e-mail.'), plain('Bem-vindo ao Oi Nanda', paras, ('Conectar o WhatsApp', APP + '/empresa')))

async def trial_tick():
    """Aviso 3 dias antes do fim do teste e no dia em que acaba (uma vez cada)."""
    for kind, lo, hi in (('trial_3d', timedelta(days=2), timedelta(days=3)), ('trial_end', timedelta(days=-2), timedelta(0))):
        rows = await db.companies.find({'plan': 'trial', f'mail_{kind}': {'$ne': True},
                                        'trial_ends_at': {'$gt': now() + lo, '$lte': now() + hi}}, {'_id': 0}).to_list(200)
        for c in rows:
            await db.companies.update_one({'id': c['id']}, {'$set': {f'mail_{kind}': True}})
            owner = await db.users.find_one({'company_id': c['id'], 'role': 'owner', 'active': True}, {'_id': 0})
            if not owner: continue
            if kind == 'trial_3d':
                paras = [f'Oi, {first(owner.get("name"))}!', 'Faltam 3 dias para acabar o teste grátis da <b>' + html.escape(c.get('name') or '') + '</b> no Oi Nanda.',
                         'Para continuar atendendo sem parar, escolha um plano. Nada do que você cadastrou se perde.']
                await send(owner['email'], 'Seu teste do Oi Nanda acaba em 3 dias', layout('Faltam 3 dias', paras, ('Ver planos', APP + '/assinatura')),
                           plain('Faltam 3 dias', paras, ('Ver planos', APP + '/assinatura')))
            else:
                paras = [f'Oi, {first(owner.get("name"))}!', 'O teste grátis acabou e a conta ficou só para leitura: as conversas e os clientes continuam lá, mas não dá para responder pelo Oi Nanda.',
                         'Escolha um plano e tudo volta a funcionar na hora.']
                await send(owner['email'], 'Seu teste do Oi Nanda acabou', layout('O teste acabou', paras, ('Escolher um plano', APP + '/assinatura')),
                           plain('O teste acabou', paras, ('Escolher um plano', APP + '/assinatura')))
