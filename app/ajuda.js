/* Central de ajuda do Oi Nanda: artigos, guias por segmento e perguntas frequentes.
   Carregado só quando a pessoa abre a Ajuda (window.HELP). Blocos de cada artigo:
   ['p', texto] · ['h', subtítulo] · ['steps', [..]] · ['list', [..]] · ['tip', texto] · ['warn', texto] · ['go', rótulo, tela]
   Texto pode ter <b> e <i>. Nada de promessa que o sistema não cumpre: se mudar a tela, mude aqui. */
window.HELP = {
cats: [
 ['comecar', 'Começar', 'Do cadastro à primeira conversa atendida pela Nanda.', '🚀'],
 ['atender', 'Atender', 'Conversas, agenda e o dia a dia do time.', '💬'],
 ['vender', 'Vender mais', 'Funil, vendas, clientes, automações e campanhas.', '📈'],
 ['acompanhar', 'Acompanhar', 'Relatórios e de onde vem cada cliente.', '📊'],
 ['conta', 'Conta e plano', 'Assinatura, cobrança, avisos e dados.', '⚙️'],
],
arts: [
/* ---------------- Começar ---------------- */
{id: 'primeiros-passos', cat: 'comecar', title: 'Primeiros passos: do cadastro à primeira venda', sum: 'O caminho mais curto para a Nanda começar a atender no seu WhatsApp.', min: 3, to: 'inicio', body: [
 ['p', 'O Oi Nanda junta num lugar só as conversas do WhatsApp e do Instagram, o funil de vendas, a agenda e a Nanda, a assistente que responde seus clientes com o que você ensinou. A ordem abaixo é a que funciona melhor.'],
 ['steps', [
  '<b>Converse com a Nanda.</b> No Início, toque em "Conversar com a Nanda" e escreva como se fosse um cliente seu. Leva 1 minuto e você já vê como ela responde.',
  '<b>Ligue o WhatsApp e começam os 7 dias grátis.</b> Sem cartão. A gente avisa antes do fim, e a conta só segue ativa se você cadastrar o cartão.',
  '<b>Ensine o seu negócio.</b> Em Nanda (IA), preencha preços, horários, regras e perguntas frequentes. O botão "Preencher com exemplo" já traz um modelo do seu segmento para você só ajustar.',
  '<b>Conecte o WhatsApp da empresa.</b> Em Empresa › WhatsApp. Leva uns 5 minutos. Leia antes o guia de conexão sem risco de bloqueio.',
  '<b>Ligue as automações recomendadas.</b> Em Automações, o bloco "Comece por estas 3" já mostra as que mais trazem venda no seu segmento.',
  '<b>Chame o time.</b> Em Equipe, convide quem atende. Cada pessoa liga o aviso no celular para saber quando um cliente chega.']],
 ['tip', 'A Nanda começa no modo <b>Copiloto</b>: ela sugere e você envia. Quando os testes estiverem bons, mude para <b>Automática</b> e ela responde sozinha.'],
 ['go', 'Ver meus primeiros passos', 'inicio']]},

{id: 'conectar-whatsapp', cat: 'comecar', title: 'Conectar o WhatsApp sem risco de bloqueio', sum: 'O que fazer antes, durante e depois de conectar, para a Meta não restringir o número.', min: 4, to: 'empresa', body: [
 ['p', 'O Oi Nanda usa a API oficial do WhatsApp, da própria Meta. É o jeito certo de usar IA e automação sem correr o risco das ferramentas "piratas". Mesmo assim, a Meta olha com cuidado números que mudam de comportamento de repente. Seguindo os passos abaixo, a conexão sai tranquila.'],
 ['h', 'Antes de conectar'],
 ['list', [
  '<b>Use o número de sempre da empresa</b>, já com histórico de conversas. Chip novo precisa de uns 7 dias de uso normal (conversas de verdade, sem disparo) antes de conectar.',
  '<b>Tenha o celular com o chip na mão.</b> O código de confirmação chega por SMS ou ligação nesse número.',
  '<b>Faça a conexão por um aparelho e uma internet que você já usa no dia a dia</b>, de preferência o próprio celular do chip. Entrar de vários aparelhos diferentes no mesmo dia chama atenção da Meta.',
  '<b>Tenha um portfólio empresarial na Meta</b> (business.facebook.com) com o nome real da empresa. Se não tiver, a tela da Meta cria um no caminho.']],
 ['h', 'Durante'],
 ['steps', [
  'Em Empresa › WhatsApp, marque os 4 itens da lista de conferência. O botão só libera depois disso, de propósito.',
  'Na tela da Meta, entre com o Facebook do responsável pela empresa e escolha o portfólio certo.',
  'Confirme o número com o código que chegar no celular.',
  'Pronto: volte ao Oi Nanda e mande uma mensagem de teste de outro celular.']],
 ['h', 'Depois de conectar'],
 ['list', [
  '<b>Cadastre um cartão na conta do WhatsApp na Meta</b> (Configurações do negócio › Pagamentos). Sem cartão, a Meta pausa as mensagens que a empresa inicia, como lembretes e campanhas. Responder quem chamou continua grátis.',
  '<b>Não importe uma lista fria para disparar.</b> Mensagem para quem nunca falou com você é o motivo número 1 de bloqueio. Use campanha só para quem já é seu cliente ou conversou com você.',
  '<b>Verifique a empresa na Meta</b> quando ela pedir. Isso aumenta o limite diário de pessoas novas.']],
 ['warn', 'Se aparecer um aviso vermelho de "WhatsApp com problema na Meta" no Oi Nanda, abra Empresa › WhatsApp: a tela explica o que a Meta pediu e como resolver.'],
 ['go', 'Conectar o WhatsApp', 'empresa']]},

{id: 'ensinar-nanda', cat: 'comecar', title: 'Ensinar a Nanda a atender como você', sum: 'As 5 partes que ela precisa saber e como testar antes de deixar ela sozinha.', min: 4, to: 'nanda', body: [
 ['p', 'A Nanda só responde com o que você ensinou. O que ela não souber, ela não inventa: passa para o seu time. Quanto mais completo, menos ela precisa chamar alguém.'],
 ['h', 'As 5 partes'],
 ['list', [
  '<b>Sobre o negócio:</b> o que é, onde fica, para quem atende, horário de funcionamento.',
  '<b>Condições e preços:</b> valores, pacotes, formas de pagamento, parcelamento. Se não quer passar preço pelo WhatsApp, escreva isso em Regras.',
  '<b>Perguntas frequentes:</b> estacionamento, o que levar, prazo de entrega, política de troca.',
  '<b>O que perguntar ao cliente:</b> as informações que você precisa para atender bem (ex.: qual procedimento, qual período prefere).',
  '<b>Regras e quando chamar o time:</b> o que ela nunca faz (dar desconto, orientação médica) e quando passa a conversa para uma pessoa.']],
 ['tip', 'Sem tempo? Toque em "Me ajude a escrever" em cada campo, ou em "Preencher o que falta com exemplo", e só troque o que for diferente.'],
 ['h', 'Teste antes de soltar'],
 ['steps', [
  'Na coluna "Testar conversa", escreva como um cliente escreveria, com erro de digitação e tudo.',
  'Se a resposta não ficou boa, ajuste o campo correspondente e teste de novo.',
  'Salve os testes importantes em "Testes salvos". Sempre que mudar algo, rode de novo e veja se nada piorou.',
  'Quando ela perguntar algo que não sabia, aparece em "Ela não soube responder". Responda ali e a resposta vira conhecimento dela.']],
 ['go', 'Abrir a Nanda', 'nanda']]},

{id: 'modos-nanda', cat: 'comecar', title: 'Automática, Copiloto ou Pausada: como a Nanda trabalha', sum: 'Escolha quanto a Nanda faz sozinha, para todos ou para uma conversa.', min: 2, to: 'nanda', body: [
 ['list', [
  '<b>Automática:</b> responde sozinha, na hora, 24 horas. Marca horário na agenda quando o cliente escolhe um horário livre. Chama o time quando não sabe ou quando o cliente pede uma pessoa.',
  '<b>Copiloto:</b> escreve a sugestão de resposta e alguém do time revisa e envia. Bom para os primeiros dias.',
  '<b>Pausada:</b> não responde nem sugere. Só o time atende.']],
 ['p', 'O modo padrão vale para todas as conversas novas e fica no topo da tela Nanda (IA). Dentro de uma conversa, dá para mudar só para aquele cliente.'],
 ['p', 'Quando o cliente pede para falar com uma pessoa, a Nanda avisa que vai chamar alguém e para de responder naquela conversa. A conversa vai para "Precisa de você". Depois que o time resolver, dá para devolver para a Nanda com um toque.'],
 ['tip', 'No plano Essencial, a Nanda trabalha como Copiloto (sugere as respostas). Para ela responder sozinha, é a partir do Profissional.'],
 ['go', 'Escolher o modo', 'nanda']]},

{id: 'instagram', cat: 'comecar', title: 'Conectar o Instagram Direct', sum: 'Responda o Direct no mesmo lugar do WhatsApp, com a Nanda ajudando.', min: 2, to: 'empresa', body: [
 ['steps', [
  'No app do Instagram, confira se a conta é <b>profissional</b> (comercial ou criador) e se está ligada a uma Página do Facebook.',
  'Em Empresa › Instagram, toque em Conectar e entre com o Facebook que administra essa Página.',
  'Marque a conta do Instagram e a Página quando a Meta perguntar, e confirme.',
  'Mande uma mensagem de teste no Direct, de outra conta.']],
 ['p', 'Quem chama no Instagram e depois passa o telefone vira uma ficha só: dá para continuar a conversa pelo WhatsApp, com todo o histórico junto.'],
 ['tip', 'Se der "código inválido" no login, tente numa janela anônima do navegador. Isso acontece quando há mais de uma conta do Instagram aberta no mesmo navegador.'],
 ['go', 'Conectar o Instagram', 'empresa']]},

{id: 'equipe', cat: 'comecar', title: 'Convidar o time e definir quem vê o quê', sum: 'Dono, Gestor e Atendente: o que cada papel pode fazer.', min: 3, to: 'equipe', body: [
 ['list', [
  '<b>Dono:</b> vê e mexe em tudo, inclusive assinatura e cobrança.',
  '<b>Gestor:</b> vê tudo do atendimento e das vendas, mexe na Nanda, automações e relatórios. Não mexe na assinatura.',
  '<b>Atendente:</b> atende as conversas dele e as que estiverem sem dono, registra vendas e vê só as próprias vendas.']],
 ['steps', [
  'Em Equipe, toque em Convidar, coloque nome, e-mail e papel.',
  'A pessoa recebe um e-mail para criar a senha (vale por 3 dias).',
  'No primeiro acesso, ela liga o aviso no celular em "Seu status e avisos" (canto de baixo do menu).']],
 ['h', 'Equipes, fila e rodízio (plano Equipe)'],
 ['p', 'Dá para separar o time em equipes (ex.: Pré-vendas, Vendas, Suporte). Cada equipe tem fila própria. No rodízio, as conversas novas são distribuídas entre quem está disponível e abaixo do limite de conversas abertas. Se o cliente esperar mais que o tempo combinado, a gerência recebe um alerta.'],
 ['h', 'Turnos e comissão'],
 ['p', 'Em Equipe › <b>Comissão e turno</b>, defina para cada pessoa os dias e o horário em que ela trabalha (ex.: SDR da manhã, seg a sex, 8h às 14h). Fora do turno, o rodízio não entrega conversa para ela. No mesmo lugar fica o percentual de comissão, que aparece calculado em Vendas › Comissões.'],
 ['go', 'Abrir Equipe', 'equipe']]},

{id: 'avisos-celular', cat: 'comecar', title: 'Receber aviso de cliente no celular', sum: 'Notificação de mensagem nova sem instalar nada da loja de apps.', min: 2, to: 'inicio', body: [
 ['steps', [
  'Abra o Oi Nanda no celular, pelo navegador.',
  'Toque no seu nome, no rodapé do menu, e ligue "Avisos neste aparelho".',
  'No iPhone: antes, toque em Compartilhar › "Adicionar à Tela de Início" e abra o Oi Nanda pelo ícone. É regra da Apple (iOS 16.4 ou mais novo).']],
 ['p', 'O aviso chega quando um cliente escreve para você ou para a sua fila, quando alguém transfere uma conversa para você e quando a Nanda chama o time.'],
 ['tip', 'Vai ficar longe? Marque-se como "ausente" no mesmo lugar: as conversas novas vão para quem está disponível.']]},

/* ---------------- Atender ---------------- */
{id: 'conversas', cat: 'atender', title: 'Como usar a tela de Conversas', sum: 'As três abas, como assumir, transferir e devolver para a Nanda.', min: 3, to: 'conversas', body: [
 ['list', [
  '<b>Precisa de você:</b> quem pediu uma pessoa, quem está esperando resposta do time e transferências para você. Comece sempre por aqui.',
  '<b>Com a Nanda:</b> conversas que ela está conduzindo. Dá para acompanhar e entrar a qualquer momento.',
  '<b>Todas:</b> o histórico completo, com filtros por canal, origem, etiqueta e tipo de cliente.']],
 ['h', 'Dentro da conversa'],
 ['list', [
  '<b>Responder:</b> escreva e envie. Se a Nanda estiver em Copiloto, a sugestão já aparece pronta para revisar.',
  '<b>Nota interna:</b> anotação que só o time vê (ex.: "prefere ser chamada à tarde").',
  '<b>Transferir:</b> passa para outra pessoa ou equipe, com o motivo.',
  '<b>Mandar depois:</b> "me chama na segunda" vira uma mensagem programada que sai sozinha no dia e hora combinados (dentro da janela de 24 h).',
  '<b>Devolver para a Nanda:</b> depois de resolver, ela volta a atender aquele cliente.',
  '<b>Botões e listas:</b> quando a mensagem foi com botões (lembrete) ou lista de horários, as opções aparecem embaixo do balão. O que o cliente tocou chega como resposta normal.']],
 ['tip', 'A ficha do cliente (ao lado da conversa) mostra etapa do funil, compras, horários marcados e a jornada completa.'],
 ['go', 'Abrir Conversas', 'conversas']]},

{id: 'janela-24h', cat: 'atender', title: 'A regra das 24 horas e o custo da Meta', sum: 'Quando a mensagem é grátis, quando precisa de modelo e quem cobra.', min: 2, to: 'automacoes', body: [
 ['p', '<b>Responder quem te chamou é grátis, sempre.</b> Por 24 horas depois da última mensagem do cliente, você (e a Nanda) escreve livremente.'],
 ['p', 'Depois das 24 horas, a Meta só deixa a empresa iniciar a conversa com um <b>modelo aprovado</b> (lembrete, promoção, retomada). O Oi Nanda pede a aprovação por você, em um clique, na aba Modelos da Meta.'],
 ['p', 'Essas mensagens iniciadas pela empresa têm um valor cobrado <b>direto pela Meta</b>, no cartão cadastrado na conta do WhatsApp. O Oi Nanda mostra a estimativa antes de você ligar uma automação ou enviar uma campanha.'],
 ['tip', 'Por isso as automações de retomada mais baratas são as que saem ainda dentro das 24 h (ex.: "Sem resposta no mesmo dia"): vão como mensagem normal, sem custo da Meta.']]},

{id: 'agenda', cat: 'atender', title: 'Agenda: serviços, horários e lembretes', sum: 'Configure uma vez e a Nanda marca só em horário livre de verdade.', min: 3, to: 'agenda', body: [
 ['steps', [
  'Em Agenda › Configurar, cadastre os serviços com duração e quantas pessoas cabem no mesmo horário (ex.: aula experimental para 3).',
  'Defina os dias e horários de atendimento e as pausas.',
  'Se usa Google Agenda, conecte: o que estiver ocupado lá bloqueia aqui, e o que for marcado aqui aparece lá.']],
 ['p', 'No WhatsApp, a Nanda manda uma <b>lista com os próximos horários livres</b>: o cliente toca no que prefere e o horário já fica marcado, sem digitar nada. Em outros canais ela oferece até 3 opções por escrito.'],
 ['h', 'Lembrete com botões'],
 ['p', 'O lembrete automático chega com dois botões: <b>Confirmo</b> (o horário vira Confirmado na hora) e <b>Preciso remarcar</b> (chega a lista de horários livres; quando o cliente escolhe, o horário antigo é liberado sozinho). Quem responde "sim" ou 👍 também confirma.'],
 ['tip', 'Quando o lembrete sai depois das 24 h da última mensagem do cliente, ele vai por modelo aprovado da Meta. Ao pedir a aprovação do modelo em Automações, os dois botões já vão junto.'],
 ['h', 'Status do horário'],
 ['list', ['<b>Agendado</b> → <b>Confirmado</b> → <b>Compareceu</b> ou <b>Faltou</b>. Mude o status tocando no horário. Marcar "Faltou" dispara a automação de oferecer novo horário, se estiver ligada.']],
 ['tip', 'Ligue os lembretes "1 dia antes" e "no dia" em Automações. São os que mais reduzem falta, ainda mais com os botões.'],
 ['go', 'Abrir Agenda', 'agenda']]},

/* ---------------- Vender ---------------- */
{id: 'funil', cat: 'vender', title: 'Funil de vendas: etapas e cards', sum: 'Cada cliente é um card. Ver onde cada um está é ver onde a venda trava.', min: 3, to: 'funil', body: [
 ['p', 'O funil já vem com as etapas do seu segmento (dá para renomear, criar e reordenar no botão Etapas, no topo do Funil). Todo contato novo entra na primeira etapa.'],
 ['list', [
  '<b>Arraste o card</b> para a etapa seguinte conforme a conversa anda. A Nanda também sugere a etapa quando percebe que mudou.',
  '<b>Etapa de venda</b> (ex.: Fechou, Matriculou, Comprou): ao mover para ela com valor, a venda é registrada na hora e a pessoa passa a contar como cliente.',
  '<b>Perdido:</b> guarde o motivo. Em Relatórios você vê onde mais perde.']],
 ['tip', 'Use o filtro "Parados" para achar negociação esquecida. É dinheiro quase fechado.'],
 ['h', 'Funil de pós-venda (plano Equipe)'],
 ['p', 'Um segundo funil só para renovação e próxima compra: quem vai vencer, quem já foi contatado, quem renovou.'],
 ['go', 'Abrir o Funil', 'funil']]},

{id: 'vendas', cat: 'vender', title: 'Registrar vendas e acompanhar o faturamento', sum: 'Venda registrada é o que alimenta relatórios, metas, recompra e renovação.', min: 3, to: 'vendas', body: [
 ['steps', [
  'Cadastre seus produtos e serviços em Vendas › Catálogo. Se o item tem validade (plano mensal, pacote de sessões), informe os dias: é isso que liga os avisos de renovação.',
  'Na conversa ou em Vendas, toque em "Registrar venda", escolha o cliente, os itens e a forma de pagamento.',
  'Se mais de uma pessoa vendeu, divida a venda em percentuais. Cada uma soma a sua parte nas metas.']],
 ['list', [
  '<b>Desconto acima do limite</b> dado por atendente fica esperando a aprovação do gestor.',
  '<b>Exportar:</b> a lista de vendas sai em Excel, com os filtros que estiverem aplicados.',
  '<b>Cancelar uma venda</b> tira ela dos números, mas o registro fica no histórico.']],
 ['h', 'Comissão'],
 ['p', 'Com o percentual de cada pessoa definido em Equipe, a aba <b>Vendas › Comissões</b> mostra, no mês, quanto cada um vendeu (a parte dele nas vendas divididas) e quanto tem de comissão. O atendente vê só a dele.'],
 ['tip', 'O Oi Nanda não cobra o cliente. Ele registra a venda que você fez (Pix, cartão, link de pagamento) para você enxergar tudo num lugar só. Quem vende pela Hotmart, Kiwify ou Eduzz pode ligar a integração e as vendas entram sozinhas.'],
 ['go', 'Abrir Vendas', 'vendas']]},

{id: 'clientes', cat: 'vender', title: 'Clientes e públicos: para quem vender de novo', sum: 'Prospects, clientes e inativos, e como montar um público em segundos.', min: 3, to: 'clientes', body: [
 ['list', [
  '<b>Prospect:</b> conversou, ainda não comprou.',
  '<b>Cliente:</b> já comprou pelo menos uma vez.',
  '<b>Inativo:</b> comprou, mas está há muito tempo sem comprar (você define quantos dias).']],
 ['p', 'Toque num número do topo (ex.: "Vencem em 30 dias") e ele vira um público. Ou monte o seu com condições: comprou tal produto, veio de tal anúncio, não compra há 90 dias. Salve para usar de novo.'],
 ['h', 'Trazer clientes de uma planilha'],
 ['steps', [
  'Em Clientes, toque em Importar planilha (Excel ou CSV).',
  'Confira o que é cada coluna. O telefone é a chave: o mesmo número vira um contato só.',
  'Veja o resumo (novos, já existentes, linhas com problema) e confirme. Dá para desfazer depois.']],
 ['warn', 'Prospect importado de planilha que nunca falou com você fica fora das campanhas, a não ser que tenha autorizado receber mensagens. Isso protege o seu número de bloqueio.'],
 ['go', 'Abrir Clientes', 'clientes']]},

{id: 'automacoes', cat: 'vender', title: 'Automações: mensagens que saem sozinhas', sum: 'Lembrete, retomada, pós-venda e renovação, sem ninguém precisar lembrar.', min: 3, to: 'automacoes', body: [
 ['p', 'Uma automação é: <b>quando</b> (um momento da conversa ou da compra) + <b>qual mensagem</b> + <b>para quem</b>. Ela sai pelo WhatsApp, com o nome do cliente, e você vê quantos responderam e quantos compraram.'],
 ['list', [
  '<b>Só sai das 8h às 21h</b>, no fuso da empresa. Fora disso, espera o próximo horário.',
  '<b>Dentro das 24 h</b> da última mensagem do cliente, vai como mensagem normal. Depois, precisa de modelo aprovado pela Meta.',
  '<b>Quem escreve "parar" ou "sair"</b> não recebe mais nada automático.',
  '<b>Se o cliente responder</b>, a conversa volta para o fluxo normal (Nanda ou time).']],
 ['steps', [
  'Comece pelas 3 recomendadas para o seu segmento ("Ligar estas 3").',
  'Veja a prévia de como o cliente recebe e ajuste o texto se quiser.',
  'Depois de uma semana, abra o Histórico e veja quais trouxeram resposta e venda.']],
 ['go', 'Abrir Automações', 'automacoes']]},

{id: 'plataformas-de-venda', cat: 'vender', title: 'Plataformas de venda e cobrança: como ligar e o que cada uma traz', sum: 'Ligue a sua plataforma e recupere boleto, Pix, cobrança vencida e carrinho abandonado pelo WhatsApp.', min: 4, to: 'empresa', body: [
 ['p', 'Se você vende por uma dessas plataformas, o Oi Nanda passa a saber o que acontece no checkout de cada pessoa, e a Nanda e as automações agem em cima disso.'],
 ['h', 'Como ligar (uns 5 minutos)'],
 ['steps', [
  'Em Empresa › Plataformas de venda, toque em Ligar na sua plataforma e copie o endereço que aparece.',
  'Na plataforma, cadastre esse endereço como webhook e marque os eventos de compra (aprovada, boleto/Pix gerado, carrinho abandonado, reembolso e chargeback). A tela do Oi Nanda mostra o caminho de cada uma.',
  'Copie o código de segurança da plataforma (Hottok na Hotmart, token na Kiwify, chave secreta na Eduzz, secret na Cakto, token de autenticação no Asaas, assinatura secreta no Mercado Pago, código de autorização na iugu) e cole no Oi Nanda. Sem ele, nada entra: é o que garante que o aviso veio mesmo da plataforma.',
  'Faça uma compra de teste ou use o botão de teste da plataforma. Quando chegar, aparece "recebendo".']],
 ['h', 'O que acontece com cada evento'],
 ['list', [
  '<b>Venda aprovada:</b> entra em Vendas, move o card para a etapa de venda e a pessoa passa a contar como cliente. Não duplica, mesmo que a plataforma mande duas vezes.',
  '<b>Boleto ou Pix gerado:</b> fica anotado na conversa. A automação "Boleto ou Pix sem pagar" manda uma mensagem com o link depois do tempo que você escolher (padrão: 6 h).',
  '<b>Carrinho abandonado:</b> a automação "Carrinho abandonado" chama a pessoa pelo nome, oferece ajuda e o link (padrão: 1 h depois).',
  '<b>Reembolso ou chargeback:</b> a venda é cancelada em Vendas e sai dos relatórios e da comissão.']],
 ['h', 'Quais plataformas'],
 ['list', ['<b>Infoprodutos:</b> Hotmart, Kiwify, Eduzz e Cakto.', '<b>Mensalidades e cobranças:</b> Asaas, Vindi e iugu.', '<b>Pagamentos e checkout:</b> Mercado Pago, Pagar.me, PagBank e Stripe.']],
 ['p', 'PayPal e InfinitePay ainda não: os avisos delas não trazem o telefone de quem pagou, então a Nanda não teria como chamar o cliente.'],
 ['h', 'Asaas, Mercado Pago, iugu, Vindi e Pagar.me'],
 ['p', 'Nessas, o aviso traz só o número da cobrança. Por isso, além do código de segurança, o Oi Nanda pede a <b>chave da API</b> (Access Token no Mercado Pago): com ela lê quem pagou, o valor e o link. Crie uma chave só para o Oi Nanda, que você pode apagar quando quiser.'],
 ['list', ['<b>Pago:</b> vira venda e move o card.', '<b>Venceu sem pagar ou cartão recusado:</b> entra em "Boleto ou Pix sem pagar", com o link da cobrança.', '<b>Mensalidade gerada antes do vencimento</b> não entra na recuperação: o Oi Nanda espera vencer.', 'Essas plataformas não têm carrinho abandonado.']],
 ['p', 'Quem pagou sai da recuperação sozinho. A pessoa é encontrada pelo celular (ou pelo e-mail) que preencheu no checkout; se ainda não existia, a ficha é criada.'],
 ['warn', 'Quem nunca conversou com você no WhatsApp só recebe a recuperação por modelo aprovado pela Meta. Em Automações, peça a aprovação do modelo da automação antes do lançamento.'],
 ['tip', 'A Nanda sabe o que a pessoa fez no checkout e de onde veio (link, anúncio, aula). Na primeira resposta ela cita isso com naturalidade.'],
 ['go', 'Ligar a minha plataforma', 'empresa']]},

{id: 'campanhas', cat: 'vender', title: 'Campanhas no WhatsApp sem queimar o número', sum: 'Mandar para um público com os freios que protegem a sua conta.', min: 3, to: 'clientes', body: [
 ['p', 'Campanha é uma mensagem para um público (ex.: clientes inativos há 90 dias). Como sai fora da janela de 24 h, usa um modelo aprovado pela Meta e tem custo da Meta por pessoa, mostrado antes de enviar.'],
 ['h', 'Os freios que já vêm ligados'],
 ['list', [
  'Envia aos poucos e respeita o limite diário de pessoas novas da Meta. Continua sozinha no dia seguinte.',
  'A mesma pessoa recebe no máximo uma promoção a cada 7 dias.',
  'Nunca envia para quem pediu para sair.',
  'Pausa sozinha se muita gente pedir para sair, se muitas mensagens falharem ou se a nota do número cair, e avisa você com o que fazer.']],
 ['p', 'Dá para gerar um cupom individual para cada pessoa ({cupom}). No resultado: entregues, responderam, compraram e quanto faturou.'],
 ['go', 'Criar campanha', 'clientes']]},

{id: 'promocoes', cat: 'vender', title: 'Promoções e cupons', sum: 'A Nanda só oferece o que está valendo e confere o cupom que o cliente digita.', min: 2, to: 'vendas', body: [
 ['p', 'Em Vendas › Promoções, crie uma regra com período (e, se quiser, dias da semana e horário), o que ela cobre e o tipo de desconto: percentual, valor fixo, preço especial ou leve X pague Y.'],
 ['list', [
  'Condições opcionais: primeira compra, valor mínimo, origem do cliente, só para inativos, limite de usos.',
  'Cupom geral (ex.: BLACK20) ou individual, um por cliente.',
  'O desconto entra certo no registro da venda e nos relatórios.']],
 ['go', 'Abrir Promoções', 'vendas']]},

{id: 'metas', cat: 'vender', title: 'Metas, prêmios e ranking (plano Equipe)', sum: 'Meta por pessoa ou equipe, com ritmo diário e prêmio ao bater.', min: 2, to: 'metas', body: [
 ['list', [
  '<b>Metas</b> para a empresa, uma equipe ou uma pessoa, com degraus (mínimo, meta, supermeta) e o ritmo: "faltam R$ X por dia".',
  '<b>Prêmios</b> ficam conquistados ao bater a meta. O gestor marca quando entregar.',
  '<b>Desafios</b> com prazo, como "quem vender mais planos anuais em novembro".',
  '<b>Ranking</b> com privacidade: todos, só o pódio ou só a própria posição.']],
 ['tip', 'O Modo TV mostra ranking e metas em tela cheia, bom para deixar na recepção ou na sala do time.'],
 ['go', 'Abrir Metas', 'metas']]},

/* ---------------- Acompanhar ---------------- */
{id: 'relatorios', cat: 'acompanhar', title: 'Entender os Relatórios', sum: 'O que cada número significa e o que fazer com ele.', min: 3, to: 'relatorios', body: [
 ['list', [
  '<b>Vendas:</b> quanto entrou no período e quantos clientes compraram. Quem comprou duas vezes conta como 1 cliente. A contagem venda por venda fica na página Vendas.',
  '<b>Conversão:</b> de cada 100 contatos novos do período, quantos já compraram.',
  '<b>Primeira resposta:</b> quanto tempo o cliente esperou pela primeira resposta. Até 5 minutos é ótimo.',
  '<b>Ticket médio:</b> quanto cada cliente que comprou gastou, em média.',
  '<b>Funil dos contatos novos:</b> quantos chegaram em cada etapa. A etapa com a maior queda é onde focar.']],
 ['p', 'O bloco "O que fazer agora" no topo lista o que dá para resolver hoje: quem está esperando, negociação parada, pedido de atendente.'],
 ['tip', 'Compare sempre com o período anterior (as setas). Um número sozinho diz pouco.'],
 ['go', 'Abrir Relatórios', 'relatorios']]},

{id: 'origens', cat: 'acompanhar', title: 'Saber de onde vem cada cliente', sum: 'Links rastreáveis, QR Code e anúncios que chamam no WhatsApp.', min: 2, to: 'vendas', body: [
 ['p', 'Em Vendas › Origens, crie um link curto (ex.: oinanda.com.br/l/abc12) para cada lugar onde você divulga: bio do Instagram, panfleto, Google. Quem chama por ele já chega marcado com a origem.'],
 ['p', 'Quem chama clicando num anúncio da Meta (clique para o WhatsApp) é reconhecido sozinho, com o nome do anúncio.'],
 ['p', 'No relatório você vê, por origem, quantos contatos vieram, quantos compraram e quanto faturou. É a resposta para "esse anúncio está valendo a pena?".'],
 ['go', 'Abrir Origens', 'vendas']]},

/* ---------------- Conta ---------------- */
{id: 'assinatura', cat: 'conta', title: 'Planos, teste grátis e cobrança', sum: 'Como funcionam os 7 dias grátis, os planos e o pacote extra da Nanda.', min: 3, to: 'assinatura', body: [
 ['list', [
  '<b>Teste grátis de 7 dias, sem cartão</b>, com tudo liberado. Os 7 dias começam quando você liga o WhatsApp. Se cadastrar o cartão no meio do teste, a primeira mensalidade só entra no dia em que o teste acaba; sem cartão, a conta fica só para leitura depois dos 7 dias.',
  '<b>Essencial (R$ 147/mês):</b> 2 pessoas, a Nanda sugerindo as respostas, conversas, agenda, funil, vendas e automações.',
  '<b>Profissional (R$ 297/mês):</b> 5 pessoas e 600 atendimentos da Nanda sozinha por mês, campanhas, públicos e retorno por anúncio.',
  '<b>Equipe (R$ 597/mês):</b> 12 pessoas, 2.000 atendimentos, equipes com rodízio, metas, ranking e pós-venda.',
  '<b>Pacote extra:</b> mais 500 atendimentos por R$ 59, avulso ou automático quando acabar.']],
 ['p', 'Um "atendimento" é um cliente atendido pela Nanda num dia, não importa quantas respostas ela mande para ele nesse dia. Na tela Assinatura você vê quanto já usou.'],
 ['p', 'Se a cobrança não passar, a conta segue funcionando por 7 dias enquanto você confere o cartão. A gente avisa por e-mail.'],
 ['go', 'Ver minha assinatura', 'assinatura']]},

{id: 'cancelar', cat: 'conta', title: 'Trocar de plano ou cancelar', sum: 'Sem multa e sem precisar falar com ninguém.', min: 1, to: 'assinatura', body: [
 ['list', [
  '<b>Trocar de plano:</b> em Empresa › Assinatura, escolha o novo plano. Vale na hora.',
  '<b>Cancelar:</b> no mesmo lugar, um clique. A conta segue ativa até o fim do período pago. Durante o teste, nada é cobrado.',
  '<b>Depois de cancelar:</b> a conta fica só para consulta. Conversas, contatos e vendas continuam guardados, e dá para reativar quando quiser.']],
 ['go', 'Abrir Assinatura', 'assinatura']]},

{id: 'avisos-conta', cat: 'conta', title: 'Os avisos que o Oi Nanda manda para você', sum: 'Quais chegam por e-mail ou WhatsApp e como escolher.', min: 1, to: 'avisos-conta', body: [
 ['p', 'O Oi Nanda avisa o responsável da conta sobre teste, cobrança, uso da Nanda e WhatsApp desconectado. Alguns são opcionais e você pode desligar em Empresa › Avisos do Oi Nanda. Os de cobrança e de problema na conta sempre chegam.'],
 ['tip', 'Ficou com dúvida em algum aviso? Responda o próprio e-mail: quem lê é uma pessoa do time.'],
 ['go', 'Escolher meus avisos', 'avisos-conta']]},

{id: 'dados', cat: 'conta', title: 'Seus dados, privacidade e LGPD', sum: 'Onde ficam, quem vê e como tirar um contato.', min: 2, to: 'empresa', body: [
 ['list', [
  'Cada empresa só enxerga os próprios dados. O token do WhatsApp fica guardado criptografado.',
  'Tudo o que o time faz fica registrado em Atividade, com quem fez e quando.',
  'Quem escreve "parar" ou "sair" para de receber mensagens automáticas e campanhas.',
  'Vendas e listas de clientes podem ser exportadas em Excel a qualquer momento.',
  'Para apagar os dados de um contato ou da conta inteira, fale com o suporte.']],
 ['p', 'Os detalhes estão na Política de Privacidade, em oinanda.com.br/privacidade.']]},
],

/* ---------------- Guias por segmento ---------------- */
segs: {
 studio: {t: 'Studio, academia ou box', goal: 'transformar a aula experimental em matrícula e manter o aluno renovando.',
  flow: ['Novo contato', 'Aula experimental marcada', 'Fez a aula', 'Negociando', 'Matriculou'],
  day: ['Abra "Precisa de você" de manhã e no fim da tarde: é onde estão os alunos prontos para fechar. A experimental é marcada pela lista de horários que a Nanda manda no WhatsApp.', 'Depois de cada experimental, marque "Compareceu" na agenda. Isso dispara o "Depois da aula experimental".', 'Cadastre os planos no catálogo com a validade (30, 90, 365 dias) para o aviso de renovação sair na hora certa.'],
  autos: ['Lembrete 1 dia antes', 'Depois da aula experimental', 'Renovação: 7 dias antes de vencer'],
  nanda: 'Ensine os horários das turmas, o valor de cada plano e a regra de não fazer avaliação de saúde pelo WhatsApp. Objetivo dela: marcar a experimental.',
  watch: 'Quantas experimentais viram matrícula (Relatórios › Funil) e quantos planos renovam (Clientes › Renovação).'},
 clinica: {t: 'Clínica de estética, odonto ou saúde', goal: 'encher a agenda de avaliações, diminuir falta e fechar o orçamento.',
  flow: ['Novo contato', 'Avaliação marcada', 'Avaliou', 'Orçamento enviado', 'Fechou'],
  day: ['O lembrete chega com os botões Confirmo e Preciso remarcar. No fim da tarde, quem não confirmou merece uma ligação.', 'Depois da avaliação, mova o card para "Orçamento enviado" com o valor. O lembrete de orçamento sem resposta cuida do resto.', 'Cadastre procedimentos com validade (ex.: toxina, 120 dias) para a paciente ser chamada para o retorno.'],
  autos: ['Lembrete 1 dia antes', 'Orçamento sem resposta', 'Faltou: oferecer novo horário'],
  nanda: 'Em Regras, deixe claro que ela não dá orientação médica e, se preferir, que não passa preço de injetáveis pelo WhatsApp: oferece a avaliação. Objetivo dela: agendar a avaliação.',
  watch: 'Taxa de falta (Agenda), quantos orçamentos fecham (Funil) e quantas pacientes voltam (Clientes › Renovação).'},
 salao: {t: 'Salão, barbearia ou beleza', goal: 'agenda cheia e cliente voltando no ciclo certo.',
  flow: ['Novo contato', 'Horário marcado', 'Atendido'],
  day: ['Deixe a agenda com os horários reais de cada profissional: a Nanda só oferece o que está livre.', 'Ao fim do dia, marque quem compareceu. Quem faltou recebe a oferta de novo horário.', 'Registre a venda (serviço e produto): é o que mostra quem são seus melhores clientes.'],
  autos: ['Lembrete no dia', 'Hora de voltar (30 dias)', 'Faltou: oferecer novo horário'],
  nanda: 'Ensine os serviços com preço e duração e a regra de não prometer profissional sem conferir a agenda. Objetivo dela: marcar o horário.',
  watch: 'Clientes que voltaram em 30 a 45 dias (Clientes) e horários vazios na semana (Agenda).'},
 imobiliaria: {t: 'Imobiliária ou corretor', goal: 'qualificar o lead rápido e levar para a visita.',
  flow: ['Novo lead', 'Qualificado', 'Visita marcada', 'Proposta', 'Negócio fechado'],
  day: ['Use as perguntas de qualificação da Nanda (compra ou aluguel, bairro, faixa de preço, financiamento). O card chega ao corretor já com isso anotado.', 'Distribua os leads por rodízio entre os corretores (plano Equipe).', 'Depois da visita, marque "Compareceu" para sair o "Depois da visita".'],
  autos: ['Sem resposta no mesmo dia', 'Lembrete 1 dia antes', 'Depois da visita'],
  nanda: 'Ensine as regiões, a faixa de valores e que ela não confirma disponibilidade de imóvel nem negocia valor: chama o corretor.',
  watch: 'Tempo da primeira resposta (lead de imóvel esfria em horas) e quantas visitas viram proposta.'},
 escola: {t: 'Escola, idiomas ou cursos presenciais', goal: 'aula experimental que vira matrícula.',
  flow: ['Novo contato', 'Aula experimental marcada', 'Fez a aula', 'Matrícula em andamento', 'Matriculou'],
  day: ['Configure a agenda com as vagas por turma (ex.: 5 por aula experimental).', 'Quem fez a aula e não matriculou em 2 dias merece uma mensagem do time.', 'Cadastre os cursos com validade (semestre, ano) para o aviso de rematrícula.'],
  autos: ['Lembrete 1 dia antes', 'Sem resposta no mesmo dia', 'Renovação: 7 dias antes de vencer'],
  nanda: 'Ensine idiomas, níveis, horários e valores. Peça para ela perguntar para quem é o curso e o nível atual. Regra: não prometer turma sem conferir vaga.',
  watch: 'Experimentais que viram matrícula e rematrículas no fim do período.'},
 loja: {t: 'Loja ou e-commerce', goal: 'responder rápido, ajudar a escolher e trazer o cliente de volta.',
  flow: ['Novo contato', 'Em atendimento', 'Orçamento enviado', 'Comprou'],
  day: ['Cadastre o catálogo com preços: a Nanda usa para responder.', 'Quando mandar fotos e preço, mova o card para "Orçamento enviado". O lembrete recupera quem sumiu.', 'Registre cada venda: é o que alimenta a recompra e as campanhas para clientes.'],
  autos: ['Sem resposta no mesmo dia', 'Orçamento sem resposta', 'Recompra em 45 dias'],
  nanda: 'Ensine frete, troca, prazo de entrega e formas de pagamento. Regra: não confirmar estoque sem conferir, chama o time.',
  watch: 'Primeira resposta, orçamentos que viram compra e clientes que compraram mais de uma vez.'},
 servicos: {t: 'Serviços profissionais', goal: 'marcar a reunião inicial e fechar a proposta.',
  flow: ['Novo contato', 'Reunião marcada', 'Proposta enviada', 'Fechou'],
  day: ['A Nanda faz as perguntas iniciais (ramo, porte, necessidade) e marca a reunião.', 'Depois de enviar a proposta, mova o card com o valor. O lembrete de proposta cuida do acompanhamento.', 'Use notas internas para registrar o que foi combinado na reunião.'],
  autos: ['Lembrete 1 dia antes', 'Proposta sem resposta', 'Sem resposta há 3 dias'],
  nanda: 'Ensine os serviços, como funciona a primeira conversa e a regra de não dar parecer técnico ou jurídico pelo WhatsApp.',
  watch: 'Reuniões que viram proposta e propostas que fecham (Funil).'},
 restaurante: {t: 'Restaurante, delivery ou confeitaria', goal: 'atender pedido e reserva rápido e trazer o cliente de volta.',
  flow: ['Novo contato', 'Pedido em andamento', 'Pedido entregue'],
  day: ['Cole o cardápio (ou o link) no que a Nanda sabe. Ela tira dúvida e encaminha o pedido para o time confirmar.', 'Mova o card para "Pedido entregue" ao entregar: sai o "Como estava o pedido".', 'Use reservas na agenda com a capacidade por horário.'],
  autos: ['Como estava o pedido', 'Saudade em 15 dias', 'Lembrete 1 dia antes'],
  nanda: 'Ensine horários, taxa de entrega e cardápio. Regra: pedidos são confirmados pelo time.',
  watch: 'Clientes que pedem de novo em 30 dias e o horário com mais mensagens.'},
 lancamento: {t: 'Infoproduto, curso online ou mentoria', goal: 'responder todo lead na hora e levar até o link de compra.',
  flow: ['Novo lead', 'Interessado', 'Link enviado', 'Comprou'],
  day: ['Ligue a Hotmart, Kiwify ou Eduzz em Empresa › Plataformas de venda: venda aprovada entra sozinha e boleto, Pix e carrinho abandonado viram recuperação automática.', 'Em lançamento, deixe a Nanda em Automática: ela responde todos ao mesmo tempo e passa para o time quem está pronto ou travou.', 'Com SDRs em turnos, defina o turno de cada um em Equipe: o rodízio só entrega conversa para quem está trabalhando.'],
  autos: ['Boleto ou Pix sem pagar', 'Carrinho abandonado', 'Link enviado e não comprou'],
  nanda: 'Ensine o que o produto entrega, preço, parcelamento e garantia. Regras: não prometer resultado financeiro e não dar desconto fora das condições.',
  watch: 'Tempo da primeira resposta na abertura do carrinho, boletos e carrinhos recuperados (Empresa › Plataformas de venda) e comissão por vendedor (Vendas › Comissões).'},
 outro: {t: 'Outro tipo de negócio', goal: 'tirar dúvidas e levar ao próximo passo: agendar, orçamento ou compra.',
  flow: ['Novo contato', 'Em conversa', 'Proposta enviada', 'Fechou'],
  day: ['Renomeie as etapas do funil com os passos reais da sua venda.', 'Ensine a Nanda com o exemplo e ajuste para o seu caso.', 'Registre as vendas para os relatórios fazerem sentido.'],
  autos: ['Sem resposta no mesmo dia', 'Sem resposta há 3 dias', 'Lembrete 1 dia antes'],
  nanda: 'Comece pelo básico: o que vende, quanto custa, horário e quando ela deve chamar o time.',
  watch: 'Primeira resposta e conversão dos contatos novos (Relatórios).'},
},

faq: [
 ['Vou continuar usando o WhatsApp no celular?', 'Sim. A Nanda entra junto com o app que você já usa (WhatsApp Business). Você responde pelo celular quando quiser e tudo fica no mesmo histórico.'],
 ['A Nanda pode inventar preço ou prometer algo errado?', 'Ela usa só o que você ensinou. O que não estiver lá, ela não inventa: chama o seu time. E você testa antes, na própria tela da Nanda.'],
 ['O cliente percebe que é uma IA?', 'Ela escreve curto e natural, como no WhatsApp. Se o cliente perguntar, ela diz com naturalidade que é a assistente virtual da empresa e oferece chamar alguém.'],
 ['Responder clientes tem custo da Meta?', 'Não. Responder quem te chamou é grátis. A Meta só cobra quando a empresa inicia a conversa depois de 24 horas (lembretes, campanhas), e o Oi Nanda mostra o valor antes.'],
 ['Meu número pode ser bloqueado?', 'O risco existe em qualquer WhatsApp e vem quase sempre de disparo para quem não conhece a empresa. O Oi Nanda usa a API oficial, envia campanhas aos poucos e com freios. Siga o guia de conexão e não importe listas frias para disparar.'],
 ['Quantas pessoas podem atender ao mesmo tempo?', 'Depende do plano: 2 no Essencial, 5 no Profissional e 12 no Equipe. Todas no mesmo número, cada uma com o seu acesso.'],
 ['Funciona fora do horário comercial?', 'A Nanda responde 24 horas. Automações e campanhas só saem das 8h às 21h, para não incomodar.'],
 ['E se eu cancelar, perco tudo?', 'Não. A conta fica só para consulta, com conversas, contatos e vendas guardados. Dá para reativar quando quiser.'],
 ['Integra com Hotmart, Kiwify, Eduzz, Cakto, Asaas, Vindi, iugu, Mercado Pago, Pagar.me, PagBank ou Stripe?', 'Sim. Vendas aprovadas e cobranças pagas entram sozinhas, e boleto, Pix, cartão recusado e carrinho abandonado viram recuperação automática pelo WhatsApp. Liga em Empresa › Plataformas de venda.'],
 ['Dá para usar com Instagram?', 'Sim. O Direct do Instagram chega na mesma tela de Conversas, com a Nanda ajudando.'],
 ['Preciso de ajuda para configurar. Como faço?', 'Responda qualquer e-mail do Oi Nanda ou fale com a gente pelo WhatsApp. Quem responde é uma pessoa do time.'],
],
};
