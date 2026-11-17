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
    import notices
    return await notices.deliver(company, 'welcome', once='welcome')

# ---------- avisos automáticos: os textos, o WhatsApp e as regras ficam em notices.py ----------
def money(v): return 'R$ ' + f'{float(v or 0):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

async def owner_of(company):
    return await db.users.find_one({'company_id': company['id'], 'role': 'owner', 'active': True}, {'_id': 0})

async def notices():
    import notices as N
    return await N.toggles()

async def payment_notice(company, pay):
    """Cobrança paga ou recusada: avisa o responsável uma vez por situação."""
    import notices as N
    from billing import GRACE_DAYS
    due = pay.get('due'); v = {'valor': money(pay['amount'])}
    if pay['status'] == 'paid':
        return await N.deliver(company, 'paid', {**v, 'data': N.day(pay.get('paid_at') or due)}, once=f'paid:{pay["id"]}')
    return await N.deliver(company, 'refused', {**v, 'prazo': N.day(N.aware(due) + timedelta(days=GRACE_DAYS)) if due else ''}, once=f'refused:{pay["id"]}')

async def charge_tick():
    import notices as N
    await N.tick()

async def trial_tick():
    import notices as N
    await N.tick()
