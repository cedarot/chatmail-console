#!/usr/bin/env python3
"""Small, read-only Chatmail operations console.

The service deliberately uses only the Python standard library so the image is
small and the source-adapter boundary remains easy to replace after the
deployed Chatmail data sources are confirmed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ACCESS_LINE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<request>[^"]*)"\s+(?P<status>\d{3})\s+(?P<bytes>\S+)'
    r'(?:\s+"[^"]*"\s+"(?P<user_agent>[^"]*)")?'
)
REQUEST_LINE = re.compile(r"^(?P<method>[A-Z]+)\s+(?P<target>\S+)(?:\s+HTTP/\d(?:\.\d)?)?$")


class SourceUnavailable(Exception):
    """Raised when a configured source cannot be read or decoded."""


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    admin_username: str
    admin_password: str
    session_secret: str
    users_source: str
    logins_source: str
    access_log_source: str
    ip_masking: bool
    retention_hours: int
    page_size_max: int
    cookie_secure: bool
    access_log_max_lines: int

    @classmethod
    def from_env(cls) -> "Config":
        config = cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=env_int("PORT", 8080, 1, 65535),
            admin_username=os.getenv("ADMIN_USERNAME", ""),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            session_secret=os.getenv("SESSION_SECRET", ""),
            users_source=os.getenv("USERS_SOURCE", "/data/users.json"),
            logins_source=os.getenv("LOGINS_SOURCE", "/data/logins.json"),
            access_log_source=os.getenv("ACCESS_LOG_SOURCE", "/data/access.log"),
            ip_masking=env_bool("IP_MASKING", True),
            retention_hours=env_int("RETENTION_HOURS", 168, 1, 24 * 365),
            page_size_max=env_int("PAGE_SIZE_MAX", 50, 1, 500),
            cookie_secure=env_bool("COOKIE_SECURE", False),
            access_log_max_lines=env_int("ACCESS_LOG_MAX_LINES", 20000, 100, 200000),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.admin_username or not self.admin_password:
            raise ValueError("ADMIN_USERNAME and ADMIN_PASSWORD must be configured")
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%d/%b/%Y:%H:%M:%S %z", "%d/%b/%Y:%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def mask_ip(value: Any) -> str:
    if not value:
        return "Unavailable"
    text = str(value).strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return "Masked"
    if address.version == 4:
        parts = text.split(".")
        return ".".join(parts[:2] + ["***", "***"])
    groups = address.exploded.split(":")
    return ":".join(groups[:2] + ["****"])


def device_summary(user_agent: Any) -> str:
    if not user_agent:
        return "Unavailable"
    text = str(user_agent)
    if "Edg/" in text or "Edge/" in text:
        browser = "Edge"
    elif "Chrome/" in text:
        browser = "Chrome"
    elif "Firefox/" in text:
        browser = "Firefox"
    elif "Safari/" in text and "Chrome/" not in text:
        browser = "Safari"
    else:
        browser = "Browser unavailable"

    if "iPhone" in text or "iPad" in text:
        platform = "iOS"
    elif "Android" in text:
        platform = "Android"
    elif "Windows" in text:
        platform = "Windows"
    elif "Mac OS X" in text or "Macintosh" in text:
        platform = "macOS"
    elif "Linux" in text:
        platform = "Linux"
    else:
        platform = "Device unavailable"
    return f"{browser} on {platform}"


def redact_path(value: Any) -> str:
    if not value:
        return "/"
    request = str(value).strip()
    match = REQUEST_LINE.match(request)
    target = match.group("target") if match else request.split()[0]
    parsed = urlsplit(target)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path[:2048]


def safe_event_id(prefix: str, values: Iterable[Any]) -> str:
    raw = "|".join("" if value is None else str(value) for value in values)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def json_items(path: Path, keys: tuple[str, ...]) -> list[Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceUnavailable(f"Unable to read configured source: {path.name}") from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    raise SourceUnavailable(f"Configured source has an unsupported format: {path.name}")


class SourceAdapter:
    def __init__(self, config: Config):
        self.config = config
        self.cutoff = lambda: now_utc() - timedelta(hours=config.retention_hours)

    def users(self) -> list[dict[str, Any]]:
        source = str(self.config.users_source)
        if source.startswith("maildir:"):
            return self.maildir_users(Path(source.removeprefix("maildir:")))
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in json_items(Path(source), ("users", "items")):
            if isinstance(item, dict):
                user_id = item.get("userId") or item.get("id") or item.get("username")
                label = item.get("label") or item.get("displayName")
            else:
                user_id, label = item, None
            if user_id is None:
                continue
            user_id = str(user_id)
            if user_id in seen:
                continue
            seen.add(user_id)
            records.append({"userId": user_id, "label": str(label) if label else None})
        records.sort(key=lambda item: item["userId"].lower())
        return records

    def maildir_users(self, root: Path) -> list[dict[str, Any]]:
        """Enumerate mailbox directory names only; never reads mailbox files."""
        if not root.is_dir():
            raise SourceUnavailable("Configured mailbox directory is unavailable")
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            frontier = [root]
            candidates: list[Path] = []
            for _ in range(4):
                next_frontier = [child for parent in frontier for child in parent.iterdir() if child.is_dir()]
                candidates.extend(child for child in next_frontier if "@" in child.name)
                frontier = next_frontier
        except OSError as exc:
            raise SourceUnavailable("Configured mailbox directory is unavailable") from exc
        for candidate in candidates:
            user_id = candidate.name
            if "@" not in user_id or user_id.startswith(".") or user_id in seen:
                continue
            seen.add(user_id)
            records.append({"userId": user_id, "label": None})
        records.sort(key=lambda item: item["userId"].lower())
        return records

    def logins(self) -> list[dict[str, Any]]:
        source = str(self.config.logins_source)
        if source.startswith("docker-json:"):
            return self.docker_json_logins(Path(source.removeprefix("docker-json:")))
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in json_items(Path(source), ("logins", "events", "items")):
            if not isinstance(item, dict):
                continue
            timestamp = parse_timestamp(item.get("timestamp") or item.get("time") or item.get("createdAt"))
            if not timestamp or timestamp < self.cutoff():
                continue
            user_id = item.get("userId") or item.get("user_id") or item.get("user")
            ip = item.get("ip") or item.get("sourceIp") or item.get("remoteAddr")
            user_agent = item.get("userAgent") or item.get("user_agent")
            event_id = str(item.get("eventId") or item.get("id") or safe_event_id("login", (user_id, timestamp, ip)))
            if event_id in seen:
                continue
            seen.add(event_id)
            records.append(
                {
                    "eventId": event_id,
                    "userId": str(user_id) if user_id is not None else None,
                    "timestamp": isoformat(timestamp),
                    "_timestamp": timestamp,
                    "ip": str(ip) if ip else None,
                    "device": device_summary(user_agent),
                }
            )
        return sorted(records, key=lambda item: item["_timestamp"], reverse=True)

    def docker_json_logins(self, path: Path) -> list[dict[str, Any]]:
        """Parse Dovecot imap-login records from Docker's JSON log driver."""
        login_line = re.compile(r"imap-login: Login: user=<([^>]+)>.*?rip=([^,\s]+)")
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = deque(handle, maxlen=self.config.access_log_max_lines)
        except OSError as exc:
            raise SourceUnavailable("Configured Chatmail container log is unavailable") from exc
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = str(payload.get("log", ""))
            match = login_line.search(message)
            timestamp = parse_timestamp(payload.get("time"))
            if not match or not timestamp or timestamp < self.cutoff():
                continue
            user_id, ip = match.groups()
            event_id = safe_event_id("login", (user_id, timestamp, ip))
            if event_id in seen:
                continue
            seen.add(event_id)
            records.append(
                {
                    "eventId": event_id,
                    "userId": user_id,
                    "timestamp": isoformat(timestamp),
                    "_timestamp": timestamp,
                    "ip": ip,
                    "device": "Unavailable",
                }
            )
        return sorted(records, key=lambda item: item["_timestamp"], reverse=True)

    def access(self) -> list[dict[str, Any]]:
        try:
            with Path(str(self.config.access_log_source)).open("r", encoding="utf-8", errors="replace") as handle:
                lines = deque(handle, maxlen=self.config.access_log_max_lines)
        except OSError as exc:
            raise SourceUnavailable(f"Unable to read configured source: {self.config.access_log_source.name}") from exc

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in lines:
            match = ACCESS_LINE.match(line.rstrip("\n"))
            if not match:
                continue
            timestamp = parse_timestamp(match.group("time"))
            if not timestamp or timestamp < self.cutoff():
                continue
            request = match.group("request")
            request_match = REQUEST_LINE.match(request)
            if not request_match:
                continue
            method = request_match.group("method")
            path = redact_path(request)
            ip = match.group("ip")
            status = int(match.group("status"))
            size = match.group("bytes")
            response_bytes = int(size) if size.isdigit() else None
            user_agent = match.group("user_agent")
            event_id = safe_event_id("access", (ip, timestamp, request, status, response_bytes))
            if event_id in seen:
                continue
            seen.add(event_id)
            records.append(
                {
                    "eventId": event_id,
                    "timestamp": isoformat(timestamp),
                    "_timestamp": timestamp,
                    "ip": ip,
                    "device": device_summary(user_agent),
                    "method": method,
                    "path": path,
                    "status": status,
                    "responseBytes": response_bytes,
                }
            )
        return sorted(records, key=lambda item: item["_timestamp"], reverse=True)


def display_record(record: dict[str, Any], ip_masking: bool) -> dict[str, Any]:
    output = {key: value for key, value in record.items() if not key.startswith("_")}
    if "ip" in output:
        output["ip"] = mask_ip(output["ip"]) if ip_masking else output["ip"] or "Unavailable"
    return output


def parse_page(params: dict[str, list[str]], maximum: int) -> tuple[int, int]:
    def integer(name: str, default: int) -> int:
        value = params.get(name, [str(default)])[0]
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed < 1:
            raise ValueError(f"{name} must be positive")
        return parsed

    page = integer("page", 1)
    page_size = integer("pageSize", min(25, maximum))
    if page_size > maximum:
        raise ValueError(f"pageSize must not exceed {maximum}")
    return page, page_size


def paginate(records: list[dict[str, Any]], page: int, page_size: int, ip_masking: bool) -> dict[str, Any]:
    start = (page - 1) * page_size
    items = [display_record(item, ip_masking) for item in records[start : start + page_size]]
    return {"items": items, "page": page, "pageSize": page_size, "hasMore": start + page_size < len(records)}


def filter_time(records: list[dict[str, Any]], params: dict[str, list[str]]) -> list[dict[str, Any]]:
    from_value = parse_timestamp(params.get("from", [""])[0])
    to_value = parse_timestamp(params.get("to", [""])[0])
    if params.get("from", [""])[0] and not from_value:
        raise ValueError("from must be a valid ISO 8601 timestamp")
    if params.get("to", [""])[0] and not to_value:
        raise ValueError("to must be a valid ISO 8601 timestamp")
    if from_value and to_value and from_value > to_value:
        raise ValueError("from must not be later than to")
    return [
        record
        for record in records
        if (not from_value or record["_timestamp"] >= from_value)
        and (not to_value or record["_timestamp"] <= to_value)
    ]


class ConsoleService:
    def __init__(self, config: Config):
        self.config = config
        self.adapter = SourceAdapter(config)
        self.login_attempts: dict[str, list[float]] = {}
        self.login_lock = threading.Lock()
        self.last_observed: dict[str, str] = {}

    def source_status(self, source_name: str, loader: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            records = loader()
        except SourceUnavailable:
            return [], {
                "state": "unavailable",
                "message": "Source unavailable",
                "observedAt": self.last_observed.get(source_name),
            }
        observed_at = isoformat(now_utc())
        self.last_observed[source_name] = observed_at
        return records, {"state": "healthy", "message": "Source available", "observedAt": observed_at}

    def can_attempt_login(self, remote_ip: str) -> bool:
        current = time.time()
        with self.login_lock:
            attempts = [value for value in self.login_attempts.get(remote_ip, []) if current - value < 300]
            self.login_attempts[remote_ip] = attempts
            return len(attempts) < 5

    def record_login_attempt(self, remote_ip: str, success: bool) -> None:
        with self.login_lock:
            if success:
                self.login_attempts.pop(remote_ip, None)
            else:
                self.login_attempts.setdefault(remote_ip, []).append(time.time())

    def audit(self, event: str, remote_ip: str, **details: Any) -> None:
        payload = {"event": event, "remoteIp": mask_ip(remote_ip), "timestamp": isoformat(now_utc())}
        payload.update({key: value for key, value in details.items() if key not in {"password", "token", "raw"}})
        print(json.dumps(payload, separators=(",", ":")), flush=True)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_signed(payload: dict[str, Any], secret: str) -> str:
    body = b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def read_signed(value: str | None, secret: str) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    body, signature = value.rsplit(".", 1)
    expected = b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def cookie_value(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for item in header.split(";"):
        key, _, value = item.strip().partition("=")
        if key == name:
            return value
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatmailConsole/1.0"

    @property
    def service(self) -> ConsoleService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Keep standard request logs free of URL query strings and headers.
        self.service.audit("http_request", self.client_address[0], method=self.command, path=urlsplit(self.path).path)

    def headers_common(self, no_store: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'")
        if no_store:
            self.send_header("Cache-Control", "no-store")

    def send_json(self, payload: dict[str, Any], status: int = 200, no_store: bool = True) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.headers_common(no_store)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body: str, status: int = 200, cookies: list[str] | None = None) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.headers_common(True)
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.headers_common(True)
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def auth_session(self) -> dict[str, Any] | None:
        token = cookie_value(self.headers.get("Cookie"), "session")
        return read_signed(token, self.service.config.session_secret)

    def require_auth(self) -> dict[str, Any] | None:
        session = self.auth_session()
        if not session:
            self.send_json({"error": {"code": "AUTH_REQUIRED", "message": "Authentication required"}}, 401)
            return None
        return session

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.send_json({"status": "ok", "service": "chatmail-console"}, no_store=False)
            return
        if path == "/readyz":
            self.send_json({"status": "ready", "configuration": "valid"}, no_store=False)
            return
        if path == "/login":
            token = secrets.token_urlsafe(24)
            self.send_html(render_login(token), cookies=[csrf_cookie_header(token, self.service.config)])
            return
        if path == "/":
            session = self.auth_session()
            if not session:
                self.redirect("/login")
                return
            csrf = str(session.get("csrf", ""))
            self.send_html(render_index(csrf))
            return
        if path.startswith("/static/"):
            self.serve_static(path.removeprefix("/static/"))
            return
        if path.startswith("/api/"):
            if not self.require_auth():
                return
            self.api_get(path, parse_qs(urlsplit(self.path).query))
            return
        self.send_json({"error": {"code": "NOT_FOUND", "message": "Not found"}}, 404)

    def serve_static(self, name: str) -> None:
        allowed = {"app.js": "text/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8"}
        if name not in allowed:
            self.send_json({"error": {"code": "NOT_FOUND", "message": "Not found"}}, 404)
            return
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            self.send_json({"error": {"code": "NOT_FOUND", "message": "Not found"}}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", allowed[name])
        self.send_header("Content-Length", str(len(body)))
        self.headers_common(True)
        self.end_headers()
        self.wfile.write(body)

    def api_get(self, path: str, params: dict[str, list[str]]) -> None:
        try:
            if path == "/api/summary":
                users, user_status = self.service.source_status("users", self.service.adapter.users)
                logins, login_status = self.service.source_status("logins", self.service.adapter.logins)
                access, access_status = self.service.source_status("access", self.service.adapter.access)
                self.service.audit("dashboard_read", self.client_address[0], resource="summary")
                self.send_json(
                    {
                        "userCount": len(users) if user_status["state"] == "healthy" else None,
                        "loginCount": len(logins) if login_status["state"] == "healthy" else None,
                        "accessCount": len(access) if access_status["state"] == "healthy" else None,
                        "recentLogins": [display_record(item, self.service.config.ip_masking) for item in logins[:5]],
                        "recentAccess": [display_record(item, self.service.config.ip_masking) for item in access[:5]],
                        "sources": {"users": user_status, "logins": login_status, "access": access_status},
                        "observedAt": isoformat(now_utc()),
                    }
                )
                return

            page, page_size = parse_page(params, self.service.config.page_size_max)
            if path == "/api/users":
                query = params.get("query", [""])[0].strip().lower()
                records, status = self.service.source_status("users", self.service.adapter.users)
                if query:
                    records = [item for item in records if query in item["userId"].lower()]
                response = paginate(records, page, page_size, self.service.config.ip_masking)
                response["sourceStatus"] = status
            elif path == "/api/logins":
                records, status = self.service.source_status("logins", self.service.adapter.logins)
                records = filter_time(records, params)
                user_id = params.get("userId", [""])[0].strip().lower()
                ip_query = params.get("ip", [""])[0].strip()
                records = [
                    item
                    for item in records
                    if (not user_id or user_id in (item.get("userId") or "").lower())
                    and (not ip_query or ip_query in (item.get("ip") or "") or ip_query in mask_ip(item.get("ip")))
                ]
                response = paginate(records, page, page_size, self.service.config.ip_masking)
                response["sourceStatus"] = status
            elif path == "/api/access":
                records, status = self.service.source_status("access", self.service.adapter.access)
                records = filter_time(records, params)
                ip_query = params.get("ip", [""])[0].strip()
                route = params.get("path", [""])[0].strip().lower()
                method = params.get("method", [""])[0].strip().upper()
                status_class = params.get("statusClass", [""])[0].strip()
                if method and method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                    raise ValueError("method is not supported")
                if status_class and status_class not in {"2", "3", "4", "5"}:
                    raise ValueError("statusClass must be 2, 3, 4, or 5")
                records = [
                    item
                    for item in records
                    if (not ip_query or ip_query in item["ip"] or ip_query in mask_ip(item["ip"]))
                    and (not route or route in item["path"].lower())
                    and (not method or item["method"] == method)
                    and (not status_class or str(item["status"]).startswith(status_class))
                ]
                response = paginate(records, page, page_size, self.service.config.ip_masking)
                response["sourceStatus"] = status
            else:
                self.send_json({"error": {"code": "NOT_FOUND", "message": "Not found"}}, 404)
                return
            response["observedAt"] = isoformat(now_utc())
            self.service.audit("dashboard_read", self.client_address[0], resource=path.removeprefix("/api/"))
            self.send_json(response)
        except ValueError as exc:
            self.send_json({"error": {"code": "INVALID_REQUEST", "message": str(exc)}}, 400)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/auth/login":
            self.login()
            return
        if path == "/auth/logout":
            self.logout()
            return
        self.send_json({"error": {"code": "NOT_FOUND", "message": "Not found"}}, 404)

    def request_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 16 * 1024:
            raise ValueError("request is too large")
        body = self.rfile.read(length).decode("utf-8", "replace")
        return {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}

    def login(self) -> None:
        remote_ip = self.client_address[0]
        if not self.service.can_attempt_login(remote_ip):
            self.send_json({"error": {"code": "RATE_LIMITED", "message": "Too many login attempts"}}, 429)
            return
        try:
            form = self.request_form()
        except ValueError as exc:
            self.send_json({"error": {"code": "INVALID_REQUEST", "message": str(exc)}}, 400)
            return
        csrf_cookie = cookie_value(self.headers.get("Cookie"), "login_csrf")
        valid_csrf = bool(csrf_cookie) and hmac.compare_digest(csrf_cookie, form.get("csrf", ""))
        valid_credentials = hmac.compare_digest(form.get("username", ""), self.service.config.admin_username) and hmac.compare_digest(
            form.get("password", "").encode("utf-8"), self.service.config.admin_password.encode("utf-8")
        )
        if not valid_csrf or not valid_credentials:
            self.service.record_login_attempt(remote_ip, False)
            self.service.audit("admin_login_failure", remote_ip)
            token = secrets.token_urlsafe(24)
            self.send_html(render_login(token, "Invalid credentials or session. Please try again."), 401, [csrf_cookie_header(token, self.service.config)])
            return
        self.service.record_login_attempt(remote_ip, True)
        session = {"user": self.service.config.admin_username, "exp": int(time.time()) + 8 * 3600, "csrf": secrets.token_urlsafe(24)}
        self.service.audit("admin_login_success", remote_ip)
        cookie = session_cookie(make_signed(session, self.service.config.session_secret), self.service.config)
        self.redirect("/", [cookie, expire_cookie("login_csrf", self.service.config)])

    def logout(self) -> None:
        session = self.auth_session()
        if not session:
            self.send_json({"error": {"code": "AUTH_REQUIRED", "message": "Authentication required"}}, 401)
            return
        token = self.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, str(session.get("csrf", ""))):
            self.send_json({"error": {"code": "CSRF_FAILED", "message": "Invalid request"}}, 403)
            return
        self.service.audit("admin_logout", self.client_address[0])
        self.redirect("/login", [expire_cookie("session", self.service.config)])


def cookie_flags(config: Config) -> str:
    secure = "; Secure" if config.cookie_secure else ""
    return f"; Path=/; HttpOnly; SameSite=Lax{secure}"


def session_cookie(value: str, config: Config) -> str:
    return f"session={value}{cookie_flags(config)}"


def csrf_cookie_header(value: str, config: Config) -> str:
    return f"login_csrf={value}{cookie_flags(config)}"


def expire_cookie(name: str, config: Config) -> str:
    secure = "; Secure" if config.cookie_secure else ""
    return f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"


def render_login(csrf: str, error: str | None = None) -> str:
    message = f'<p class="error" role="alert">{html_escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Chatmail Console</title><link rel="stylesheet" href="/static/styles.css"></head>
<body class="login-page"><main class="login-card"><p class="eyebrow">ADMINISTRATION</p><h1>Chatmail Console</h1><p class="muted">Sign in to view operational activity.</p>{message}<form method="post" action="/auth/login"><input type="hidden" name="csrf" value="{html_escape(csrf)}"><label>Username<input name="username" autocomplete="username" required></label><label>Password<input type="password" name="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form></main></body></html>"""


def render_index(csrf: str) -> str:
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return template.replace("__CSRF__", html_escape(csrf))


def html_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


def create_server(config: Config) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.service = ConsoleService(config)  # type: ignore[attr-defined]
    return server


def main() -> None:
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    server = create_server(config)
    print(json.dumps({"event": "server_started", "host": config.host, "port": config.port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
