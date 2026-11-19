import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {"agent-a": "tok-agent-a-0123456789abcdef", "agent-b": "tok-agent-b-0123456789abcdef",
          "agent-c": "tok-agent-c-0123456789abcdef"}
WEBHOOK_SECRET = "secret-webhook-test"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        Hook.received.append({"headers": dict(self.headers), "raw": raw, "json": json.loads(raw)})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def webhook_server():
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/webhooks/memoire", Hook.received
    srv.shutdown()


def _agents_file(tmp, url_hook):
    agents = {"agents": {
        "agent-a": {"token_sha256": hashlib.sha256(TOKENS["agent-a"].encode()).hexdigest(),
                   "webhook": {"url": url_hook, "secret": WEBHOOK_SECRET,
                               "headers": {"X-Extra": "1"}}},
        "agent-b": {"token_sha256": hashlib.sha256(TOKENS["agent-b"].encode()).hexdigest()},
        "agent-c": {"token": TOKENS["agent-c"]},
    }}
    (tmp / "agents.json").write_text(json.dumps(agents))
    return tmp / "agents.json"


def start_server(tmp, url_hook, extra_env=None):
    """Démarre le vrai serveur (sous-processus) ; renvoie (proc, info)."""
    port = free_port()
    env = {**os.environ, "MP_AGENTS_FILE": str(_agents_file(tmp, url_hook)), "MP_DB_PATH": str(tmp / "data" / "m.db"),
           "MP_PORT": str(port), "MP_HOST": "127.0.0.1", "MP_ALLOW_QUERY_TOKEN": "1", **(extra_env or {})}
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(proc.stdout.read().decode())
    return proc, {"base": base, "db": tmp / "data" / "m.db", "files": tmp / "data" / "files", "tmp": tmp}


@pytest.fixture(scope="session")
def server(tmp_path_factory, webhook_server):
    tmp = tmp_path_factory.mktemp("srv")
    proc, info = start_server(tmp, webhook_server[0])
    yield info
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="session")
def fserver(tmp_path_factory, webhook_server):
    """Limites par défaut, instance dédiée aux tests de fichiers (ne pollue pas les boîtes de `server`)."""
    tmp = tmp_path_factory.mktemp("srvd")
    proc, info = start_server(tmp, webhook_server[0])
    yield info
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="session")
def server_files(tmp_path_factory, webhook_server):
    """Limites réduites pour tester taille / quota / nombre."""
    tmp = tmp_path_factory.mktemp("srvf")
    proc, info = start_server(tmp, webhook_server[0], {
        "MP_FILE_MAX_BYTES": "100000", "MP_FILES_QUOTA_BYTES": "250000", "MP_FILES_MAX_COUNT": "6"})
    yield info
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="session")
def server_ttl(tmp_path_factory, webhook_server):
    """Expiration en ~3 s, purge de fond toutes les 0,2 s."""
    tmp = tmp_path_factory.mktemp("srvt")
    proc, info = start_server(tmp, webhook_server[0], {
        "MP_FILE_TTL_DAYS": str(3 / 86400), "MP_FILE_PURGE_INTERVAL": "0.2"})
    yield info
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="session")
def server_nofiles(tmp_path_factory, webhook_server):
    tmp = tmp_path_factory.mktemp("srvn")
    proc, info = start_server(tmp, webhook_server[0], {"MP_FILES_ENABLED": "0"})
    yield info
    proc.terminate()
    proc.wait(5)
