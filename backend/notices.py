"""Avisos do Oi Nanda para o responsável de cada conta: e-mail e WhatsApp.

Cada aviso tem um texto padrão (DEFS) que o admin pode editar em Avisos. O e-mail sai pelo Resend (mailer.send).
O WhatsApp sai do número do próprio Oi Nanda (PLATFORM_WA_PHONE_ID, PLATFORM_WA_TOKEN, PLATFORM_WA_WABA_ID) e só com
modelo aprovado pela Meta. Sem essas chaves, ou sem modelo aprovado, o aviso vai só por e-mail.
"""
import os, re, html, logging
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from core import db, now, uid
import mailer

log = logging.getLogger('notices')
APP = mailer.APP
SP = ZoneInfo('America/Sao_Paulo')
TONES = {'i': ('#FEF0EA', '#9E3A22', '#E8603F'), 'g': ('#DDF4EC', '#0A5C50', '#0A7C62'), 'a': ('#FBF0D6', '#7A5600', '#C98A00'), 'n': ('#EEF1F4', '#3F4C59', '#7A8794')}
SEG_PHRASE = {'studio': 'studios e academias', 'clinica': 'clínicas', 'salao': 'salões e barbearias', 'imobiliaria': 'imobiliárias', 'escola': 'escolas e cursos',
              'loja': 'lojas', 'servicos': 'escritórios de serviços', 'restaurante': 'restaurantes e delivery', 'lancamento': 'infoprodutos e cursos online'}
VARS = {'segmento': 'studios e academias', 'seg': 'studio', 'conversas': '38', 'respostas': '112', 'horarios': '9', 'vendas': '4', 'primeiro_nome': 'Paula', 'empresa': 'Studio Vita', 'valor': 'R$ 297,00', 'data': '20/11', 'prazo': '24/11', 'plano': 'Profissional, mensal',
        'proxima': '20/12', 'usadas': '1.600', 'limite': '2.000', 'numero': '(11) 90000-0002', 'hora': '11:20', 'dia': '11'}

def D(name, when, ch, tone, eyebrow, icon, subject, pre, title, body, button, wa=None, **k):
    return dict(name=name, when=when, ch=ch, tone=tone, eyebrow=eyebrow, icon=icon, subject=subject, pre=pre, title=title, body=body, button=button, wa=wa, **k)

SUB, PLANS_URL = '/empresa/assinatura', '/assinatura'
DEFS = {
 'welcome': D('Boas-vindas', 'ao criar a conta', 'we', 'i', 'Primeiros passos', 'smile', '{primeiro_nome}, seus 14 dias grátis começaram', 'Sem cartão. Ensine a Nanda em 1 minuto e conecte o WhatsApp.',
    'Que bom ter você aqui, {primeiro_nome}', 'A conta {empresa} está pronta e os 14 dias grátis já começaram, sem cartão.\nO primeiro passo leva 1 minuto: conte à Nanda o que você vende e quanto custa. Depois é conectar o WhatsApp e ver ela atendendo.', ('Ensinar a Nanda', '/comecar'),
    '{primeiro_nome}, a conta da *{empresa}* está pronta e os *14 dias grátis* já começaram, sem cartão. Ensine a Nanda em 1 minuto e conecte o WhatsApp para ela começar a atender.',
    fig=('14 dias', 'grátis · sem cartão'), steps=[(2, 'Ensinar a Nanda', 'o que você vende e quanto custa, em 1 minuto'), (0, 'Conectar o WhatsApp da empresa', 'uns 5 minutos, com o celular do chip na mão'), (0, 'Ver a Nanda atendendo', 'seus clientes respondidos em segundos')],
    tip=('Quer ver o caminho completo antes? Montamos um guia curto, do cadastro à primeira venda.', 'Ler os primeiros passos', '/ajuda/primeiros-passos'), always=True),
 'activate': D('Lembrete para ativar', '1 dia depois do cadastro, se ainda não ativou', 'we', 'i', 'Sua Nanda está pronta', 'spark', 'A Nanda está esperando por você, {primeiro_nome}', 'Ative os 14 dias grátis e ela começa a atender no seu WhatsApp.',
    'A Nanda está pronta para atender', '{primeiro_nome}, ativando os 14 dias grátis a Nanda começa a responder seus clientes no WhatsApp, a qualquer hora.\nHoje não é cobrado nada. A gente avisa 3 dias antes do fim e dá para cancelar com um clique.', ('Ativar meus 14 dias', '/ativar'),
    '{primeiro_nome}, a Nanda da *{empresa}* está pronta. Ativando os *14 dias grátis*, ela começa a atender no seu WhatsApp. Hoje não é cobrado nada e dá para cancelar quando quiser.',
    steps=[(1, 'Conta criada', ''), (2, 'Ativar os 14 dias grátis', 'hoje R$ 0,00'), (0, 'Conectar o WhatsApp', 'uns 5 minutos')], second='Ficou alguma dúvida? É só responder', reply='Tenho uma dúvida'),
 'trial_card_3d': D('Teste com cartão acaba em 3 dias', '3 dias antes da primeira mensalidade', 'we', 'i', 'Teste grátis', 'clock', 'Seu teste grátis termina em {data}', 'A primeira mensalidade entra no cartão em {data}.',
    'Faltam 3 dias de teste', '{primeiro_nome}, passando para avisar com antecedência: em {data} entra a primeira mensalidade de {valor} no cartão cadastrado, e a Nanda segue atendendo sem intervalo.\nSe preferir não continuar, é um clique em Empresa › Assinatura, sem multa.', ('Ver assinatura', SUB),
    '{primeiro_nome}, o teste grátis da {empresa} termina em *{data}*. Nesse dia entra a primeira mensalidade de *{valor}* no cartão. Para cancelar, é um clique em Empresa › Assinatura.',
    fig=('{valor}', 'em {data} · primeira mensalidade'), rows=[('Plano', '{plano}'), ('Cancelar', 'um clique, sem multa')], always=True),
 'ai_pack': D('Pacote extra automático', 'quando entra um pacote automático da Nanda', 'e', 'i', 'Uso da Nanda', 'spark', 'Entrou um pacote extra de 500 atendimentos', 'A Nanda segue atendendo sozinha. {valor} na próxima mensalidade.',
    'A Nanda segue atendendo', '{primeiro_nome}, os atendimentos do mês acabaram e, como o pacote automático está ligado, entraram mais 500. Ninguém fica sem resposta.\nO valor de {valor} vem junto da próxima mensalidade.', ('Ver uso da Nanda', SUB),
    fig=('+500', 'atendimentos da Nanda este mês'), rows=[('Valor', '{valor} na próxima mensalidade'), ('Para desligar', 'Empresa › Assinatura')]),
 'wa_missing': D('WhatsApp ainda não conectado', '1 e 3 dias depois do cadastro, se não conectou', 'we', 'i', 'Falta um passo', 'chat', 'Falta um passo para a Nanda atender', 'Conectar o WhatsApp leva uns 5 minutos.',
    'Falta só conectar o WhatsApp', '{primeiro_nome}, sem o WhatsApp conectado as mensagens dos seus clientes não chegam no Oi Nanda e a Nanda não tem como ajudar.\nSeparamos o que deixa a conexão tranquila: faça pelo celular que tem o chip do número e tenha um cartão para cadastrar na Meta depois.', ('Conectar agora', '/empresa'),
    '{primeiro_nome}, falta só um passo: conectar o WhatsApp da *{empresa}*. Leva uns 5 minutos. Se travar, responde aqui que eu acompanho.',
    steps=[(1, 'Conta criada', ''), (2, 'Conectar o WhatsApp da empresa', 'pelo celular que tem o chip, uns 5 minutos'), (0, 'Ensinar a Nanda', 'preços, horários e serviços')],
    tip=('Antes de conectar, leia o guia de 4 minutos que evita bloqueio do número pela Meta.', 'Conectar sem risco de bloqueio', '/ajuda/conectar-whatsapp'), second='Prefere ajuda? É só responder este e-mail', reply='Quero ajuda'),
 'trial_3d': D('Teste acaba em 3 dias', '3 dias antes do fim do teste', 'we', 'i', 'Teste grátis', 'clock', 'Faltam 3 dias do seu teste grátis', 'Escolha um plano e continue sem intervalo.',
    'Faltam 3 dias de teste', '{primeiro_nome}, para a Nanda continuar atendendo sem intervalo, é só escolher um plano. Nada do que você cadastrou se perde: conversas, contatos e o que você ensinou continuam lá.', ('Ver planos', PLANS_URL),
    '{primeiro_nome}, faltam *3 dias* do teste da {empresa}. Escolhendo um plano até *{data}*, o atendimento segue sem intervalo.',
    fig=('3 dias', 'de teste grátis pela frente'), bar=(11, 14, 'Dia 11 de 14', 'termina em {data}'),
    tip=('Na dúvida entre os planos? O guia explica a diferença em 3 minutos.', 'Planos e cobrança', '/ajuda/assinatura')),
 'trial_1d': D('Teste acaba amanhã', '1 dia antes do fim do teste', 'we', 'i', 'Teste grátis', 'clock', 'Amanhã termina o seu teste grátis', 'Suas conversas e contatos continuam como estão.',
    'O teste termina amanhã', '{primeiro_nome}, escolhendo um plano hoje, a Nanda segue atendendo sem intervalo. Conversas, contatos e respostas continuam como estão.', ('Escolher meu plano', PLANS_URL),
    '{primeiro_nome}, o teste da {empresa} termina *amanhã, {data}*. Escolhendo um plano hoje, a Nanda segue atendendo sem intervalo.',
    fig=('Amanhã', '{data} é o último dia do teste'), bar=(13, 14, 'Dia 13 de 14', 'termina em {data}')),
 'trial_end': D('Teste acabou', 'no dia em que o teste termina', 'we', 'n', 'Conta em consulta', 'pause', 'Seu teste terminou, e está tudo guardado', 'Escolha um plano e volte a atender na hora.',
    'O teste terminou', '{primeiro_nome}, a conta ficou só para consulta: dá para ver tudo, mas não para responder. Escolhendo um plano, o atendimento volta na hora, do ponto onde parou.', ('Escolher meu plano', PLANS_URL),
    '{primeiro_nome}, o teste da {empresa} terminou hoje. Está *tudo guardado*. Com um plano, o atendimento volta na hora.',
    rows=[('Conversas e contatos', 'guardados'), ('O que você ensinou à Nanda', 'guardado'), ('Atendimento', 'volta ao escolher o plano')], second='Quer conversar antes de decidir? É só responder'),
 'charge_3d': D('Renovação em 3 dias', '3 dias antes de cada cobrança', 'we', 'i', 'Lembrete', 'card', 'Sua assinatura renova em {data}', '{valor} no cartão cadastrado. Não precisa fazer nada.',
    'Renovação em 3 dias', '{primeiro_nome}, é só um lembrete da próxima cobrança. Não precisa fazer nada.', ('Ver assinatura', SUB),
    '{primeiro_nome}, a assinatura da {empresa} renova em *{data}*: *{valor}* no cartão cadastrado. Não precisa fazer nada.',
    fig=('{valor}', 'em {data} · no cartão cadastrado'), rows=[('Plano', '{plano}')], second='Trocar o cartão'),
 'paid': D('Pagamento confirmado', 'quando a cobrança é paga', 'e', 'g', 'Pagamento confirmado', 'check', 'Recebemos seu pagamento de {valor}', 'Tudo certo com a sua assinatura.',
    'Recebemos seu pagamento', '{primeiro_nome}, está tudo certo. Obrigado por seguir com a gente: a Nanda continua atendendo com você.', ('Ver assinatura', SUB),
    fig=('{valor}', 'pago em {data}'), rows=[('Plano', '{plano}'), ('Próxima cobrança', '{proxima}')], always=True),
 'refused': D('Cobrança recusada', 'quando o cartão não passa', 'we', 'a', 'Precisa de atenção', 'card', 'A cobrança de {valor} não passou', 'Confira o cartão para a Nanda seguir atendendo.',
    'A cobrança não passou', '{primeiro_nome}, isso acontece: limite, cartão vencido ou bloqueio do banco. Fique tranquilo, o atendimento segue normal por enquanto.\nVale conferir o cartão ou trocar por outro. Leva um minuto.', ('Conferir o cartão', SUB),
    '{primeiro_nome}, a cobrança de *{valor}* da {empresa} não passou no cartão. O atendimento segue normal até *{prazo}*. Vale conferir o cartão.',
    fig=('{valor}', 'não passou no cartão cadastrado'), rows=[('Nova tentativa', 'nos próximos dias'), ('Atendimento garantido até', '{prazo}')], reply='Falar com o suporte', always=True),
 'last_call': D('Último aviso antes de pausar', '1 dia antes do fim do prazo, se segue sem pagar', 'we', 'a', 'Precisa de atenção', 'clock', 'Amanhã é o último dia para acertar a assinatura', 'Trocar o cartão leva um minuto.',
    'O prazo termina amanhã', '{primeiro_nome}, a cobrança ainda não passou. Depois de amanhã a conta fica só para consulta até o pagamento entrar. Trocar o cartão leva um minuto.', ('Trocar o cartão', SUB),
    '{primeiro_nome}, a cobrança de *{valor}* ainda não passou. O prazo vai até *amanhã, {prazo}*. Trocar o cartão leva um minuto.',
    fig=('Amanhã, {prazo}', 'último dia para acertar {valor}'), foot='Se já acertou, pode desconsiderar este aviso.', reply='Falar com o suporte', always=True),
 'paused': D('Conta pausada', 'quando o prazo acaba sem pagamento', 'we', 'n', 'Conta em consulta', 'pause', 'Sua conta está em pausa, e nada foi apagado', 'O atendimento volta com o pagamento.',
    'A conta está em pausa', '{primeiro_nome}, como a cobrança não passou, a conta ficou só para consulta. Nada foi apagado. Assim que o pagamento entrar, o atendimento volta sozinho.', ('Regularizar agora', SUB),
    '{primeiro_nome}, a conta da {empresa} ficou *só para consulta* porque a cobrança não passou. Nada foi apagado. O atendimento volta assim que o pagamento entrar.',
    fig=('{valor}', 'em aberto'), rows=[('Seus dados', 'guardados'), ('Atendimento', 'volta com o pagamento')], reply='Falar com o suporte', always=True),
 'cancelled': D('Cancelamento confirmado', 'quando o cliente cancela', 'e', 'n', 'Cancelamento confirmado', 'bye', 'Cancelamento confirmado', 'Sem novas cobranças. A conta segue ativa até {data}.',
    'Assinatura cancelada', '{primeiro_nome}, cancelamos a assinatura como você pediu. Até {data} tudo segue funcionando, e depois a conta fica guardada só para consulta.\nSe mudar de ideia, é só reativar. E se tiver um minuto, conta para a gente o que faltou: lemos todas as respostas.', ('Reativar assinatura', SUB),
    fig=('{data}', 'a conta segue ativa até esta data'), rows=[('Novas cobranças', 'nenhuma'), ('Seus dados', 'guardados')], second='Quer contar o motivo? É só responder', ghost=True, always=True),
 'ai_80': D('Nanda em 80% do plano', 'ao usar 80% dos atendimentos do mês', 'we', 'i', 'Uso da Nanda', 'spark', 'A Nanda já atendeu {usadas} clientes este mês', 'Atendimento movimentado por aí.',
    'A Nanda está trabalhando bastante', '{primeiro_nome}, ótimo sinal: atendimento movimentado. Para ela seguir atendendo sozinha até o fim do mês, dá para adicionar um pacote de 500 atendimentos por R$ 59 ou deixar o pacote automático ligado.', ('Ver uso da Nanda', SUB),
    '{primeiro_nome}, a Nanda já atendeu *{usadas} dos {limite}* atendimentos do plano este mês. Para ela seguir sozinha até o fim do mês, dá para adicionar um pacote em Empresa › Assinatura.',
    fig=('80%', 'dos atendimentos do mês já usados'), bar=(80, 100, '{usadas} de {limite} atendimentos', 'zera no dia 1º')),
 'ai_100': D('Nanda chegou ao limite', 'ao usar 100% dos atendimentos do mês', 'we', 'a', 'Uso da Nanda', 'spark', 'A Nanda usou os {limite} atendimentos do mês', 'Ela segue sugerindo as respostas para a sua equipe.',
    'A Nanda passou a sugerir as respostas', '{primeiro_nome}, os atendimentos do mês acabaram. Ela não parou: segue sugerindo as respostas para a sua equipe enviar.\nCom um pacote de 500 atendimentos por R$ 59, ela volta a atender sozinha na hora.', ('Adicionar pacote', SUB),
    '{primeiro_nome}, a Nanda usou os *{limite} atendimentos* do mês. Ela segue sugerindo as respostas para a equipe. Com um pacote de 500 por R$ 59, volta a atender sozinha na hora.',
    fig=('100%', 'dos atendimentos do mês usados'), bar=(100, 100, '{limite} de {limite} atendimentos', 'zera no dia 1º'), always=True),
 'wa_down': D('WhatsApp desconectou', 'quando o número da empresa cai', 'we', 'a', 'Precisa de atenção', 'chat', 'O WhatsApp da sua empresa desconectou', 'Reconectar leva poucos minutos.',
    'O WhatsApp desconectou', '{primeiro_nome}, enquanto estiver desconectado, as mensagens novas dos clientes não aparecem no Oi Nanda. Reconectar leva poucos minutos.', ('Reconectar', '/empresa'),
    '{primeiro_nome}, o WhatsApp da {empresa} *desconectou* do Oi Nanda às {hora}. Mensagens novas não aparecem até reconectar. Leva poucos minutos.',
    rows=[('Número', '{numero}'), ('Desconectou', 'hoje, {hora}')], tip=('Se a Meta pediu alguma ação, o guia mostra o caminho.', 'Ver o guia de conexão', '/ajuda/conectar-whatsapp'), reply='Quero ajuda', always=True),
 # ---- dicas do começo: só por e-mail, cada uma uma vez, e só quando faz sentido ----
 'tip_teach': D('Dica: ensinar a Nanda', '2 dias depois de começar o teste, se a Nanda ainda não sabe o básico do negócio', 'e', 'i', 'Dica do Oi Nanda', 'spark', '10 minutos que fazem a Nanda responder muito melhor', 'Ela só usa o que você ensinou. Veja o que vale preencher primeiro.',
    'Ensine a Nanda em 10 minutos', '{primeiro_nome}, a Nanda só responde com o que você ensinou. O que ela não souber, não inventa: passa para o seu time. Quanto mais completo, menos ela precisa chamar alguém.\nNão precisa escrever do zero: na tela da Nanda tem um exemplo pronto para {segmento}. É só trocar o que for diferente.', ('Ensinar a Nanda', '/nanda'),
    steps=[(0, 'Sobre o negócio', 'o que é, onde fica, horários'), (0, 'Preços e condições', 'valores, pacotes, formas de pagamento'), (0, 'Perguntas frequentes', 'o que os clientes sempre perguntam'), (0, 'Teste uma conversa', 'escreva como um cliente escreveria')],
    steps_title='O que preencher primeiro', tip=('Passo a passo com exemplos, incluindo como testar antes de deixar ela sozinha.', 'Ler o guia da Nanda', '/ajuda/ensinar-nanda')),
 'tip_autos': D('Dica: ligar as automações', '4 dias depois de começar o teste, se nenhuma automação está ligada', 'e', 'i', 'Dica do Oi Nanda', 'spark', 'As 3 automações que mais ajudam {segmento}', 'Mensagens que saem sozinhas na hora certa, para nenhuma venda se perder.',
    'Deixe o Oi Nanda lembrar por você', '{primeiro_nome}, boa parte das vendas se perde por esquecimento: o cliente sumiu, o orçamento ficou parado, ninguém lembrou da renovação.\nAs automações mandam a mensagem certa sozinhas, com o nome do cliente, só das 8h às 21h. Para {segmento}, estas são as que mais ajudam no começo:', ('Ligar as automações', '/automacoes'),
    steps=[(0, 'Lembrete', ''), (0, 'Retomada', ''), (0, 'Pós-venda', '')], steps_title='Recomendadas para você', tip=('Como funcionam, quando saem e quanto custa cada mensagem.', 'Ler o guia de automações', '/ajuda/automacoes')),
 'week1': D('Resumo da primeira semana', '7 dias depois de conectar o WhatsApp, se já teve conversa', 'e', 'g', 'Sua primeira semana', 'check', 'Sua primeira semana com a Nanda', '{conversas} conversas, {respostas} respostas da Nanda e mais.',
    'Uma semana de Oi Nanda', '{primeiro_nome}, olha o que aconteceu nos primeiros 7 dias com o WhatsApp conectado. Os números completos estão em Relatórios.', ('Ver meus relatórios', '/relatorios'),
    fig=('{conversas}', 'conversas nos primeiros 7 dias'), rows=[('Respostas da Nanda', '{respostas}'), ('Horários marcados', '{horarios}'), ('Vendas registradas', '{vendas}')],
    tip=('O guia do seu segmento mostra o que acompanhar daqui para frente.', 'Ler o guia do seu segmento', '/ajuda/seg-{seg}')),
}
EDIT = ('subject', 'pre', 'title', 'body', 'wa')
OPTIONAL = ('wa_missing', 'activate', 'trial_3d', 'trial_1d', 'charge_3d', 'paid', 'ai_80', 'ai_pack', 'tip_teach', 'tip_autos', 'week1')  # o cliente pode desligar; os demais sempre chegam

def fill(text, v):
    return re.sub(r'\{(\w+)\}', lambda m: str(v.get(m.group(1), m.group(0))), text or '')

def money(v): return mailer.money(v)
def day(d):
    if not d: return ''
    if getattr(d, 'tzinfo', 1) is None: d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(SP).strftime('%d/%m')

async def toggles():
    d = await db.settings.find_one({'id': 'notices'}, {'_id': 0}) or {}
    return {k: d.get(k, True) for k in DEFS}

async def texts(key):
    """Texto em uso: o padrão com o que o admin mudou por cima."""
    d = await db.settings.find_one({'id': 'notice_texts'}, {'_id': 0}) or {}
    return {**{f: DEFS[key][f] for f in EDIT}, **{f: v for f, v in (d.get(key) or {}).items() if f in EDIT and v}}

# ---------- e-mail ----------
FOUNDER = {'welcome', 'week1', 'cancelled'}  # e-mails assinados pelo fundador: começo e despedida

def email_html(key, t, v):
    n = DEFS[key]; e = html.escape; F = lambda x: fill(x, v)
    if key == 'tip_autos' and not v.get('_steps'):  # prévia e teste: as recomendadas do segmento do exemplo
        from niches import top3
        v = {**v, '_steps': [(0, r['name'], r.get('why') or '') for r in top3(v.get('seg') or 'studio')]}
    c = {'tone': n['tone'], 'icon': f'{n["icon"]}-{n["tone"]}', 'eyebrow': n['eyebrow'], 'title': F(t['title']), 'pre': F(t['pre']),
         'body': [e(F(p)) for p in t['body'].split('\n') if p.strip()],
         'fig': (F(n['fig'][0]), F(n['fig'][1])) if n.get('fig') else None,
         'steps': v.get('_steps') or n.get('steps'), 'steps_title': n.get('steps_title'), 'rows': [(a, F(b)) for a, b in n.get('rows') or []],
         'tip': (F(n['tip'][0]), F(n['tip'][1]), APP + F(n['tip'][2])) if n.get('tip') else None,
         'button': (n['button'][0], APP + n['button'][1]), 'ghost': n.get('ghost'),
         'second': (n['second'], APP + n['button'][1]) if n.get('second') else None, 'foot': n.get('foot'),
         'why': f'Você recebe este aviso por ser responsável pela conta {v.get("empresa", "")} no Oi Nanda.', 'prefs': True,
         'account': v.get('empresa') or None, 'sign': 'founder' if key in FOUNDER else None}
    if n.get('bar'):
        a, b, l, r = n['bar']; c['bar'] = (v.get('bar_now', a), v.get('bar_total', b), F(v.get('bar_label') or l), F(r))
    return mailer.render(c)

def email_text(key, t, v):
    n = DEFS[key]; L = [fill(t['title'], v), '']
    if n.get('fig'): L += [fill(n['fig'][0], v) + ' ' + fill(n['fig'][1], v), '']
    L += [fill(t['body'], v), ''] + [f'{a}: {fill(b, v)}' for a, b in n.get('rows') or []]
    if v.get('_steps') or n.get('steps'): L += [''] + [f'- {x[1]}' + (f' ({fill(x[2], v)})' if x[2] else '') for x in (v.get('_steps') or n['steps'])]
    if n.get('tip'): L += ['', f'Guia rápido: {fill(n["tip"][1], v)}: {APP}{fill(n["tip"][2], v)}']
    return '\n'.join(L + ['', f'{n["button"][0]}: {APP}{n["button"][1]}'])

def wa_text(t, v, mark=True):
    s = fill(t.get('wa') or '', v)
    return s if mark else s.replace('*', '')

# ---------- WhatsApp (número do Oi Nanda) ----------
async def platform():
    """Número que manda os avisos: o WhatsApp conectado na conta do próprio Oi Nanda (a do admin).
    As variáveis PLATFORM_WA_* continuam valendo, se alguém preferir informar à mão."""
    if os.environ.get('PLATFORM_WA_PHONE_ID') and os.environ.get('PLATFORM_WA_TOKEN'):
        return {'phone': os.environ['PLATFORM_WA_PHONE_ID'], 'token': os.environ['PLATFORM_WA_TOKEN'], 'waba': os.environ.get('PLATFORM_WA_WABA_ID')}
    from core import ADMIN_EMAIL
    me = await db.users.find_one({'email': ADMIN_EMAIL, 'role': 'owner'}, {'_id': 0, 'company_id': 1}) if ADMIN_EMAIL else None
    w = await db.wa_numbers.find_one({'company_id': me['company_id'], 'status': 'connected'}, {'_id': 0}) if me else None
    if not w or not w.get('token_enc'): return None
    if w.get('alert'): return None  # número com bloqueio ou restrição na Meta: os avisos saem só por e-mail até liberar
    import whatsapp
    return {'phone': w['phone_number_id'], 'token': whatsapp.decrypt(w['token_enc']), 'waba': w.get('waba_id'), 'display': w.get('display_phone')}

async def wa_ready(): return bool(await platform())

def wa_params(text):
    """Texto com {variaveis} -> corpo do modelo da Meta ({{1}}, {{2}}...) e a ordem das variáveis."""
    order = []
    def rep(m):
        if m.group(1) not in order: order.append(m.group(1))
        return '{{%d}}' % (order.index(m.group(1)) + 1)
    return re.sub(r'\{(\w+)\}', rep, text), order

async def wa_template(key):
    """Último modelo aprovado deste aviso (o texto novo só entra depois de a Meta aprovar)."""
    return await db.notice_templates.find_one({'key': key, 'status': 'APPROVED'}, {'_id': 0}, sort=[('version', -1)])

async def wa_submit(key, text):
    """Manda o texto para aprovação da Meta. Devolve o registro do modelo (status PENDING) ou None se o número não está configurado."""
    P = await platform(); waba = (P or {}).get('waba')
    last = await db.notice_templates.find_one({'key': key}, {'_id': 0}, sort=[('version', -1)])
    if last and last['text'] == text: return last
    doc = {'id': uid(), 'key': key, 'version': (last or {}).get('version', 0) + 1, 'text': text, 'status': 'DRAFT', 'at': now()}
    doc['name'] = f'aviso_{key}_v{doc["version"]}'
    if P and waba:
        n = DEFS[key]; body, order = wa_params(text)
        comps = [{'type': 'HEADER', 'format': 'TEXT', 'text': re.sub(r'\{\w+\}', '', n['title']).strip()[:60]},
                 {'type': 'BODY', 'text': body, **({'example': {'body_text': [[VARS.get(k, k) for k in order]]}} if order else {})},
                 {'type': 'FOOTER', 'text': 'Oi Nanda · avisos da sua conta'},
                 {'type': 'BUTTONS', 'buttons': [{'type': 'URL', 'text': n['button'][0][:25], 'url': APP + n['button'][1]}] + ([{'type': 'QUICK_REPLY', 'text': n['reply'][:25]}] if n.get('reply') else [])}]
        try:
            import whatsapp
            r = await whatsapp.graph('POST', f'/{waba}/message_templates', P['token'], {'name': doc['name'], 'language': 'pt_BR', 'category': 'UTILITY', 'components': comps})
            doc.update(status=(r.get('status') or 'PENDING').upper(), meta_id=r.get('id'))
        except Exception as ex:
            doc.update(status='REJECTED', error=str(getattr(ex, 'detail', ex))[:300])
    await db.notice_templates.insert_one({**doc})
    return doc

async def wa_refresh():
    """Confere na Meta os modelos que ainda esperam aprovação."""
    P = await platform()
    if not P: return
    import whatsapp
    for t in await db.notice_templates.find({'status': {'$in': ['PENDING', 'IN_APPEAL']}, 'meta_id': {'$ne': None}}, {'_id': 0}).to_list(100):
        try:
            r = await whatsapp.graph('GET', f'/{t["meta_id"]}', P['token'], params={'fields': 'status'})
            if r.get('status') and r['status'].upper() != t['status']: await db.notice_templates.update_one({'id': t['id']}, {'$set': {'status': r['status'].upper()}})
        except Exception: log.info('não conferiu o modelo %s', t['name'])

WA_SENT = []  # nos testes: o que teria saído pelo WhatsApp
async def wa_send(phone, tpl, v):
    _, order = wa_params(tpl['text'])
    payload = {'messaging_product': 'whatsapp', 'to': phone, 'type': 'template', 'template': {'name': tpl['name'], 'language': {'code': 'pt_BR'},
               'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': str(v.get(k, ''))} for k in order]}] if order else []}}
    if os.environ.get('MONGO_URL') == 'mock': WA_SENT.append({'to': phone, 'name': tpl['name'], 'vars': {k: v.get(k) for k in order}}); return True
    try:
        import whatsapp
        P = await platform()
        await whatsapp.graph('POST', f'/{P["phone"]}/messages', P['token'], payload); return True
    except Exception as ex:
        log.warning('aviso por WhatsApp não saiu (%s): %s', tpl['name'], getattr(ex, 'detail', ex)); return False

# ---------- envio ----------
async def base_vars(company, owner):
    b = company.get('billing') or {}
    from billing import PLANS
    plan = PLANS.get(b.get('plan') or company.get('plan') or '', {}).get('name', '')
    seg = company.get('segment') if company.get('segment') in SEG_PHRASE else 'outro'
    return {'primeiro_nome': (owner.get('name') or '').split(' ')[0] or 'Olá', 'empresa': company.get('name') or '', 'seg': seg, 'segmento': SEG_PHRASE.get(seg, 'o seu negócio'),
            'valor': money(b.get('amount')), 'plano': (plan + (', anual' if b.get('cycle') == 'annual' else ', mensal')) if plan else '', 'data': '', 'prazo': '', 'proxima': day(b.get('next_payment'))}

def quiet():
    if os.environ.get('MONGO_URL') == 'mock': return False  # os testes rodam a qualquer hora
    h = now().astimezone(SP).hour
    return h < 8 or h >= 20

async def deliver(company, key, v=None, once=None, manual=False, only=None):
    """Manda o aviso `key` ao responsável da conta. `once` evita repetir (mesma chave = mesmo aviso).

    Avisos que não são urgentes respeitam o horário (8h às 20h) e o limite de um por dia: devolve None e o próximo ciclo tenta de novo.
    """
    n = DEFS[key]
    if not manual and company.get('test'): return False  # conta de teste marcada pelo admin: sem avisos automáticos
    if not manual:
        if not (await toggles()).get(key): return False
        if once and await db.notice_log.find_one({'company_id': company['id'], 'once': once}): return False
        if not n.get('always'):
            if quiet(): return None
            if await db.notice_log.find_one({'company_id': company['id'], 'manual': {'$ne': True}, 'soft': True, 'at': {'$gte': now() - timedelta(hours=20)}}): return None
    if not manual and key in (company.get('notice_off') or []): return False
    owner = await mailer.owner_of(company)
    if not owner: return False
    v = {**await base_vars(company, owner), **(v or {})}; t = await texts(key)
    mail = wa = False
    if only in (None, 'email'):
        mail = await mailer.send(owner['email'], fill(t['subject'], v), email_html(key, t, v), email_text(key, t, v))
    if only in (None, 'wa') and 'w' in n['ch'] and company.get('notice_wa') is not False and await wa_ready():
        from channels import norm_phone
        phone = norm_phone(company.get('notice_phone') or owner.get('wa_phone') or company.get('phone') or ''); tpl = await wa_template(key)
        if phone and tpl: wa = await wa_send(phone, tpl, v)
    await db.notice_log.insert_one({'id': uid(), 'company_id': company['id'], 'company': company.get('name'), 'key': key, 'name': n['name'], 'subject': fill(t['subject'], v),
                                    'to': owner['email'], 'email': bool(mail), 'wa': bool(wa), 'once': once, 'manual': manual, 'soft': not n.get('always'), 'at': now()})
    return True

# ---------- gatilhos ----------
def aware(d): return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d

async def tick():
    """Roda junto das automações: avisos que dependem do tempo (teste, cobrança, prazo, WhatsApp sem conectar)."""
    from billing import GRACE_DAYS, plan_of
    n = now()
    for c in await db.companies.find({'plan': 'trial', 'trial_ends_at': {'$gt': n - timedelta(days=2), '$lte': n + timedelta(days=3)}}, {'_id': 0}).to_list(500):
        end = aware(c['trial_ends_at']); left = end - n; v = {'data': day(end)}
        total = max(1, round((end - aware(c.get('created_at') or end - timedelta(days=14))).total_seconds() / 86400))
        if timedelta(days=2) < left <= timedelta(days=3) and not c.get('mail_trial_3d'):
            await deliver(c, 'trial_3d', {**v, 'bar_now': total - 3, 'bar_total': total, 'bar_label': f'Dia {total - 3} de {total}'}, once='trial_3d')
        elif timedelta(0) < left <= timedelta(days=1):
            await deliver(c, 'trial_1d', {**v, 'bar_now': total - 1, 'bar_total': total, 'bar_label': f'Dia {total - 1} de {total}'}, once='trial_1d')
        elif left <= timedelta(0) and not c.get('mail_trial_end'):
            await deliver(c, 'trial_end', v, once='trial_end')
    # criou a conta e não ativou os 14 dias: um lembrete gentil no dia seguinte
    for c in await db.companies.find({'plan': 'demo', 'created_at': {'$gt': n - timedelta(days=3), '$lte': n - timedelta(days=1)}}, {'_id': 0}).to_list(500):
        await deliver(c, 'activate', {}, once='activate_1')
    # teste com cartão: 3 dias antes da primeira mensalidade
    from billing import PLANS
    for c in await db.companies.find({'billing.trial_until': {'$gt': n + timedelta(days=2), '$lte': n + timedelta(days=3)}, 'billing.status': 'active'}, {'_id': 0}).to_list(500):
        b = c['billing']; P = PLANS.get(b.get('plan')) or {}
        await deliver(c, 'trial_card_3d', {'data': day(b['trial_until']), 'valor': money(b.get('amount') or 0), 'plano': P.get('name', '') + (', anual' if b.get('cycle') == 'annual' else ', mensal')},
                      once='trial_card_3d:' + str(b.get('sub_id')))
    # WhatsApp ainda não conectado: 1 e 3 dias depois de começar o teste
    trials = await db.companies.find({'$or': [{'plan': 'trial', 'created_at': {'$gt': n - timedelta(days=5), '$lte': n - timedelta(days=1)}},
                                              {'billing.trial_until': {'$gt': n}, 'billing.since': {'$gt': n - timedelta(days=5), '$lte': n - timedelta(days=1)}}]}, {'_id': 0}).to_list(500)
    for c in trials:
        if await db.wa_numbers.find_one({'company_id': c['id']}, {'_id': 1}): continue
        age = n - aware((c.get('billing') or {}).get('since') if (c.get('billing') or {}).get('trial_until') else c['created_at'])
        if age < timedelta(days=2): await deliver(c, 'wa_missing', {'data': day(c.get('trial_ends_at'))}, once='wa_missing_1')
        elif timedelta(days=3) <= age < timedelta(days=4): await deliver(c, 'wa_missing', {'data': day(c.get('trial_ends_at'))}, once='wa_missing_3')
    # dicas do começo: ensinar a Nanda (dia 2) e ligar as automações do segmento (dia 4), só se ainda não fez
    from niches import top3
    for c in await db.companies.find({'$or': [{'plan': 'trial', 'created_at': {'$gt': n - timedelta(days=6)}},
                                              {'billing.since': {'$gt': n - timedelta(days=6)}, 'billing.trial_until': {'$gt': n}}]}, {'_id': 0}).to_list(500):
        b = c.get('billing') or {}
        age = n - aware(b.get('since') if b.get('trial_until') and b.get('since') else c.get('created_at') or n)
        if timedelta(days=2) <= age < timedelta(days=3):
            st = await db.ai_settings.find_one({'company_id': c['id']}, {'_id': 0, 'about': 1, 'offer': 1}) or {}
            if not ((st.get('about') or '').strip() and (st.get('offer') or '').strip()):
                await deliver(c, 'tip_teach', {}, once='tip_teach')
        elif timedelta(days=4) <= age < timedelta(days=5):
            if not await db.automations.find_one({'company_id': c['id'], 'active': True}, {'_id': 1}):
                steps = [(0, r['name'], r.get('why') or '') for r in top3(c.get('segment'))]
                if steps: await deliver(c, 'tip_autos', {'_steps': steps}, once='tip_autos')
    # uma semana de WhatsApp conectado: resumo do que aconteceu (só se teve conversa)
    for w in await db.wa_numbers.find({'status': 'connected', 'connected_at': {'$gt': n - timedelta(days=8), '$lte': n - timedelta(days=7)}}, {'_id': 0, 'company_id': 1, 'connected_at': 1}).to_list(500):
        c = await db.companies.find_one({'id': w['company_id']}, {'_id': 0})
        if not c or await db.notice_log.find_one({'company_id': c['id'], 'once': 'week1'}, {'_id': 1}): continue
        since = aware(w['connected_at']); q = {'company_id': c['id']}
        convs = len(await db.messages.distinct('contact_id', {**q, 'dir': 'in', 'at': {'$gte': since}}))
        if not convs: continue
        f = lambda x: f'{x:,}'.replace(',', '.')
        v = {'conversas': f(convs), 'respostas': f(await db.messages.count_documents({**q, 'dir': 'out', 'by': {'$regex': r'\(IA\)$'}, 'at': {'$gte': since}})),
             'horarios': f(await db.appointments.count_documents({**q, 'created_at': {'$gte': since}})),
             'vendas': f(await db.sales.count_documents({**q, 'status': 'ok', 'date': {'$gte': since}}))}
        await deliver(c, 'week1', v, once='week1')
    # renovação em 3 dias (a primeira, depois do teste, tem aviso próprio)
    for c in await db.companies.find({'billing.status': 'active', 'billing.next_payment': {'$gt': n + timedelta(days=2), '$lte': n + timedelta(days=3)},
                                      'billing.trial_until': {'$not': {'$gt': n}}}, {'_id': 0}).to_list(500):
        await deliver(c, 'charge_3d', {'data': day(c['billing']['next_payment'])}, once='charge_3d:' + day(c['billing']['next_payment']))
    # cobrança recusada que segue sem pagar: último aviso e pausa
    for p in await db.payments.find({'status': 'refused', 'due': {'$gt': n - timedelta(days=GRACE_DAYS + 3), '$lte': n - timedelta(days=GRACE_DAYS - 1)}}, {'_id': 0}).to_list(500):
        c = await db.companies.find_one({'id': p['company_id']}, {'_id': 0})
        if not c or await db.payments.find_one({'company_id': c['id'], 'status': 'paid', 'due': {'$gte': p['due']}}): continue
        limit = aware(p['due']) + timedelta(days=GRACE_DAYS); v = {'valor': money(p['amount']), 'prazo': day(limit)}
        if n < limit: await deliver(c, 'last_call', v, once='last_call:' + str(p['id']))
        elif plan_of(c)[0] == 'readonly': await deliver(c, 'paused', v, once='paused:' + str(p['id']))

async def ai_mark(company_id, used=None):
    """Chamado a cada atendimento novo (ou sugestão, no Essencial): avisa uma vez ao chegar em 80% e em 100% do mês."""
    from billing import ai_used, quota, limits
    c = await db.companies.find_one({'id': company_id}, {'_id': 0})
    if not c: return
    lim = (await quota(c)) or limits(c)['sug']
    used = await ai_used(company_id) if used is None else used
    if not lim or used not in (lim, int(lim * 0.8)): return
    f = lambda x: f'{x:,}'.replace(',', '.')
    m = now().strftime('%Y-%m')
    await deliver(c, 'ai_100' if used == lim else 'ai_80', {'usadas': f(used), 'limite': f(lim), 'bar_now': used, 'bar_total': lim},
                  once=('ai_100:' if used == lim else 'ai_80:') + m + ':' + str(lim))

async def check_numbers():
    """Confere se o WhatsApp de cada empresa ainda responde na Meta. Se o acesso caiu, marca e avisa o responsável."""
    import whatsapp
    for w in await db.wa_numbers.find({'status': 'connected'}, {'_id': 0}).to_list(2000):
        try: await whatsapp.graph('GET', f'/{w["phone_number_id"]}', whatsapp.decrypt(w['token_enc']), params={'fields': 'id'})
        except Exception as ex:
            if getattr(ex, 'status_code', 0) not in (400, 401, 403) or 'token' not in str(getattr(ex, 'detail', '')).lower(): continue
            await db.wa_numbers.update_one({'company_id': w['company_id']}, {'$set': {'status': 'disconnected', 'disconnected_at': now()}})
            c = await db.companies.find_one({'id': w['company_id']}, {'_id': 0})
            if c: await deliver(c, 'wa_down', {'numero': w.get('display_phone') or '', 'hora': now().astimezone(SP).strftime('%H:%M')}, once='wa_down:' + now().strftime('%Y-%m-%d'))

# ---------- o cliente escolhe os avisos que recebe (Empresa › Avisos do Oi Nanda) ----------
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from core import owners, audit
router = APIRouter()

def prefs_out(company, owner):
    off = company.get('notice_off') or []
    return {'whatsapp': company.get('notice_wa') is not False, 'phone': company.get('notice_phone') or company.get('phone') or '', 'email': (owner or {}).get('email'),
            'optional': [{'key': k, 'name': DEFS[k]['name'], 'when': DEFS[k]['when'], 'on': k not in off} for k in OPTIONAL],
            'always': [DEFS[k]['name'] for k in DEFS if k not in OPTIONAL and k != 'welcome']}

@router.get('/company/notices')
async def prefs_get(user=Depends(owners)):
    return prefs_out(user['company'], user)

class PrefsIn(BaseModel):
    whatsapp: Optional[bool] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    off: Optional[List[str]] = None

@router.put('/company/notices')
async def prefs_put(body: PrefsIn, request: Request, user=Depends(owners)):
    from channels import norm_phone
    ch = {}
    if body.whatsapp is not None: ch['notice_wa'] = body.whatsapp
    if body.phone is not None:
        if body.phone.strip() and not norm_phone(body.phone): raise HTTPException(400, 'Confira o número: DDD + celular, ex.: (11) 91234-5678.')
        ch['notice_phone'] = body.phone.strip()
    if body.off is not None: ch['notice_off'] = [k for k in body.off if k in OPTIONAL]
    if ch:
        await db.companies.update_one({'id': user['company_id']}, {'$set': ch})
        await audit(request, user, 'Mudou os avisos que recebe do Oi Nanda')
    return prefs_out(await db.companies.find_one({'id': user['company_id']}, {'_id': 0}), user)
