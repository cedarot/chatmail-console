# Chatmail Console

Chatmail Console is a small, read-only administrative dashboard for a Chatmail deployment. It shows user IDs, recent login events, and website access metadata from configured read-only sources.

The first adapter reads three files:

- `users.json`: a JSON array of strings or objects with `userId`/`id` and an optional `label`.
- `logins.json`: a JSON array of objects with `timestamp`, optional `userId`, `ip`, and `userAgent`.
- `access.log`: Nginx/Apache combined-style access lines.

The adapter boundary is intentionally replaceable. Before connecting to production at `8.166.118.0`, inspect the actual Chatmail user/login source and reverse-proxy log format, then provide read-only mounts or implement a source adapter for that format. Do not mount private message stores unless the adapter requires a documented, read-only query and never returns message content.

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
| `CHATMAIL_DATA_DIR` | Host directory mounted read-only at `/data` | `./demo` |
| `HOST_PORT` | Host port mapped to the container | `8080` |
| `USERS_SOURCE` | User JSON path inside container | `/data/users.json` |
| `LOGINS_SOURCE` | Login JSON path inside container | `/data/logins.json` |
| `ACCESS_LOG_SOURCE` | Website access log path inside container | `/data/access.log` |
| `IP_MASKING` | Mask IPs in UI/API by default | `true` |
| `COOKIE_SECURE` | Mark session cookies Secure when HTTPS is used | `false` |
| `RETENTION_HOURS` | Maximum event age served by the console | `168` |
| `PAGE_SIZE_MAX` | Maximum server-side page size | `50` |
| `ACCESS_LOG_MAX_LINES` | Maximum access-log lines inspected per request | `20000` |

The console strips query strings and fragments from access paths. It does not store an event cache, request bodies, message bodies, cookies, authorization headers, passwords, or tokens. IP masking is enabled by default. Retention is applied when reading events and is finite by default.

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

Upgrade by pulling the reviewed source, rebuilding the image, and restarting with `docker compose up -d --build`. Verify the health endpoint and login after each upgrade.
