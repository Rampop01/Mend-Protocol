"""
Mend HTTP server: crash intake, incident API and the dashboard.

  GET  /                               dashboard
  GET  /health                         liveness
  GET  /api/v1/status                  configuration and live on-chain state
  GET  /api/v1/incidents               recent incidents
  GET  /api/v1/incidents/<id>          one incident in full
  POST /api/v1/incidents               report a crash (used by the SDK)
  POST /api/v1/incidents/<id>/approve  approve the selected patch
  POST /api/v1/incidents/<id>/reject   reject it

By default the server listens on loopback only. It refuses to listen on any
other interface unless MEND_INGEST_TOKEN is set, and then every /api/ request
must carry `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from mend.bot.telegram_bot import TelegramBot
from mend.config import Settings, load_settings
from mend.integrations.escrow import EscrowClient
from mend.integrations.near import NearError
from mend.swarm.orchestrator import IncidentError, SwarmOrchestrator

logger = logging.getLogger("mend.server")

DASHBOARD = Path(__file__).resolve().parent / "web" / "dashboard.html"
MAX_BODY_BYTES = 512 * 1024
INCIDENT_ROUTE = re.compile(r"^/api/v1/incidents/(INC-[A-Z0-9]{4,32})(/approve|/reject)?$")


class MendServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: Settings, orchestrator: SwarmOrchestrator) -> None:
        super().__init__((settings.host, settings.port), MendHandler)
        self.settings = settings
        self.orchestrator = orchestrator
        self._chain_cache: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
        self._chain_lock = threading.Lock()

    def chain_overview(self) -> Dict[str, Any]:
        """On-chain state, cached briefly so the dashboard's polling does not hammer the RPC."""
        escrow = self.orchestrator.escrow
        if escrow is None:
            return {"configured": False, "error": "NEAR is not configured. Run `mend setup`."}
        with self._chain_lock:
            at, cached = self._chain_cache
            if cached is not None and time.time() - at < 8:
                return cached
            try:
                overview: Dict[str, Any] = {"configured": True, **escrow.overview()}
            except NearError as exc:
                overview = {"configured": True, "error": str(exc), "contract_id": escrow.contract_id}
            self._chain_cache = (time.time(), overview)
            return overview

    def invalidate_chain_cache(self) -> None:
        with self._chain_lock:
            self._chain_cache = (0.0, None)


class MendHandler(BaseHTTPRequestHandler):
    server: MendServer
    server_version = "Mend"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    # -- helpers -----------------------------------------------------------

    def _json(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self) -> bool:
        """Rejects DNS-rebinding requests when serving on loopback."""
        if not self.server.settings.is_loopback:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        public = self.server.settings.public_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "::1") or (bool(public) and host == public)

    def _authorized(self) -> bool:
        token = self.server.settings.ingest_token
        if not token:
            return True  # loopback-only mode; enforced at startup
        supplied = self.headers.get("Authorization", "")
        return supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:].strip(), token)

    def _same_origin(self) -> bool:
        """Blocks cross-site form posts from a browser on the same machine."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.split("://", 1)[-1] == (self.headers.get("Host") or "")

    def _read_json(self) -> Optional[Dict[str, Any]]:
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": "Request body too large"})
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "Body is not valid JSON"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "Body must be a JSON object"})
            return None
        return data

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._json(403, {"error": "Unexpected Host header"})
            return
        path = self.path.split("?", 1)[0]
        orchestrator = self.server.orchestrator

        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path in ("/", "/index.html"):
            body = DASHBOARD.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.startswith("/api/"):
            self._json(404, {"error": "Not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "Missing or invalid bearer token"})
            return

        if path == "/api/v1/status":
            settings = self.server.settings
            self._json(200, {
                "projects": [{"repo": p.repo, "managed": p.managed, "base_branch": p.base_branch} for p in settings.projects],
                "incidents": orchestrator.store.count(),
                "bounty_near": settings.bounty_near,
                "agents": {
                    "forge_rules": {"enabled": True},
                    "forge_llm": {
                        "enabled": orchestrator.llm_forge.available,
                        "model": settings.llm_model if orchestrator.llm_forge.available else None,
                    },
                },
                "telegram": bool(settings.telegram_token and settings.telegram_chat_id),
                "github_pull_requests": bool(settings.github_token),
                "near": self.server.chain_overview(),
            })
        elif path == "/api/v1/incidents":
            self._json(200, {"incidents": [orchestrator.public_view(i, full=False) for i in orchestrator.store.list(50)]})
        else:
            match = INCIDENT_ROUTE.match(path)
            incident = orchestrator.store.get(match.group(1)) if match and not match.group(2) else None
            if incident is None:
                self._json(404, {"error": "Not found"})
            else:
                self._json(200, orchestrator.public_view(incident))

    def _project_for_token(self):
        """A project's own SDK token: it may only report crashes for that project."""
        supplied = self.headers.get("Authorization", "")
        if not supplied.startswith("Bearer "):
            return None
        digest = hashlib.sha256(supplied[7:].strip().encode()).hexdigest()
        for project in self.server.settings.projects:
            if project.ingest_token_hash and hmac.compare_digest(project.ingest_token_hash, digest):
                return project
        return None

    def do_POST(self) -> None:
        if not self._host_allowed() or not self._same_origin():
            self._json(403, {"error": "Cross-origin request refused"})
            return
        path = self.path.split("?", 1)[0]
        project_scope = None
        if not self._authorized():
            project_scope = self._project_for_token() if path == "/api/v1/incidents" else None
            if project_scope is None:
                self._json(401, {"error": "Missing or invalid bearer token"})
                return
        orchestrator = self.server.orchestrator
        data = self._read_json()
        if data is None:
            return

        try:
            if path == "/api/v1/incidents":
                incident = orchestrator.report(data, project_hint=project_scope)
                self._json(202, {
                    "incident_id": incident["incident_id"],
                    "status": incident["status"],
                    "occurrences": incident["occurrences"],
                })
                return
            match = INCIDENT_ROUTE.match(path)
            if not match or not match.group(2):
                self._json(404, {"error": "Not found"})
                return
            who = "api:" + (re.sub(r"[^\w.@-]", "", str(data.get("approver") or "operator"))[:40] or "operator")
            if match.group(2) == "/approve":
                incident = orchestrator.approve(match.group(1), approver=who)
            else:
                incident = orchestrator.reject(match.group(1), rejector=who)
            self.server.invalidate_chain_cache()
            self._json(200, orchestrator.public_view(incident))
        except IncidentError as exc:
            self._json(409 if "not awaiting" in str(exc) or "changed since" in str(exc) else 400, {"error": str(exc)})
        except Exception:
            logger.exception("Request failed: POST %s", path)
            self._json(500, {"error": "Internal error. See the server log."})


def build(settings: Settings) -> Tuple[MendServer, SwarmOrchestrator, Optional[TelegramBot]]:
    if not settings.is_loopback and not settings.ingest_token:
        raise SystemExit(
            f"Refusing to listen on {settings.host} without MEND_INGEST_TOKEN. "
            "Set a token, or use MEND_HOST=127.0.0.1."
        )
    escrow = EscrowClient.load(settings.near_keys_file)
    accounts = None
    if settings.public:
        from mend.accounts import AccountStore, Vault
        accounts = AccountStore(settings.db_path, Vault(settings.secret_key))
    orchestrator = SwarmOrchestrator(settings, escrow=escrow, accounts=accounts)
    bot: Optional[TelegramBot] = None
    if settings.telegram_token:
        hosted = None
        if accounts is not None:
            from mend.hosted import HostedService
            hosted = HostedService(settings, orchestrator, accounts)
        bot = TelegramBot(settings.telegram_token, settings.telegram_chat_id, settings.telegram_approver_ids,
                          orchestrator, hosted=hosted)
    return MendServer(settings, orchestrator), orchestrator, bot


def serve(settings: Optional[Settings] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or load_settings()
    server, orchestrator, bot = build(settings)
    orchestrator.start()

    escrow = orchestrator.escrow
    for project in settings.projects:
        logger.info("Watching %s (%s)", project.repo, "GitHub, fixes as pull requests" if project.managed else "local checkout")
    logger.info("NEAR: %s", f"{escrow.network} contract {escrow.contract_id}" if escrow else "not configured (run `mend setup`)")
    logger.info("LLM forge agent: %s", f"enabled ({settings.llm_model})" if orchestrator.llm_forge.available else "disabled (set NEAR_AI_API_KEY)")
    logger.info("GitHub pull requests: %s", f"enabled ({settings.github_token_source})" if settings.github_token else "disabled (run `gh auth login` or set GITHUB_TOKEN)")
    if bot is not None:
        try:
            bot.start()
            if not settings.telegram_chat_id:
                logger.warning("TELEGRAM_CHAT_ID is not set: send /start to the bot to find it")
        except RuntimeError as exc:
            logger.error("%s", exc)
    else:
        logger.info("Telegram: disabled (set TELEGRAM_BOT_TOKEN). Use the dashboard or `mend approve`.")
    logger.info("Listening on http://%s:%d", settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        if bot is not None:
            bot.stop()
        orchestrator.stop()
        server.server_close()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
