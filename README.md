# Chatmail Console

Chatmail Console is a small, read-only administrative dashboard for a Chatmail deployment. Its dashboard is organized into Recent Activities, Recent Logins, Access Events, and Users tabs, backed by configured read-only sources.

## Dashboard snapshot

![Chatmail Console dashboard](docs/snapshot.png)

The first adapter reads three read-only sources:

- `maildir:/data/mail`: mailbox directory names only. The production Chatmail layout is supported without reading mailbox files or message content.
- `docker-json:/data/chatmail.json.log`: sanitized Dovecot `imap-login` records exported by the host helper.
- `/data/access.log`: Nginx/Apache combined-style access lines.

The adapter boundary is intentionally replaceable. The production deployment below mounts the mailbox directory and Chatmail container log read-only. The mailbox adapter enumerates only directory metadata to identify user IDs. It never opens mailbox files. The Docker JSON log adapter parses only Dovecot login lines and does not expose the raw log record.

## Run with Docker Compose

```sh
cp .env.example .env
```

Replace `ADMIN_PASSWORD` and `SESSION_SECRET` in `.env`. Generate a session secret with a password manager or a command such as `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. Keep `.env` private. Then start the console:

```sh
docker compose config
docker compose up -d --build
```

Open `http://127.0.0.1:8080`. The default Compose bind address is loopback. For production, put the service behind HTTPS and an admin-only network or allowlist, set `COOKIE_SECURE=true`, and change `CHATMAIL_DATA_DIR` to a read-only directory containing the authorized source files. Do not bind this console publicly without a security review.

## Deploy a reviewed release

The production host uses immutable release directories and a `current` symlink. Keep the runtime `.env` outside the source releases; it contains the administrator password and session secret and must never be committed.

### One-command install or redeploy

After the server-side `.env` has been created as described below, run this from a clean checkout of the reviewed branch. It transfers the current checkout, preserves the server secret, rebuilds the image, and starts the console in one pasteable command:

```sh
set -eu
DEPLOY_HOST=8.166.118.0
DEPLOY_ROOT=/srv/chatmail-console
REVISION=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"

git archive --format=tar "$REVISION" | ssh root@"$DEPLOY_HOST" "set -eu
release=$DEPLOY_ROOT/releases/$REVISION
mkdir -p \"\$release\"
tar -xf - -C \"\$release\"
ln -sfn $DEPLOY_ROOT/.env \"\$release/.env\"
ln -sfn \"\$release\" $DEPLOY_ROOT/current
docker compose -p chatmail-console -f $DEPLOY_ROOT/current/docker-compose.yml --env-file $DEPLOY_ROOT/.env up -d --build
curl -fsS http://127.0.0.1:18080/healthz
"
```

The command requires root SSH access to the production host and an existing `/srv/chatmail-console/.env`. It does not print or copy the password or session secret. For first-time secret setup, use the commands below before running the one-command install.

Prepare the host once, then create `/srv/chatmail-console/.env` from `.env.example` with the production source paths. Transfer the private file over SSH and restrict it to root:

```sh
ssh root@8.166.118.0 'install -d -m 700 /srv/chatmail-console /srv/chatmail-console/data'
scp .env root@8.166.118.0:/srv/chatmail-console/.env
ssh root@8.166.118.0 'chmod 600 /srv/chatmail-console/.env'
```

From a clean checkout of the reviewed branch, archive the exact revision to the host and rebuild the console:

```sh
DEPLOY_HOST=8.166.118.0
DEPLOY_ROOT=/srv/chatmail-console
REVISION=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"

git archive --format=tar "$REVISION" | ssh root@"$DEPLOY_HOST" "set -eu
release=$DEPLOY_ROOT/releases/$REVISION
mkdir -p \"\$release\"
tar -xf - -C \"\$release\"
ln -sfn $DEPLOY_ROOT/.env \"\$release/.env\"
ln -sfn \"\$release\" $DEPLOY_ROOT/current
docker compose -p chatmail-console -f $DEPLOY_ROOT/current/docker-compose.yml --env-file $DEPLOY_ROOT/.env up -d --build
"
```

Verify the release locally on the host. The health endpoint must succeed and the container should report `healthy`:

```sh
ssh root@8.166.118.0 'curl -fsS http://127.0.0.1:18080/healthz && docker inspect --format "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}" chatmail-console-chatmail-console-1'
```

To roll back, point `current` at a previously verified release and recreate the service without rebuilding:

```sh
ssh root@8.166.118.0 'set -eu; ln -sfn /srv/chatmail-console/releases/<known-good-revision> /srv/chatmail-console/current; docker compose -p chatmail-console -f /srv/chatmail-console/current/docker-compose.yml --env-file /srv/chatmail-console/.env up -d --no-build'
```

## Production source configuration

For the current deployment at `8.166.118.0`, create `.env` from `.env.example` and set:

```dotenv
CHATMAIL_MAIL_DIR=/srv/chatmail-relay/data/mail
CHATMAIL_ACCESS_LOG_HOST=/srv/chatmail-relay/data/nginx/log/access.log
CHATMAIL_LOGIN_EVENTS_HOST=/srv/chatmail-console/data/logins.jsonl
```

Install the host sanitizer from `ops/sync_dovecot_logins.py` and its systemd unit. It reads the current Chatmail Docker JSON log as root and writes only sanitized Dovecot login user/IP/timestamp fields to `/srv/chatmail-console/data/logins.jsonl`. The raw Docker log is never mounted into the console. The console does not restart or modify the Chatmail container.

Upgrade by pulling the reviewed source, rebuilding the image, and restarting with `docker compose up -d --build`. Verify the health endpoint and login after each upgrade.
