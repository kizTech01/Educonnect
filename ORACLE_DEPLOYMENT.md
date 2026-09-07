# Deploying EduConnect on an Oracle Cloud Always Free VM

This deployment uses Docker Compose to run five services:

- `caddy` provides HTTPS and forwards application requests.
- `web` runs Django with Gunicorn.
- `reminders` checks for due class reminders once per minute.
- `db` runs PostgreSQL.
- Docker volumes preserve PostgreSQL data, uploaded media, static files, and
  Caddy's TLS certificates across restarts and upgrades.

## Before you deploy

1. Create an Oracle Cloud Always Free Ubuntu VM and note its public IP address.
2. Register a domain or subdomain, such as `portal.example.edu.ng`, and create
   an A record pointing to that public IP address.
3. In Oracle Cloud's security list, allow inbound TCP ports `80` and `443`.
   Keep SSH (port `22`) restricted to your own IP address when possible.
4. Install Docker Engine and the Docker Compose plugin on the VM.

## Configure secrets

On the VM, clone this repository and enter its directory. Create the production
environment file from the template:

```bash
cp .env.production.example .env.production
```

Edit `.env.production` and replace all `CHANGE_ME` values. Use the same
alphanumeric password in `POSTGRES_PASSWORD` and `DATABASE_URL`. Set
`CADDY_DOMAIN`, `DJANGO_ALLOWED_HOSTS`, and `CSRF_TRUSTED_ORIGINS` to your real
domain. Do not commit `.env.production`.

You can generate a Django secret and database password with:

```bash
openssl rand -hex 32
openssl rand -hex 24
```

## Start the production services

Run this command from the repository root:

```bash
docker compose --env-file .env.production -f compose.production.yaml up -d --build
```

After Caddy obtains the certificate, open `https://your-domain` in a browser.
The `reminders` service then runs `send_due_course_reminders` once a minute.
Each reminder is deduplicated by the application, so a reminder is not sent
twice if the service restarts.

## Check service logs

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs -f web reminders caddy
```

## Update the deployment

```bash
git pull
docker compose --env-file .env.production -f compose.production.yaml up -d --build
```

Never use `docker compose down -v` on the server unless you deliberately want
to delete the PostgreSQL database, uploaded files, and HTTPS certificate data.
