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

# ---------- visual único de todos os e-mails ----------
TONE = {  # fundo do destaque, cor do texto do destaque, cor de acento
    'i': ('#FEF0EA', '#9E3A22', '#E8603F'), 'g': ('#E3F6EE', '#0A5C50', '#14946F'), 'a': ('#FDF3DC', '#7A5600', '#D99A00'),
    'r': ('#FCE9E6', '#9E2B1E', '#D9412F'), 'n': ('#EEF1F4', '#3F4C59', '#7A8794')}
FONT = "Manrope,'Segoe UI',Helvetica,Arial,sans-serif"
LEGAL = 'Fagotti Marketing Direto Ltda · CNPJ 40.864.395/0001-95'

def _btn(label, url, tone='i', ghost=False):
    e = html.escape
    style = 'background:#ffffff;color:#17212B;border:1px solid #DCD2C9' if ghost else f'background:#E8603F;color:#ffffff;border:1px solid #E8603F'
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:22px 0 0"><tr><td align="center" style="border-radius:12px">'
            f'<a href="{e(url)}" style="display:block;{style};border-radius:12px;padding:15px 22px;font-family:{FONT};font-size:16px;font-weight:700;'
            f'text-decoration:none;text-align:center">{e(label)}</a></td></tr></table>')

def render(c):
    """Monta o e-mail. c: tone, icon, eyebrow, title, pre, body (lista de parágrafos, HTML já seguro), fig (número, legenda),
    bar (atual, total, rótulo, direita), steps [(estado, título, sub)], rows [(rótulo, valor)], button (texto, url), second (texto, url),
    foot, why (por que recebe), prefs (link de preferências)."""
    e = html.escape; t = c.get('tone') or 'i'; bg, fg, acc = TONE.get(t, TONE['i'])
    icon = c.get('icon')
    out = []
    head = (f'<td width="44" valign="middle" style="padding-right:12px"><img src="{APP}/mail/{icon}.png" width="44" height="44" alt="" style="display:block;border:0;border-radius:13px"></td>' if icon else '')
    out.append(f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 18px"><tr>{head}<td valign="middle" style="font-family:{FONT};font-size:12px;'
               f'font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{fg}">{e(c.get("eyebrow") or "")}</td></tr></table>')
    out.append(f'<h1 style="margin:0 0 12px;font-family:{FONT};font-size:25px;line-height:1.22;font-weight:800;letter-spacing:-.4px;color:#17212B">{e(c["title"])}</h1>')
    for p in c.get('body') or []:
        out.append(f'<p style="margin:0 0 12px;font-family:{FONT};font-size:15.5px;line-height:1.6;color:#3B4754">{p}</p>')
    if c.get('fig'):
        bar = ''
        if c.get('bar'):
            a, b, l, r = c['bar']; pct = max(0, min(100, round(100 * a / b))) if b else 0
            bar = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px"><tr><td style="background:#ffffff;border-radius:99px;height:8px;font-size:0;line-height:0">'
                   f'<div style="width:{pct}%;height:8px;background:{acc};border-radius:99px"></div></td></tr></table>'
                   f'<table role="presentation" width="100%" style="margin-top:8px;font-family:{FONT};font-size:12.5px;color:#4B5968"><tr><td style="font-weight:700;color:#17212B">{e(l)}</td><td align="right">{e(r)}</td></tr></table>')
        out.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 6px"><tr><td style="background:{bg};border-radius:16px;padding:18px 20px">'
                   f'<div style="font-family:{FONT};font-size:30px;line-height:1.1;font-weight:800;letter-spacing:-.5px;color:#17212B">{e(c["fig"][0])}</div>'
                   f'<div style="font-family:{FONT};font-size:13px;color:{fg};margin-top:4px;font-weight:600">{e(c["fig"][1])}</div>{bar}</td></tr></table>')
    if c.get('steps'):
        out.append(f'<div style="font-family:{FONT};font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#8593A1;margin:20px 0 10px">{e(c.get("steps_title") or "Próximos passos")}</div>')
        for i, (st, a, b) in enumerate(c['steps'], 1):
            dot = ('#E3F6EE', '#0A5C50', '✓') if st == 1 else (acc, '#ffffff', str(i)) if st == 2 else ('#F3EDE7', '#4B5968', str(i))
            out.append(f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 12px"><tr><td valign="top" style="padding-right:12px">'
                       f'<div style="width:26px;height:26px;line-height:26px;border-radius:50%;text-align:center;font-family:{FONT};font-size:12.5px;font-weight:800;background:{dot[0]};color:{dot[1]}">{dot[2]}</div></td>'
                       f'<td style="font-family:{FONT};font-size:14.5px;line-height:1.35;color:#17212B;font-weight:{400 if st == 1 else 700};{"text-decoration:line-through;color:#8593A1;" if st == 1 else ""}">{e(a)}'
                       + (f'<br><span style="font-size:12.5px;font-weight:400;color:#6B7785;text-decoration:none">{e(b)}</span>' if b else '') + '</td></tr></table>')
    rows = [(a, b) for a, b in c.get('rows') or [] if str(b).strip()]
    if rows:
        cells = ''.join(f'<tr><td style="padding:11px 16px;font-family:{FONT};font-size:13.5px;color:#6B7785;{"border-top:1px solid #F0EAE3;" if k else ""}">{e(a)}</td>'
                        f'<td align="right" style="padding:11px 16px;font-family:{FONT};font-size:13.5px;font-weight:700;color:#17212B;{"border-top:1px solid #F0EAE3;" if k else ""}">{e(b)}</td></tr>'
                        for k, (a, b) in enumerate(rows))
        out.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:14px 0 0;border:1px solid #EFE7DF;border-radius:14px;border-collapse:separate">{cells}</table>')
    if c.get('button'): out.append(_btn(c['button'][0], c['button'][1], t, c.get('ghost')))
    if c.get('second'):
        out.append(f'<p style="margin:14px 0 0;text-align:center;font-family:{FONT};font-size:13.5px"><a href="{e(c["second"][1])}" style="color:#C44A2C;font-weight:700;text-decoration:none">{e(c["second"][0])} →</a></p>')
    if c.get('foot'):
        out.append(f'<p style="margin:18px 0 0;padding-top:16px;border-top:1px solid #F0EAE3;font-family:{FONT};font-size:12.5px;line-height:1.5;color:#6B7785">{e(c["foot"])}</p>')
    why = e(c.get('why') or 'Você recebe este e-mail porque tem uma conta no Oi Nanda.')
    prefs = f' · <a href="{APP}/empresa/avisos" style="color:#8A817A">Escolher meus avisos</a>' if c.get('prefs') else ''
    return (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
            f'<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet"></head>'
            f'<body style="margin:0;padding:0;background:#F4EFEA">'
            f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#F4EFEA">{e(c.get("pre") or "")}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4EFEA"><tr><td align="center" style="padding:28px 14px 36px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">'
            f'<tr><td style="padding:0 6px 18px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td><a href="{APP}/"><img src="{APP}/mail/logo.png" width="68" height="40" alt="Oi Nanda" style="display:block;border:0"></a></td>'
            f'<td align="right" style="font-family:{FONT};font-size:13px"><a href="{APP}/" style="color:#6B7785;text-decoration:none;font-weight:600">Abrir o painel →</a></td></tr></table></td></tr>'
            f'<tr><td style="background:#ffffff;border-radius:22px;border:1px solid #EDE5DC;overflow:hidden">'
            f'<div style="height:5px;background:{acc};border-radius:22px 22px 0 0;font-size:0;line-height:0">&nbsp;</div>'
            f'<div style="padding:30px 28px 30px">{"".join(out)}</div></td></tr>'
            f'<tr><td style="padding:22px 10px 0"><table role="presentation" cellpadding="0" cellspacing="0"><tr>'
            f'<td valign="top" style="padding-right:12px"><div style="width:36px;height:36px;line-height:36px;border-radius:50%;background:#E8603F;color:#fff;text-align:center;font-family:{FONT};font-weight:800;font-size:15px">N</div></td>'
            f'<td style="font-family:{FONT};font-size:13.5px;line-height:1.45;color:#3B4754"><b style="color:#17212B">Equipe Oi Nanda</b><br>Ficou com dúvida? É só responder este e-mail: quem lê é uma pessoa do time.</td></tr></table></td></tr>'
            f'<tr><td style="padding:22px 10px 0;font-family:{FONT};font-size:12px;line-height:1.6;color:#8A817A">'
            f'<a href="{APP}/" style="color:#6B7785;font-weight:700;text-decoration:none">Painel</a> &nbsp;·&nbsp; <a href="https://oinanda.com.br/" style="color:#6B7785;font-weight:700;text-decoration:none">Site</a>'
            f' &nbsp;·&nbsp; <a href="https://instagram.com/oinanda.app" style="color:#6B7785;font-weight:700;text-decoration:none">Instagram</a><br>{why}{prefs}<br>{LEGAL}</td></tr>'
            f'</table></td></tr></table></body></html>')

def layout(title, paragraphs, button=None, foot=None, **kw):
    """Atalho para e-mails simples (senha, convite, alertas): usa o mesmo visual de todos."""
    return render({'title': title, 'body': paragraphs, 'button': button, 'foot': foot, **kw})

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
    paras = [f'Oi, {first(user.get("name"))}! Recebemos um pedido para criar uma senha nova para a sua conta no Oi Nanda.']
    foot = 'Não foi você? Pode ignorar este e-mail: a senha atual continua valendo e ninguém entra sem este link.'
    return await send(user['email'], 'Crie sua nova senha do Oi Nanda',
                      layout('Vamos criar sua senha nova', paras, ('Criar nova senha', link), foot, icon='lock-i', eyebrow='Acesso à conta',
                             pre='O link vale por 30 minutos e só funciona uma vez.', rows=[('Validade do link', '30 minutos'), ('Uso', 'uma vez só')],
                             why='Você recebe este e-mail porque alguém pediu uma senha nova para este endereço.'),
                      plain('Nova senha', paras + [foot], ('Criar nova senha', link)))

async def send_invite(member, by_name, company_name):
    link = await password_link(member, minutes=72 * 60, kind='invite')
    paras = [f'Oi, {first(member.get("name"))}! {html.escape(by_name)} chamou você para atender os clientes da <b>{html.escape(company_name)}</b> no Oi Nanda.',
             'É por lá que chegam as conversas do WhatsApp e do Instagram, com a Nanda ajudando a responder.']
    return await send(member['email'], f'{by_name} chamou você para o Oi Nanda',
                      layout(f'Você entrou para o time da {company_name}', paras, ('Criar minha senha', link), 'O link vale por 3 dias. Depois disso, peça um novo convite.',
                             icon='team-i', eyebrow='Convite', pre=f'{by_name} chamou você para atender os clientes da {company_name}.',
                             steps=[(2, 'Crie sua senha', 'pelo botão abaixo'), (0, 'Ligue os avisos no celular', 'para saber quando um cliente chega'), (0, 'Atenda as conversas', 'as suas e as que estiverem sem dono')],
                             steps_title='Como começar', why=f'Você recebe este e-mail porque {by_name} convidou este endereço para a {company_name}.'),
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
