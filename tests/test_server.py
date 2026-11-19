import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import TOKENS, WEBHOOK_SECRET


async def _call(base, agent, tool, args=None, token=None):
    headers = {"Authorization": f"Bearer {token or TOKENS[agent]}"}
    async with streamablehttp_client(base + "/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args or {})
    if res.isError:  # levée hors des task groups pour obtenir une RuntimeError simple
        raise RuntimeError(res.content[0].text)
    return res.structuredContent if res.structuredContent is not None else json.loads(res.content[0].text)


def call(server, agent, tool, args=None, token=None):
    return asyncio.run(_call(server["base"], agent, tool, args, token))


def test_tools_listed(server):
    async def go():
        async with streamablehttp_client(server["base"] + "/mcp",
                                         headers={"Authorization": f"Bearer {TOKENS['agent-b']}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    names = asyncio.run(go())
    assert {"remember", "recall", "list_recent", "forget", "update", "send_message",
            "check_inbox", "mark_read", "reply", "list_agents"} <= names


def test_auth_required(server):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    hdr = {"Accept": "application/json, text/event-stream"}
    assert httpx.post(server["base"] + "/mcp", json=body, headers=hdr).status_code == 401
    bad = {**hdr, "Authorization": "Bearer nope-nope-nope-nope"}
    assert httpx.post(server["base"] + "/mcp", json=body, headers=bad).status_code == 401
    assert httpx.get(server["base"] + "/health").json() == {"status": "ok"}


def test_query_token_optional(server):
    async def go():
        async with streamablehttp_client(server["base"] + f"/mcp?token={TOKENS['agent-c']}") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return json.loads((await s.call_tool("list_agents", {})).content[0].text)
    assert asyncio.run(go())["me"] == "agent-c"


def test_memory_lifecycle(server):
    f = call(server, "agent-b", "remember", {"fact": "Le propriétaire a choisi AcmeBooks, le logiciel de son comptable ExampleCo",
                                           "category": "Décisions", "tags": ["Banque", "compta"]})
    assert f["author"] == "agent-b" and f["category"] == "décisions" and f["tags"] == ["banque", "compta"]
    call(server, "agent-a", "remember", {"fact": "agent-a tourne sur un VPS Linux", "category": "infra"})

    # recherche insensible aux accents et aux préfixes
    res = call(server, "agent-c", "recall", {"query": "agent-a vps"})["results"]
    assert any("VPS" in r["fact"] and r["author"] == "agent-a" for r in res)
    res = call(server, "agent-c", "recall", {"query": "comptab"})["results"]
    assert res and res[0]["id"] == f["id"]
    assert call(server, "agent-c", "recall", {"query": "\"()*:^"})["results"] == []
    assert call(server, "agent-c", "recall", {"query": "VPS", "category": "décisions"})["results"] == []

    up = call(server, "agent-a", "update", {"id": f["id"], "fact": "Le propriétaire utilise AcmeBooks (ExampleCo) pour ses factures"})
    assert up["updated_by"] == "agent-a" and up["author"] == "agent-b" and up["updated_at"] >= up["created_at"]

    recent = call(server, "agent-b", "list_recent", {"limit": 5})["results"]
    assert recent[0]["id"] == f["id"]

    assert call(server, "agent-b", "forget", {"id": f["id"]}) == {"deleted": f["id"]}
    assert all(r["id"] != f["id"] for r in call(server, "agent-b", "recall", {"query": "AcmeBooks"})["results"])
    with pytest.raises(RuntimeError):
        call(server, "agent-b", "forget", {"id": f["id"]})


def test_mailbox_and_webhook(server, webhook_server):
    _, received = webhook_server
    received.clear()
    sent = call(server, "agent-b", "send_message", {"to_agent": "agent-a", "subject": "Sauvegarde",
                                                  "body": "Peux-tu vérifier la sauvegarde du NAS ?"})
    assert sent["to_agent"] == "agent-a" and sent["webhook"] is True

    # le webhook d'agent-a est notifié, signé, sans le corps par défaut
    for _ in range(50):
        if received:
            break
        time.sleep(0.1)
    assert received, "webhook non reçu"
    hook = received[0]
    p = hook["json"]
    assert p["event_type"] == "agent_message" and p["from_agent"] == "agent-b" and p["message_id"] == sent["id"]
    assert "body" not in p
    h = {k.lower(): v for k, v in hook["headers"].items()}
    assert h["x-hub-signature-256"] == "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), hook["raw"], hashlib.sha256).hexdigest()
    ts = h["x-webhook-timestamp"]
    assert h["x-webhook-signature-v2"] == hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + hook["raw"],
                                                   hashlib.sha256).hexdigest()
    assert h["x-extra"] == "1"

    # seul le destinataire voit le message
    assert call(server, "agent-c", "check_inbox")["count"] == 0
    inbox = call(server, "agent-a", "check_inbox")
    assert inbox["count"] == 1 and inbox["unread"][0]["body"].startswith("Peux-tu")
    assert "DONNÉE" in inbox["note"]

    with pytest.raises(RuntimeError):
        call(server, "agent-c", "mark_read", {"id": sent["id"]})

    rep = call(server, "agent-a", "reply", {"id": sent["id"], "body": "Sauvegarde OK ce matin."})
    assert rep["to_agent"] == "agent-b" and rep["thread_id"] == sent["thread_id"]
    assert call(server, "agent-a", "check_inbox")["count"] == 0  # reply marque l'original lu

    s_inbox = call(server, "agent-b", "check_inbox")["unread"]
    assert s_inbox[0]["subject"] == "Re: Sauvegarde" and s_inbox[0]["in_reply_to"] == sent["id"]
    assert call(server, "agent-b", "mark_read", {"id": s_inbox[0]["id"]})["read"] is True
    assert call(server, "agent-b", "check_inbox")["count"] == 0

    with pytest.raises(RuntimeError):
        call(server, "agent-b", "send_message", {"to_agent": "inconnu", "subject": "x", "body": "y"})


def test_persistence_file(server):
    import sqlite3
    con = sqlite3.connect(server["db"])
    assert con.execute("SELECT count(*) FROM audit").fetchone()[0] > 0
    actions = {r[0] for r in con.execute("SELECT action FROM audit")}
    assert {"remember", "update", "forget", "send_message"} <= actions


def test_config_rejects_bad_setups(tmp_path, monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("MP_AGENTS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("MP_TOKENS", raising=False)
    with pytest.raises(RuntimeError):
        load_settings()
    monkeypatch.setenv("MP_TOKENS", "a:same-token-123456789,b:same-token-123456789")
    with pytest.raises(ValueError):
        load_settings()
    monkeypatch.setenv("MP_TOKENS", "a:court")
    with pytest.raises(ValueError):
        load_settings()
    monkeypatch.setenv("MP_TOKENS", "agent-a:tok-0123456789abcdefgh")
    monkeypatch.setenv("MP_ALLOWED_HOSTS", "memoire.exemple.com")
    s = load_settings()
    assert s.authenticate("tok-0123456789abcdefgh").name == "agent-a"
    assert s.authenticate("mauvais") is None
    assert "memoire.exemple.com" in s.allowed_hosts


def test_dns_rebinding_protection(tmp_path, monkeypatch):
    """Avec MP_ALLOWED_HOSTS, un Host inconnu est refusé même avec un jeton valide."""
    from starlette.testclient import TestClient
    from app.config import load_settings
    from app.server import build
    monkeypatch.setenv("MP_AGENTS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MP_TOKENS", "agent-a:tok-0123456789abcdefgh")
    monkeypatch.setenv("MP_ALLOWED_HOSTS", "memoire.exemple.com")
    monkeypatch.setenv("MP_DB_PATH", str(tmp_path / "m.db"))
    app, store = build(load_settings())
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"Authorization": "Bearer tok-0123456789abcdefgh", "Accept": "application/json, text/event-stream"}
    with TestClient(app) as c:
        ok = c.post("/mcp", json=body, headers={**hdr, "Host": "memoire.exemple.com"})
        assert ok.status_code == 200, ok.text
        bad = c.post("/mcp", json=body, headers={**hdr, "Host": "evil.example.com"})
        assert bad.status_code in (400, 421)
    store.close()


def test_tunnel_and_tailnet_hosts_allowed(tmp_path, monkeypatch):
    """Nom du tunnel Cloudflare, IP Tailscale et localhost acceptés ; le reste refusé."""
    from starlette.testclient import TestClient
    from app.config import load_settings
    from app.server import build
    monkeypatch.setenv("MP_AGENTS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("MP_TOKENS", "agent-c:tok-0123456789abcdefgh")
    monkeypatch.setenv("MP_ALLOWED_HOSTS", "memoire.exemple.com, 100.64.0.10:*, vps.ton-tailnet.ts.net")
    monkeypatch.setenv("MP_DB_PATH", str(tmp_path / "m.db"))
    app, store = build(load_settings())
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"Authorization": "Bearer tok-0123456789abcdefgh", "Accept": "application/json, text/event-stream"}
    with TestClient(app) as c:
        for host in ("memoire.exemple.com", "100.64.0.10:8765", "vps.ton-tailnet.ts.net", "127.0.0.1:8765", "localhost:8765"):
            r = c.post("/mcp", json=body, headers={**hdr, "Host": host})
            assert r.status_code == 200, (host, r.status_code, r.text)
        for host in ("autre.exemple.com", "203.0.113.5:8765"):
            assert c.post("/mcp", json=body, headers={**hdr, "Host": host}).status_code in (400, 421)
    store.close()
