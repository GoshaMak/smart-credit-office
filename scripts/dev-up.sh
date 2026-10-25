#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    secret=$(python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')
    (umask 077; printf 'JWT_SECRET=%s\n' "$secret" > .env)
fi

docker compose up --build --detach --wait
printf '%s\n' 'Backend is ready at http://localhost:8000'
