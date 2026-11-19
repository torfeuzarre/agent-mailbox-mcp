"""Bout en bout : vrai serveur HTTP, client MCP, faux webhook."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import stat
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import TOKENS, WEBHOOK_SECRET
from test_server import call


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def err(server, agent, tool, args):
    with pytest.raises(RuntimeError) as e:
        call(server, agent, tool, args)
    return str(e.value)


def tool_names(server):
    async def go():
        async with streamablehttp_client(server["base"] + "/mcp",
                                         headers={"Authorization": f"Bearer {TOKENS['agent-b']}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    return asyncio.run(go())


def test_file_tools_listed_and_old_tools_kept(fserver):
    server = fserver
    names = tool_names(server)
    assert {"put_file", "get_file", "list_files", "delete_file"} <= names
    assert {"remember", "recall", "list_recent", "forget", "update", "send_message", "check_inbox",
            "mark_read", "reply", "list_agents"} <= names


def test_roundtrip_notification_inbox_and_isolation(fserver, webhook_server):
    server = fserver
    _, received = webhook_server
    received.clear()
    data = os.urandom(150_000)
    put = call(server, "agent-b", "put_file", {"to_agent": "agent-a", "name": "../../archive-test.tar.gz",
                                              "content_base64": b64(data), "subject": "archive-test 1.0"})
    sha = hashlib.sha256(data).hexdigest()
    assert put["sha256"] == sha and put["size"] == len(data) and put["name"] == "archive-test.tar.gz"
    assert put["webhook"] is True and "secret" in put["warning"].lower()

    # webhook d'agent-a : même mécanisme que send_message (signé), métadonnées seulement, jamais le contenu
    for _ in range(50):
        if received:
            break
        time.sleep(0.1)
    assert received, "webhook non reçu"
    hook = received[0]
    p = hook["json"]
    assert p["event_type"] == "agent_message" and p["from_agent"] == "agent-b" and p["message_id"] == put["message_id"]
    assert p["file_id"] == put["id"] and p["file_sha256"] == sha and p["file_size"] == len(data)
    assert "body" not in p and b64(data[:60]) not in hook["raw"].decode()
    h = {k.lower(): v for k, v in hook["headers"].items()}
    assert h["x-hub-signature-256"] == "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), hook["raw"], hashlib.sha256).hexdigest()

    # message dans la boîte du destinataire, avec id et SHA-256
    inbox = call(server, "agent-a", "check_inbox")["unread"]
    msg = next(m for m in inbox if m["id"] == put["message_id"])
    assert msg["subject"] == "archive-test 1.0" and sha in msg["body"] and f"get_file(id={put['id']})" in msg["body"]
    assert "donnée" in msg["body"]
    assert b64(data[:60]) not in msg["body"]

    # isolement
    for who in ("agent-b", "agent-c"):
        assert "introuvable" in err(server, who, "get_file", {"id": put["id"]})
    assert call(server, "agent-c", "list_files")["files"] == []
    assert [f["id"] for f in call(server, "agent-b", "list_files")["files"]] == [put["id"]]
    mine = call(server, "agent-a", "list_files")["files"][0]
    assert mine["direction"] == "reçu" and mine["sha256"] == sha and "content_base64" not in mine

    # récupération : contenu identique, SHA revérifié
    got = call(server, "agent-a", "get_file", {"id": put["id"]})
    assert base64.b64decode(got["content_base64"]) == data
    assert got["sha256"] == sha and got["sha256_verified"] is True and got["from_agent"] == "agent-b"
    meta = call(server, "agent-a", "get_file", {"id": put["id"], "include_content": False})
    assert "content_base64" not in meta and meta["sha256"] == sha

    # permissions disque + aucun nom de fichier fourni sur le disque
    d = server["files"]
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    blobs = list(d.iterdir())
    assert [b.name for b in blobs] == [f"{put['id']}.bin"] and stat.S_IMODE(os.stat(blobs[0]).st_mode) == 0o600

    # suppression par le destinataire
    assert call(server, "agent-a", "delete_file", {"id": put["id"]}) == {"deleted": put["id"]}
    assert "introuvable" in err(server, "agent-a", "get_file", {"id": put["id"]})
    assert not list(d.iterdir())


def test_journal_without_content(fserver):
    server = fserver
    import sqlite3
    marker = b"MARQUEUR-UNIQUE-DE-CONTENU-0042"
    put = call(server, "agent-c", "put_file", {"to_agent": "agent-b", "name": "journal-nom-secret.txt",
                                               "content_base64": b64(marker * 3)})
    call(server, "agent-b", "get_file", {"id": put["id"]})
    call(server, "agent-b", "delete_file", {"id": put["id"]})
    con = sqlite3.connect(server["db"])
    rows = con.execute("SELECT agent, action, detail FROM audit WHERE target_id=? AND action LIKE 'file_%'", (put["id"],)).fetchall()
    assert [r[1] for r in rows] == ["file_put", "file_get", "file_delete"]
    sha = hashlib.sha256(marker * 3).hexdigest()
    assert all(f"sha256={sha}" in r[2] and "size=93" in r[2] for r in rows)
    assert rows[0][0] == "agent-c" and "to=agent-b" in rows[0][2]
    con.close()
    assert all("journal-nom-secret" not in r[2] for r in rows)        # le journal ne contient pas le nom
    raw = b"".join(p.read_bytes() for p in server["db"].parent.glob("m.db*"))
    assert marker not in raw and base64.b64encode(marker) not in raw  # le contenu n'est jamais dans la base


def test_secrets_refused(fserver):
    server = fserver
    m = err(server, "agent-b", "put_file", {"to_agent": "agent-a", "name": "AuthKey_ABC.p8", "content_base64": b64(b"x")})
    assert "refusé" in m and "p8" in m
    key = ("-----BEGIN " + "EC PRIVATE KEY-----\n" + "QUJDREVGR0g=" * 12 + "\n-----END " + "EC PRIVATE KEY-----\n").encode()
    m = err(server, "agent-b", "put_file", {"to_agent": "agent-a", "name": "notes.txt", "content_base64": b64(key)})
    assert "refusé" in m and "QUJDREVGR0g" not in m and "précaution" in m
    assert not [f for f in call(server, "agent-b", "list_files")["files"] if f["name"] == "notes.txt"]


def test_unknown_and_self_recipient(fserver):
    server = fserver
    assert "inconnu" in err(server, "agent-b", "put_file", {"to_agent": "nobody", "name": "a", "content_base64": b64(b"x")})
    assert "autre agent" in err(server, "agent-b", "put_file", {"to_agent": "agent-b", "name": "a", "content_base64": b64(b"x")})
    assert "invalide" in err(server, "agent-b", "put_file", {"to_agent": "agent-a", "name": "a", "content_base64": "%%%"})


def test_default_limit_is_5_mib_end_to_end(fserver):
    server = fserver
    ok = os.urandom(5 * 1024 * 1024)
    put = call(server, "agent-b", "put_file", {"to_agent": "agent-c", "name": "cinq-mo.bin", "content_base64": b64(ok)})
    assert put["size"] == 5 * 1024 * 1024
    got = call(server, "agent-c", "get_file", {"id": put["id"]})
    assert hashlib.sha256(base64.b64decode(got["content_base64"])).hexdigest() == hashlib.sha256(ok).hexdigest()
    call(server, "agent-c", "delete_file", {"id": put["id"]})
    assert "trop gros" in err(server, "agent-b", "put_file", {"to_agent": "agent-c", "name": "x.bin",
                                                            "content_base64": b64(ok + b"x")})


def test_configured_limit_quota_and_count(server_files):
    s = server_files
    big = os.urandom(100_001)
    assert "trop gros" in err(s, "agent-b", "put_file", {"to_agent": "agent-a", "name": "x.bin", "content_base64": b64(big)})
    ids = [call(s, "agent-b", "put_file", {"to_agent": "agent-a", "name": f"{i}.bin",
                                         "content_base64": b64(os.urandom(100_000))})["id"] for i in range(2)]
    assert "Quota" in err(s, "agent-c", "put_file", {"to_agent": "agent-a", "name": "z.bin",
                                                    "content_base64": b64(os.urandom(60_000))})   # 260 000 > 250 000
    call(s, "agent-a", "delete_file", {"id": ids[0]})
    call(s, "agent-c", "put_file", {"to_agent": "agent-a", "name": "z.bin", "content_base64": b64(os.urandom(60_000))})
    for i in range(4):
        call(s, "agent-c", "put_file", {"to_agent": "agent-a", "name": f"s{i}", "content_base64": b64(b"x" * 10)})
    assert "Quota" in err(s, "agent-c", "put_file", {"to_agent": "agent-a", "name": "s9", "content_base64": b64(b"x")})  # 7e > 6


def test_http_body_limit(server_files):
    base = server_files["base"]
    hdr = {"Authorization": f"Bearer {TOKENS['agent-b']}", "Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}
    r = httpx.post(base + "/mcp", content=b"x" * 600_000, headers=hdr)            # Content-Length > limite
    assert r.status_code == 413

    def chunks():                                                                 # flux sans Content-Length
        for _ in range(20):
            yield b"y" * 50_000
    r = httpx.post(base + "/mcp", content=chunks(), headers=hdr)
    assert r.status_code == 413
    assert httpx.post(base + "/mcp", content=b"x" * 600_000).status_code == 401  # l'authentification passe d'abord
    assert httpx.get(base + "/health").status_code == 200


def test_automatic_expiry_and_background_purge(server_ttl):
    s = server_ttl
    put = call(s, "agent-b", "put_file", {"to_agent": "agent-a", "name": "ephemere.txt", "content_base64": b64(b"bientot parti")})
    assert call(s, "agent-a", "get_file", {"id": put["id"]})["content_base64"]
    assert (s["files"] / f"{put['id']}.bin").exists()
    deadline = time.time() + 12
    while time.time() < deadline and (s["files"] / f"{put['id']}.bin").exists():
        time.sleep(0.3)
    assert not (s["files"] / f"{put['id']}.bin").exists(), "le contenu aurait dû être purgé en tâche de fond"
    assert "introuvable" in err(s, "agent-a", "get_file", {"id": put["id"]})
    import sqlite3
    con = sqlite3.connect(s["db"])
    assert con.execute("SELECT agent FROM audit WHERE action='file_expire' AND target_id=?", (put["id"],)).fetchone() == ("system",)
    con.close()


def test_files_can_be_disabled(server_nofiles):
    names = tool_names(server_nofiles)
    assert not ({"put_file", "get_file", "list_files", "delete_file"} & names)
    assert {"send_message", "remember"} <= names
    assert call(server_nofiles, "agent-b", "list_agents")["me"] == "agent-b"
    assert not server_nofiles["files"].exists()


def test_unauthenticated_cannot_use_files(fserver):
    server = fserver
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_file", "arguments": {"id": 1}}}
    r = httpx.post(server["base"] + "/mcp", json=body, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
