"""Pacotes por nicho.

Um núcleo só (conversas, funil, agenda, Nanda, automações) e, por segmento, o que já vem pronto:
etapas do funil, serviço da agenda, exemplo para ensinar a Nanda e automações sugeridas ("receitas").
Para incluir um nicho novo, basta acrescentar uma entrada em NICHES.

Nas receitas, a etapa é indicada por um pedaço do nome (ex.: 'marcad'), resolvida nas etapas da empresa.
Variáveis das mensagens: {primeiro_nome}, {nome}, {empresa}, {servico}, {data}, {hora}.
"""
from fastapi import APIRouter

router = APIRouter()

def R(key, name, trigger, hours, message, stage=None, category='MARKETING', why=''):
    return {'key': key, 'name': name, 'trigger': trigger, 'hours': hours, 'stage': stage, 'message': message, 'category': category, 'why': why}

# receitas que servem para qualquer negócio que tem agenda
AGENDA_RECIPES = [
    R('lembrete_24h', 'Lembrete 1 dia antes', 'before_appt', 24,
      'Oi, {primeiro_nome}! Passando para lembrar: {servico} amanhã, {data} às {hora}. Posso confirmar sua presença?', category='UTILITY',
      why='Reduz falta. Vai 24 h antes de cada horário marcado.'),
    R('lembrete_3h', 'Lembrete no dia', 'before_appt', 3,
      'Oi, {primeiro_nome}! Hoje às {hora} tem {servico} aqui no {empresa}. Te esperamos!', category='UTILITY',
      why='Um toque curto poucas horas antes.'),
    R('faltou', 'Faltou: oferecer novo horário', 'appt_no_show', 2,
      'Oi, {primeiro_nome}! Sentimos sua falta hoje. Quer que eu veja um novo horário para você?',
      why='Quando o horário é marcado como "Faltou", chama para remarcar.'),
]
FOLLOWUP = [
    R('esfriou_1', 'Sem resposta há 1 dia', 'no_reply', 24,
      'Oi, {primeiro_nome}! Conseguiu ver minha última mensagem? Fico à disposição para qualquer dúvida.',
      why='Retoma quem parou de responder durante a negociação.'),
    R('esfriou_3', 'Sem resposta há 3 dias', 'no_reply', 72,
      'Oi, {primeiro_nome}! Vou deixar seu atendimento em aberto por aqui. Quando quiser continuar, é só me chamar!',
      why='Última tentativa, sem pressionar.'),
]

NICHES = {
    'studio': {
        'label': 'Studio, academia ou box', 'short': 'Studio e academia',
        'stages': [('Novo contato', 'open'), ('Aula experimental marcada', 'open'), ('Fez a aula', 'open'), ('Negociando', 'open'), ('Matriculou', 'won'), ('Perdido', 'lost')],
        'service': ('Aula experimental', 60, 3),
        'ai': {'about': 'Studio de pilates e funcional no bairro X. Turmas pequenas, até 3 alunos por professor.',
               'offer': 'Plano mensal: R$ 289 (2x por semana).\nTrimestral: R$ 790.\nAula experimental grátis.',
               'goal': 'Agendar a aula experimental.',
               'qualify': 'Qual o objetivo (dor, condicionamento, emagrecer)?\nJá praticou antes?\nQual período prefere: manhã, tarde ou noite?',
               'rules': 'Não fazer avaliação de saúde pelo WhatsApp.\nNão dar desconto: chamar o time.',
               'faq': 'Tem estacionamento? Sim, conveniado.\nPrecisa levar algo? Roupa confortável e meia antiderrapante.'},
        'recipes': AGENDA_RECIPES + FOLLOWUP + [
            R('pos_aula', 'Depois da aula experimental', 'appt_done', 3,
              'Oi, {primeiro_nome}! O que achou da aula? Se quiser, já te mando os planos para começar.', why='Puxa a matrícula enquanto a experiência está fresca.'),
            R('sumido', 'Aluno sumido há 30 dias', 'no_reply', 720, 'Oi, {primeiro_nome}! Sentimos sua falta por aqui. Quer que eu reserve um horário para você voltar?',
              stage='matricul', why='Reativa quem matriculou e parou de aparecer.')],
    },
    'clinica': {
        'label': 'Clínica de estética, odonto ou saúde', 'short': 'Clínica',
        'stages': [('Novo contato', 'open'), ('Avaliação marcada', 'open'), ('Avaliou', 'open'), ('Orçamento enviado', 'open'), ('Fechou', 'won'), ('Perdido', 'lost')],
        'service': ('Avaliação', 30, 1),
        'ai': {'about': 'Clínica de estética facial e corporal.',
               'offer': 'Limpeza de pele: R$ 180 (1 h).\nAvaliação gratuita para harmonização.',
               'goal': 'Agendar a avaliação.',
               'qualify': 'Qual procedimento tem interesse?\nJá fez antes?\nMelhor dia e período?',
               'rules': 'Não passar preço de injetáveis pelo WhatsApp: oferecer avaliação.\nNão dar orientação médica.',
               'faq': 'Formas de pagamento: Pix ou cartão em até 6x.'},
        'recipes': AGENDA_RECIPES + FOLLOWUP + [
            R('orcamento', 'Orçamento sem resposta', 'no_reply', 48, 'Oi, {primeiro_nome}! Ficou alguma dúvida sobre o orçamento? Posso te ajudar a escolher a melhor data.',
              stage='orçamento', why='Quem recebe orçamento e some costuma fechar com um lembrete.')],
    },
    'salao': {
        'label': 'Salão, barbearia ou beleza', 'short': 'Salão e barbearia',
        'stages': [('Novo contato', 'open'), ('Horário marcado', 'open'), ('Atendido', 'won'), ('Não veio', 'lost')],
        'service': ('Corte', 45, 1),
        'ai': {'about': 'Barbearia com 3 profissionais.', 'offer': 'Corte: R$ 50.\nBarba: R$ 35.\nCorte + barba: R$ 75.',
               'goal': 'Marcar o horário.', 'qualify': 'Qual serviço?\nTem preferência de profissional?',
               'rules': 'Não prometer profissional específico sem conferir a agenda.', 'faq': 'Aceita Pix e cartão.'},
        'recipes': AGENDA_RECIPES + [
            R('retorno_30', 'Hora de voltar (30 dias)', 'no_reply', 720, 'Oi, {primeiro_nome}! Já faz um mês do seu último horário. Quer que eu reserve o próximo?',
              stage='atendid', why='Traz o cliente de volta no ciclo certo.')],
    },
    'imobiliaria': {
        'label': 'Imobiliária ou corretor', 'short': 'Imobiliária',
        'stages': [('Novo lead', 'open'), ('Qualificado', 'open'), ('Visita marcada', 'open'), ('Proposta', 'open'), ('Negócio fechado', 'won'), ('Perdido', 'lost')],
        'service': ('Visita ao imóvel', 60, 1),
        'ai': {'about': 'Imobiliária com imóveis para venda e locação na região X.', 'offer': 'Imóveis de R$ 300 mil a R$ 1,5 milhão. Locação a partir de R$ 1.800.',
               'goal': 'Entender o que o cliente procura e marcar a visita.',
               'qualify': 'Compra ou aluguel?\nBairro de interesse?\nFaixa de preço?\nQuantos quartos?\nVai financiar?',
               'rules': 'Não confirmar disponibilidade de imóvel sem o corretor.\nNão negociar valor.', 'faq': ''},
        'recipes': AGENDA_RECIPES + FOLLOWUP + [
            R('pos_visita', 'Depois da visita', 'appt_done', 4, 'Oi, {primeiro_nome}! O que achou do imóvel? Se quiser, separo outras opções parecidas.',
              why='Mantém o lead quente depois da visita.')],
    },
    'escola': {
        'label': 'Escola, idiomas ou cursos presenciais', 'short': 'Escola e cursos',
        'stages': [('Novo contato', 'open'), ('Aula experimental marcada', 'open'), ('Fez a aula', 'open'), ('Matrícula em andamento', 'open'), ('Matriculou', 'won'), ('Perdido', 'lost')],
        'service': ('Aula experimental', 60, 5),
        'ai': {'about': 'Escola de idiomas com turmas presenciais e online.', 'offer': 'Inglês: R$ 390/mês.\nMaterial incluso.\nAula experimental grátis.',
               'goal': 'Agendar a aula experimental ou o teste de nível.', 'qualify': 'Qual idioma?\nPara quem é o curso?\nNível atual?\nPeríodo preferido?',
               'rules': 'Não prometer turma sem conferir vagas.', 'faq': ''},
        'recipes': AGENDA_RECIPES + FOLLOWUP,
    },
    'loja': {
        'label': 'Loja ou e-commerce', 'short': 'Loja',
        'stages': [('Novo contato', 'open'), ('Em atendimento', 'open'), ('Orçamento enviado', 'open'), ('Comprou', 'won'), ('Perdido', 'lost')],
        'service': ('Atendimento na loja', 30, 1),
        'ai': {'about': 'Loja de roupas femininas com loja física e envio para todo o Brasil.', 'offer': 'Peças de R$ 89 a R$ 399. Frete grátis acima de R$ 299.',
               'goal': 'Ajudar a escolher e levar até a compra.', 'qualify': 'Qual peça procura?\nTamanho?\nPara quando precisa?',
               'rules': 'Não confirmar estoque sem conferir: chamar o time.', 'faq': 'Troca em até 30 dias.\nEnvio em até 2 dias úteis.'},
        'recipes': FOLLOWUP + [
            R('orcamento', 'Orçamento sem resposta', 'no_reply', 24, 'Oi, {primeiro_nome}! Separei as peças que você pediu. Quer que eu reserve para você?',
              stage='orçamento', why='Recupera quem pediu preço e não fechou.'),
            R('recompra', 'Recompra em 45 dias', 'no_reply', 1080, 'Oi, {primeiro_nome}! Chegaram novidades que combinam com o que você levou. Quer ver?',
              stage='comprou', why='Faz o cliente voltar a comprar.')],
    },
    'servicos': {
        'label': 'Serviços profissionais (contabilidade, advocacia, consultoria)', 'short': 'Serviços profissionais',
        'stages': [('Novo contato', 'open'), ('Reunião marcada', 'open'), ('Proposta enviada', 'open'), ('Fechou', 'won'), ('Perdido', 'lost')],
        'service': ('Reunião inicial', 45, 1),
        'ai': {'about': 'Escritório de contabilidade para pequenas empresas e MEI.', 'offer': 'Abertura de empresa a partir de R$ 690.\nMensalidade a partir de R$ 390.',
               'goal': 'Entender a necessidade e marcar a reunião.', 'qualify': 'Já tem empresa aberta?\nQual o ramo?\nQuantos funcionários?',
               'rules': 'Não dar parecer técnico ou jurídico pelo WhatsApp.', 'faq': ''},
        'recipes': AGENDA_RECIPES[:2] + FOLLOWUP + [
            R('proposta', 'Proposta sem resposta', 'no_reply', 48, 'Oi, {primeiro_nome}! Conseguiu analisar a proposta? Posso esclarecer algum ponto?',
              stage='proposta', why='Proposta parada costuma ser dúvida não falada.')],
    },
    'restaurante': {
        'label': 'Restaurante, delivery ou confeitaria', 'short': 'Restaurante e delivery',
        'stages': [('Novo contato', 'open'), ('Pedido em andamento', 'open'), ('Pedido entregue', 'won'), ('Desistiu', 'lost')],
        'service': ('Reserva de mesa', 120, 10),
        'ai': {'about': 'Restaurante italiano com salão e delivery.', 'offer': 'Cardápio: (cole aqui ou envie o link).\nTaxa de entrega a partir de R$ 6.',
               'goal': 'Tirar dúvidas do cardápio, fazer reservas e encaminhar pedidos.', 'qualify': 'Delivery ou reserva?\nPara quantas pessoas?',
               'rules': 'Pedidos são confirmados pelo time.', 'faq': 'Horário: ter a dom, 18h às 23h.'},
        'recipes': AGENDA_RECIPES[:1] + [
            R('pos_pedido', 'Como estava o pedido', 'stage_enter', 2, 'Oi, {primeiro_nome}! Como estava seu pedido? Sua opinião ajuda muito a gente.',
              stage='entregue', why='Coleta opinião e abre espaço para o próximo pedido.'),
            R('volta_15', 'Saudade em 15 dias', 'no_reply', 360, 'Oi, {primeiro_nome}! Bateu vontade? Hoje tem novidade no cardápio.',
              stage='entregue', why='Traz de volta quem já pediu.')],
    },
    'lancamento': {
        'label': 'Infoproduto, curso online ou mentoria', 'short': 'Infoproduto',
        'stages': [('Novo lead', 'open'), ('Interessado', 'open'), ('Link enviado', 'open'), ('Comprou', 'won'), ('Perdido', 'lost')],
        'service': ('Conversa de apresentação', 30, 1),
        'ai': {'about': 'Curso online de X, com aulas gravadas e encontros ao vivo.', 'offer': 'R$ 997 à vista ou 12x de R$ 97. Garantia de 7 dias.',
               'goal': 'Tirar dúvidas e levar até o link de compra.', 'qualify': 'Qual o momento atual do cliente?\nO que quer resolver?',
               'rules': 'Não prometer resultado financeiro.\nNão dar desconto fora das condições.', 'faq': 'Acesso por 1 ano.'},
        'recipes': FOLLOWUP + [
            R('link_enviado', 'Link enviado e não comprou', 'no_reply', 24, 'Oi, {primeiro_nome}! Conseguiu acessar o link? Se tiver qualquer dúvida antes de entrar, me fala.',
              stage='link', why='Recupera quem chegou no checkout e parou.')],
    },
    'outro': {
        'label': 'Outro tipo de negócio', 'short': 'Outro',
        'stages': [('Novo contato', 'open'), ('Em conversa', 'open'), ('Proposta enviada', 'open'), ('Fechou', 'won'), ('Perdido', 'lost')],
        'service': ('Atendimento', 60, 1),
        'ai': {'about': 'O que é o negócio, onde fica e para quem atende.', 'offer': 'O que vende e quanto custa.',
               'goal': 'Tirar dúvidas e levar ao próximo passo (agendar, orçamento ou compra).', 'qualify': '', 'rules': '', 'faq': ''},
        'recipes': AGENDA_RECIPES + FOLLOWUP,
    },
}
ORDER = ['studio', 'clinica', 'salao', 'imobiliaria', 'escola', 'loja', 'servicos', 'restaurante', 'lancamento', 'outro']

def niche(segment): return NICHES.get(segment) or NICHES['outro']

def recipes_for(segment):
    return niche(segment)['recipes']

@router.get('/niches')
async def niches():
    """Lista pública dos segmentos (cadastro e site)."""
    return [{'key': k, 'label': NICHES[k]['label'], 'short': NICHES[k]['short']} for k in ORDER]
