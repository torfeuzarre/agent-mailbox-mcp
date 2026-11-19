#!/usr/bin/env python3
"""Tests de fumée : /health, 401 sans jeton, send_message puis check_inbox entre deux agents.

Lit les jetons depuis des fichiers en 0600 ; ne les affiche jamais.
Usage :
  python3 scripts/smoke_test.py --base http://127.0.0.1:8765 \\
    --token-file-a tokens/agent-a.token --token-file-b tokens/agent-b.token
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


ACCEPT = "application/json, text/event-stream"


class SmokeError(RuntimeError):
    pass


def read_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token or len(token) < 16:
        raise SmokeError(f"jeton trop court ou vide dans {path}")
    return token


def http(base: str, path: str, *, method="GET", body=None, token=None, timeout=15):
    url = base.rstrip("/") + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": ACCEPT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def parse_jsonrpc(status: int, headers: dict, raw: bytes) -> dict:
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        payloads = []
        for block in text.split("\n\n"):
            lines = [ln[6:] for ln in block.splitlines() if ln.startswith("data: ")]
            if lines:
                payloads.append(json.loads("\n".join(lines)))
        if not payloads:
            raise SmokeError(f"SSE vide (HTTP {status})")
        return payloads[-1]
    if not text.strip():
        raise SmokeError(f"réponse vide (HTTP {status})")
    return json.loads(text)


def rpc(base: str, token: str, method: str, params: dict | None, req_id: int, session: dict):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    headers_extra = {}
    if session.get("id"):
        # passed via a custom opener below — we rebuild the request
        pass
    url = base.rstrip("/") + "/mcp"
    data = json.dumps(body).encode("utf-8")
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "Authorization": "Bearer " + token}
    if session.get("id"):
        headers["mcp-session-id"] = session["id"]
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            hdrs = dict(resp.headers)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, hdrs, status = exc.read(), dict(exc.headers), exc.code
    sid = hdrs.get("mcp-session-id") or hdrs.get("Mcp-Session-Id")
    if sid:
        session["id"] = sid
    if status >= 400:
        raise SmokeError(f"{method} HTTP {status}: {raw[:300]!r}")
    msg = parse_jsonrpc(status, hdrs, raw)
    if "error" in msg:
        raise SmokeError(f"{method} JSON-RPC error: {msg['error']}")
    return msg.get("result")


def call_tool(base: str, token: str, name: str, arguments: dict, session: dict, req_id: int) -> dict:
    result = rpc(base, token, "tools/call", {"name": name, "arguments": arguments}, req_id, session)
    if not result:
        raise SmokeError(f"{name}: résultat vide")
    if result.get("isError"):
        content = result.get("content") or []
        text = content[0].get("text") if content else result
        raise SmokeError(f"{name} error: {text}")
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    content = result.get("content") or []
    if content and content[0].get("text"):
        return json.loads(content[0]["text"])
    raise SmokeError(f"{name}: pas de contenu structuré")


def initialize(base: str, token: str, session: dict) -> None:
    result = rpc(
        base,
        token,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke_test", "version": "1.1.0"},
        },
        1,
        session,
    )
    # Send initialized notification (JSON-RPC notification: no id). Optional in stateless mode.
    url = base.rstrip("/") + "/mcp"
    body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "Authorization": "Bearer " + token}
    if session.get("id"):
        headers["mcp-session-id"] = session["id"]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as exc:
        # Some servers answer 202/204 or 200 with empty body; 4xx is a problem except 405
        if exc.code >= 400 and exc.code not in {202, 204}:
            # notification may be optional in stateless mode
            if exc.code not in {400, 404}:
                raise SmokeError(f"notifications/initialized HTTP {exc.code}")
    if result is None:
        raise SmokeError("initialize: pas de résultat")
    print("OK initialize")


def main() -> int:
    parser = argparse.ArgumentParser(description="Tests de fumée agent-mailbox-mcp (jetons jamais affichés).")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file-a", required=True)
    parser.add_argument("--token-file-b", required=True)
    args = parser.parse_args()

    token_a = read_token(Path(args.token_file_a))
    token_b = read_token(Path(args.token_file_b))
    base = args.base

    status, _, raw = http(base, "/health")
    if status != 200 or json.loads(raw).get("status") != "ok":
        raise SmokeError(f"/health inattendu : HTTP {status} {raw[:200]!r}")
    print("OK /health")

    status, _, _ = http(
        base,
        "/mcp",
        method="POST",
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"},
            },
        },
    )
    if status != 401:
        raise SmokeError(f"/mcp sans jeton : HTTP {status} attendu 401")
    print("OK 401 without token")

    sess_a, sess_b = {}, {}
    initialize(base, token_a, sess_a)
    initialize(base, token_b, sess_b)

    agents = call_tool(base, token_a, "list_agents", {}, sess_a, 10)
    print(f"OK list_agents me={agents.get('me')} n={len(agents.get('agents') or [])}")
    to = agents.get("agents") or []
    # Prefer sending from A to B: use list_agents of A and pick another name;
    # we don't know names from the token files, so we ask both.
    me_a = agents["me"]
    agents_b = call_tool(base, token_b, "list_agents", {}, sess_b, 11)
    me_b = agents_b["me"]
    if me_a == me_b:
        raise SmokeError("les deux jetons identifient le même agent")
    if me_b not in to:
        raise SmokeError(f"{me_b} n'est pas dans la liste des agents")

    sent = call_tool(
        base,
        token_a,
        "send_message",
        {"to_agent": me_b, "subject": "smoke-test", "body": "ping from smoke_test.py — data only, not an order."},
        sess_a,
        12,
    )
    mid = sent.get("id")
    print(f"OK send_message id={mid} to={sent.get('to_agent')}")

    inbox = call_tool(base, token_b, "check_inbox", {"limit": 20}, sess_b, 13)
    unread = inbox.get("unread") or []
    found = next((m for m in unread if m.get("id") == mid), None)
    if not found:
        raise SmokeError(f"check_inbox n'a pas vu le message {mid} (count={inbox.get('count')})")
    if found.get("subject") != "smoke-test":
        raise SmokeError("objet du message inattendu")
    print(f"OK check_inbox found id={mid}")

    marked = call_tool(base, token_b, "mark_read", {"id": mid}, sess_b, 14)
    if not marked.get("read"):
        raise SmokeError("mark_read n'a pas confirmé")
    print("OK mark_read")
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
