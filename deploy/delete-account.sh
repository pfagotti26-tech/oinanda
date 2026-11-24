#!/usr/bin/env bash
# Exclui de vez uma conta (empresa) e todos os dados dela, pelo e-mail de quem tem acesso.
# Uso: bash delete-account.sh email@exemplo.com
# Mostra o que vai ser apagado e só continua se você digitar EXCLUIR. Não tem como desfazer
# (a não ser restaurando um backup inteiro). Não apaga a conta do admin.
set -euo pipefail
cd "$(dirname "$0")"
E="$(echo "${1:-}" | tr 'A-Z' 'a-z' | tr -d ' ')"
[[ "$E" == *@*.* ]] || { echo "Uso: bash delete-account.sh email@exemplo.com"; exit 1; }
ADM="$(grep -m1 '^ADMIN_EMAIL=' .env | cut -d= -f2- | tr 'A-Z' 'a-z' || true)"
[ "$E" != "$ADM" ] || { echo "Esse é o e-mail do admin. Não dá para excluir por aqui."; exit 1; }
Q="const u=db.users.findOne({email:'$E'});
if(!u){print('NAO_ACHOU')}else{
 const c=db.companies.findOne({id:u.company_id})||{};
 const ppl=db.users.countDocuments({company_id:u.company_id});
 let n=0;db.getCollectionNames().forEach(k=>{if(!['users','companies'].includes(k))n+=db[k].countDocuments({company_id:u.company_id})});
 const b=c.billing||{};
 print('EMPRESA='+(c.name||'?')+'|PESSOAS='+ppl+'|REGISTROS='+n+'|ASSINATURA='+((b.sub_id&&b.status==='active')?'ativa':'nenhuma')+'|ID='+u.company_id)}"
R="$(docker compose exec -T mongo mongosh oinanda --quiet --eval "$Q")"
[ "$R" != "NAO_ACHOU" ] || { echo "Nenhuma conta com o e-mail $E."; exit 1; }
IFS='|' read -r EMP PPL REG ASS CID <<< "$R"
echo "Conta encontrada:"
echo "  ${EMP/EMPRESA=/Empresa: }"
echo "  ${PPL/PESSOAS=/Pessoas com acesso: }"
echo "  ${REG/REGISTROS=/Registros (conversas, contatos, vendas, agenda...): }"
echo "  ${ASS/ASSINATURA=/Assinatura no Mercado Pago: }"
[ "${ASS#ASSINATURA=}" = "nenhuma" ] || echo "  ATENÇÃO: a assinatura ativa não é cancelada por aqui. Cancele antes no Mercado Pago."
read -rp "Isto APAGA DE VEZ a conta e todos os dados acima. Digite EXCLUIR para continuar: " ok
[ "$ok" = "EXCLUIR" ] || { echo "Nada foi apagado."; exit 1; }
CID="${CID#ID=}"
docker compose exec -T mongo mongosh oinanda --quiet --eval "
const ids=db.users.find({company_id:'$CID'},{id:1}).toArray().map(x=>x.id);
db.getCollectionNames().forEach(k=>{if(!['users','companies'].includes(k)){db[k].deleteMany({company_id:'$CID'});if(ids.length)db[k].deleteMany({user_id:{\$in:ids}})}});
db.users.deleteMany({company_id:'$CID'});db.companies.deleteMany({id:'$CID'});print('ok')" >/dev/null
docker compose exec -T api sh -c "rm -rf /media/$CID" 2>/dev/null || true
echo "Pronto: a conta de $E foi excluída. O e-mail já pode criar uma conta nova do zero."
