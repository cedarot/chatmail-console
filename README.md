# Chatmail Console

Chatmail Console is a small, read-only administrative dashboard for a Chatmail deployment. It shows user IDs, recent login events, and website access metadata from configured read-only sources.

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

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `ADMIN_USERNAME` | Single MVP administrator username | required |
| `ADMIN_PASSWORD` | Single MVP administrator password | required |
| `SESSION_SECRET` | HMAC signing secret, at least 32 characters | required |
| `BIND_ADDRESS` | Host bind address used by Compose | `127.0.0.1` |
| `CHATMAIL_MAIL_DIR` | Chatmail mail directory mounted read-only at `/data/mail` | `./demo/mail` |
| `CHATMAIL_MAIL_GID` | Host group ID allowed to enumerate mailbox directories | `996` |
| `CHATMAIL_ACCESS_LOG_HOST` | Host Nginx access log mounted at `/data/access.log` | `./demo/access.log` |
| `CHATMAIL_LOGIN_EVENTS_HOST` | Sanitized Dovecot login JSONL mounted at `/data/chatmail.json.log` | `./demo/chatmail.json.log` |
| `HOST_PORT` | Host port mapped to the container | `8080` |
| `USERS_SOURCE` | User source, including `maildir:` directory adapter | `maildir:/data/mail` |
| `LOGINS_SOURCE` | Login source, including `docker-json:` adapter | `docker-json:/data/chatmail.json.log` |
| `ACCESS_LOG_SOURCE` | Website access log path inside container | `/data/access.log` |
| `IP_MASKING` | Mask IPs in UI/API by default | `true` |
| `COOKIE_SECURE` | Mark session cookies Secure when HTTPS is used | `false` |
| `RETENTION_HOURS` | Maximum event age served by the console | `168` |
| `PAGE_SIZE_MAX` | Maximum server-side page size | `50` |
| `ACCESS_LOG_MAX_LINES` | Maximum access-log lines inspected per request | `20000` |

The console strips query strings and fragments from access paths. It does not store an event cache, request bodies, message bodies, cookies, authorization headers, passwords, or tokens. IP masking is enabled by default. Retention is applied when reading events and is finite by default. Mounting `/srv/chatmail-relay/data/mail` is a sensitive read-only operation; use a dedicated deployment account/container mount and do not grant the console write access.

The Compose service receives the Chatmail mail service group as a supplementary group so it can enumerate mailbox directory names. On the current host, the domain directory is owned by group ID `996`; grant that group read/execute access to the domain directory without changing mailbox file ownership or write permissions.

## API and health checks

- `POST /auth/login` and `POST /auth/logout`
- `GET /api/summary`
- `GET /api/users?query=&page=&pageSize=`
- `GET /api/logins?from=&to=&userId=&ip=&page=&pageSize=`
- `GET /api/access?from=&to=&ip=&path=&method=&statusClass=&page=&pageSize=`
- `GET /healthz` for process health
- `GET /readyz` for configuration readiness

All data APIs require authentication. List responses are bounded and include source status. The console reports an unavailable source instead of treating it as zero data.

## Tests

```sh
python -m unittest discover -s tests -v
python -m py_compile app.py
docker compose config
docker compose build
```

## Operations

Use `docker compose ps`, `docker compose logs --no-color --tail=200`, and `curl -fsS http://127.0.0.1:8080/healthz` for basic diagnosis. The container is read-only, drops Linux capabilities, uses a non-root user, and has a no-new-privileges security option. The host should provide log rotation for container output. Back up only deployment configuration and the upstream source according to its own retention policy; the console has no persistent event database to back up.

## Production source configuration

For the current deployment at `8.166.118.0`, create `.env` from `.env.example` and set:

```dotenv
CHATMAIL_MAIL_DIR=/srv/chatmail-relay/data/mail
CHATMAIL_ACCESS_LOG_HOST=/srv/chatmail-relay/data/nginx/log/access.log
CHATMAIL_LOGIN_EVENTS_HOST=/srv/chatmail-console/data/logins.jsonl
```

Install the host sanitizer from `ops/sync_dovecot_logins.py` and its systemd unit. It reads the current Chatmail Docker JSON log as root and writes only sanitized Dovecot login user/IP/timestamp fields to `/srv/chatmail-console/data/logins.jsonl`. The raw Docker log is never mounted into the console. The console does not restart or modify the Chatmail container.

Upgrade by pulling the reviewed source, rebuilding the image, and restarting with `docker compose up -d --build`. Verify the health endpoint and login after each upgrade.
