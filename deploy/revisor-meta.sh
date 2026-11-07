#!/usr/bin/env bash
# Cria (ou renova) o acesso do revisor da Meta na empresa onde o Instagram de teste está conectado.
# A senha é sorteada aqui e só aparece na sua tela: copie para o pedido de análise da Meta.
set -euo pipefail
cd "$(dirname "$0")"
IG="${1:-alta_creators}"
docker compose exec -T -e IG_USER="$IG" api python - <<'PY'
import asyncio, os, secrets
from core import db, now, uid, hash_password
async def main():
    a = await db.ig_accounts.find_one({'username': os.environ['IG_USER'], 'status': 'connected'})
    if not a: print('Nenhuma empresa com @' + os.environ['IG_USER'] + ' conectado. Nada foi criado.'); return
    email = 'revisor.meta@oinanda.com.br'
    pw = secrets.token_urlsafe(10)
    u = await db.users.find_one({'email': email})
    if u and u['company_id'] != a['company_id']: print('Esse e-mail já existe em outra empresa. Nada foi alterado.'); return
    upd = {'password_hash': hash_password(pw), 'must_change_password': False, 'active': True, 'role': 'manager'}
    if u: await db.users.update_one({'id': u['id']}, {'$set': upd, '$inc': {'auth_version': 1}})
    else: await db.users.insert_one({'id': uid(), 'company_id': a['company_id'], 'name': 'Revisor Meta', 'email': email, 'auth_version': 0, 'created_at': now(), **upd})
    c = await db.companies.find_one({'id': a['company_id']}, {'name': 1})
    print('PRONTO: acesso do revisor na empresa "%s" (gestor, Instagram @%s).' % ((c or {}).get('name'), os.environ['IG_USER']))
    print()
    print('Cole isto no pedido da Meta (Instruções para o analista > campo de credenciais de teste):')
    print()
    print('Login: https://app.oinanda.com.br  |  E-mail: %s  |  Senha: %s' % (email, pw))
asyncio.run(main())
PY
