#!/usr/bin/env bash
# Run once as root on a fresh Ubuntu Droplet, from the project folder:
#   bash deploy/setup-droplet.sh apply.example.com
set -euo pipefail

domain="${1:?Usage: bash deploy/setup-droplet.sh your-domain.com}"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

if [ ! -f .env ]; then
  password="$(openssl rand -base64 24 | tr -d '/+=')"
  session_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  vault_key="$(openssl rand -base64 32 | tr '+/' '-_')"
  sed -e "s|^DOMAIN=.*|DOMAIN=${domain}|" \
      -e "s|^APP_PASSWORD=.*|APP_PASSWORD=${password}|" \
      -e "s|^SESSION_KEY=.*|SESSION_KEY=${session_key}|" \
      -e "s|^VAULT_KEY=.*|VAULT_KEY=${vault_key}|" \
      .env.example > .env
  chmod 600 .env
  echo "Created .env. Your dashboard password is in that file (APP_PASSWORD)."
fi

for name in DATABASE_URL SUPABASE_URL SUPABASE_SERVICE_KEY; do
  if ! grep -q "^${name}=." .env; then
    echo "Add ${name} to .env (see the notes in that file), then run this script again."
    exit 1
  fi
done

docker compose up -d --build
docker compose ps
echo "Done. Open https://${domain} once DNS points at this Droplet."
