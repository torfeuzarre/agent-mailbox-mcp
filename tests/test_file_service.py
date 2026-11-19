"""Logique de stockage (sans réseau) : horloge simulée, disque réel dans tmp_path."""
import base64
import hashlib
import os
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import FileSettings, load_settings
from app.db import Store
from app.files import FileError, FileService, sanitize_name


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    clock = Clock()
    cfg = FileSettings(dir=str(tmp_path / "files"), max_bytes=1000, quota_bytes=2500, max_files=4, ttl_days=7)
    svc = FileService(store, cfg, clock)
    yield svc, store, clock, tmp_path
    store.close()


# ---------------- noms ----------------
@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"), ("..\\..\\win\\x.txt", "x.txt"), ("/abs/path/rapport.pdf", "rapport.pdf"),
    ("a\x00b.txt", "ab.txt"), ("évaluation été.pdf", "évaluation été.pdf"), (".bashrc", "bashrc"),
    ("..", "fichier"), ("", "fichier"), ("   ", "fichier"), ("a\u202egpj.exe", "agpj.exe"),
    ("sh;rm -rf *.txt", "sh_rm -rf _.txt"), ("a..b...c.txt", "a.b.c.txt"), ("tab\tnew\nline.txt", "tabnewline.txt"),
])
def test_sanitize_name(raw, expected):
    assert sanitize_name(raw) == expected


def test_sanitize_long_name_keeps_extension():
    n = sanitize_name("x" * 500 + ".tar.gz")
    assert len(n) <= 120 and n.endswith(".gz")
    assert len(sanitize_name("y" * 500)) == 120


# ---------------- cycle nominal ----------------
def test_put_get_roundtrip_with_sha(env):
    svc, store, clock, tmp = env
    data = os.urandom(900)
    row = svc.put("agent-b", "agent-a", "../x/rapport.bin", b64(data), "Objet")
    assert row["sha256"] == hashlib.sha256(data).hexdigest() and row["size"] == 900 and row["name"] == "rapport.bin"
    assert row["expires_at"].startswith("2026-11-05T12:00")  # +7 jours
    out = svc.get("agent-a", row["id"])
    assert base64.b64decode(out["content_base64"]) == data
    assert out["sha256"] == row["sha256"] and out["sha256_verified"] is True
    assert svc.get("agent-a", row["id"], include_content=False).get("content_base64") is None
    assert store.file_get(row["id"])["downloaded_at"]


def test_only_recipient_reads_sender_and_recipient_delete(env):
    svc, *_ = env
    row = svc.put("agent-b", "agent-a", "a.txt", b64(b"hello"))
    for who in ("agent-b", "agent-c", "agent-d"):
        with pytest.raises(FileError):
            svc.get(who, row["id"])
    with pytest.raises(FileError):
        svc.delete("agent-c", row["id"])
    assert svc.delete("agent-b", row["id"]) == {"deleted": row["id"]}   # l'expéditeur peut retirer son envoi
    with pytest.raises(FileError):
        svc.get("agent-a", row["id"])
    r2 = svc.put("agent-b", "agent-a", "b.txt", b64(b"hello2"))
    assert svc.delete("agent-a", r2["id"]) == {"deleted": r2["id"]}
    assert not list(svc.dir.glob("*.bin"))


def test_list_files_both_directions_no_content(env):
    svc, *_ = env
    a = svc.put("agent-b", "agent-a", "a.txt", b64(b"1"))
    b = svc.put("agent-a", "agent-b", "b.txt", b64(b"2"))
    svc.put("agent-c", "agent-d", "c.txt", b64(b"3"))
    lst = {f["id"]: f for f in svc.list("agent-b")}
    assert set(lst) == {a["id"], b["id"]} and lst[a["id"]]["direction"] == "envoyé" and lst[b["id"]]["direction"] == "reçu"
    assert all("content_base64" not in f for f in lst.values())


# ---------------- disque ----------------
def test_disk_layout_and_permissions(env):
    svc, store, clock, tmp = env
    row = svc.put("agent-b", "agent-a", "../../etc/cron.d/evil", b64(b"x" * 10))
    assert sorted(p.name for p in svc.dir.iterdir()) == [f"{row['id']}.bin"]   # le nom fourni n'est jamais un chemin
    assert stat.S_IMODE(os.stat(svc.dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(svc.dir / f"{row['id']}.bin").st_mode) == 0o600
    assert not list(tmp.glob("etc*")) and not (tmp / "etc").exists()


# ---------------- limites ----------------
def test_size_limits_and_invalid_input(env):
    svc, *_ = env
    svc.put("a", "b", "ok.bin", b64(b"x" * 1000))                       # = max
    with pytest.raises(FileError, match="trop gros"):
        svc.put("a", "b", "big.bin", b64(b"x" * 1001))
    with pytest.raises(FileError, match="trop gros"):
        svc.put("a", "b", "huge.bin", "A" * 100000)                      # rejeté avant décodage
    for bad in ("", "   ", "!!!notbase64!!!", "QUJD=", "QUJ"):
        with pytest.raises(FileError):
            svc.put("a", "b", "x.bin", bad)
    with pytest.raises(FileError, match="autre agent"):
        svc.put("a", "a", "x.bin", b64(b"x"))


def test_b64_with_line_breaks_accepted(env):
    svc, *_ = env
    data = os.urandom(600)
    enc = base64.encodebytes(data).decode()   # retours à la ligne MIME
    row = svc.put("a", "b", "x.bin", enc)
    assert base64.b64decode(svc.get("b", row["id"])["content_base64"]) == data


def test_quota_total_and_count(env):
    svc, *_ = env
    ids = [svc.put("a", "b", f"{i}.bin", b64(bytes([i]) * 900))["id"] for i in range(2)]   # 1800 / 2500
    with pytest.raises(FileError, match="Quota"):
        svc.put("a", "b", "3.bin", b64(b"z" * 900))                       # 2700 > 2500
    svc.delete("b", ids[0])
    svc.put("a", "b", "3.bin", b64(b"z" * 900))                           # libéré -> accepté
    svc.put("a", "b", "s1", b64(b"1"))
    svc.put("a", "b", "s2", b64(b"2"))
    with pytest.raises(FileError, match="Quota"):
        svc.put("a", "b", "s3", b64(b"3"))                                # 5e fichier > max_files=4


# ---------------- secrets ----------------
def test_secret_refused_and_logged_without_content(env):
    svc, store, clock, tmp = env
    with pytest.raises(FileError, match="refusé"):
        svc.put("agent-b", "agent-a", "AuthKey_X.p8", b64(b"x"))
    key = ("-----BEGIN " + "PRIVATE KEY-----\n" + "QUJD" * 20 + "\n-----END " + "PRIVATE KEY-----\n").encode()
    with pytest.raises(FileError) as e:
        svc.put("agent-b", "agent-a", "innocent.txt", b64(key))
    assert "QUJDQUJD" not in str(e.value) and "barrière" in str(e.value)
    assert not list(svc.dir.glob("*.bin"))
    refused = store._conn.execute("SELECT agent, detail FROM audit WHERE action='file_refused'").fetchall()
    assert len(refused) == 2 and all("QUJD" not in r["detail"] for r in refused)


# ---------------- intégrité ----------------
def test_corrupted_blob_detected_and_not_returned(env):
    svc, store, *_ = env
    row = svc.put("a", "b", "x.bin", b64(b"contenu original"))
    path = svc.dir / f"{row['id']}.bin"
    path.write_bytes(b"contenu falsifie!")
    with pytest.raises(FileError, match="corrompu"):
        svc.get("b", row["id"])
    path.write_bytes(b"contenu original" + b"+")                        # taille différente
    with pytest.raises(FileError, match="corrompu"):
        svc.get("b", row["id"])
    assert store._conn.execute("SELECT count(*) FROM audit WHERE action='file_corrupt'").fetchone()[0] == 2


def test_missing_blob(env):
    svc, *_ = env
    row = svc.put("a", "b", "x.bin", b64(b"abc"))
    (svc.dir / f"{row['id']}.bin").unlink()
    with pytest.raises(FileError, match="indisponible"):
        svc.get("b", row["id"])
    with pytest.raises(FileError):
        svc.get("b", row["id"])   # la ligne a été nettoyée


# ---------------- expiration ----------------
def test_expiry_and_purge(env):
    svc, store, clock, tmp = env
    row = svc.put("a", "b", "x.bin", b64(b"abc"))
    clock.t += timedelta(days=6, hours=23)
    assert svc.get("b", row["id"])["content_base64"]
    clock.t += timedelta(hours=2)                                         # > 7 jours
    with pytest.raises(FileError, match="introuvable"):
        svc.get("b", row["id"])
    assert not list(svc.dir.glob("*.bin")) and store.file_get(row["id"]) is None
    audit = store._conn.execute("SELECT agent, action, detail FROM audit WHERE action='file_expire'").fetchone()
    assert audit["agent"] == "system" and "sha256=" in audit["detail"] and "size=3" in audit["detail"]


def test_ttl_is_configurable(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    clock = Clock()
    svc = FileService(store, FileSettings(dir=str(tmp_path / "f"), ttl_days=1), clock)
    row = svc.put("a", "b", "x", b64(b"abc"))
    assert row["expires_at"].startswith("2026-10-30T12:00")
    store.close()


def test_orphans_swept(env):
    svc, store, clock, tmp = env
    # contenu orphelin ancien -> supprimé ; récent -> conservé (dépôt peut-être en cours)
    old, new = svc.dir / "9001.bin", svc.dir / "9002.bin"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    os.utime(old, (time.time() - 7200,) * 2)
    svc.purge()
    assert not old.exists() and new.exists()
    # ligne sans contenu (sauvegarde restaurée) : supprimée seulement si > 1 h
    clock.t = datetime.now(timezone.utc)          # created_at vient de l'horloge réelle de la base
    row = svc.put("a", "b", "x", b64(b"abc"))
    (svc.dir / f"{row['id']}.bin").unlink()
    svc.purge()
    assert store.file_get(row["id"]) is not None
    clock.t += timedelta(hours=2)
    svc.purge()
    assert store.file_get(row["id"]) is None


def test_audit_never_contains_content_or_name(env):
    svc, store, *_ = env
    secret_name = "nom-confidentiel-xyz.txt"
    row = svc.put("agent-b", "agent-a", secret_name, b64(b"MARQUEUR-CONTENU-123"))
    svc.get("agent-a", row["id"])
    svc.delete("agent-a", row["id"])
    rows = store._conn.execute("SELECT agent, action, target_id, detail FROM audit WHERE action LIKE 'file_%'").fetchall()
    assert [r["action"] for r in rows] == ["file_put", "file_get", "file_delete"]
    dump = repr([tuple(r) for r in rows])
    assert "MARQUEUR" not in dump and "confidentiel" not in dump
    sha = hashlib.sha256(b"MARQUEUR-CONTENU-123").hexdigest()
    for r in rows:
        assert r["target_id"] == row["id"] and f"sha256={sha}" in r["detail"] and "size=20" in r["detail"]
    assert "to=agent-a" in rows[0]["detail"] and rows[0]["agent"] == "agent-b"


def test_disabled_service(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    svc = FileService(store, FileSettings(enabled=False, dir=str(tmp_path / "f")))
    with pytest.raises(FileError, match="désactivé"):
        svc.put("a", "b", "x", b64(b"abc"))
    assert not (tmp_path / "f").exists()
    store.close()


# ---------------- configuration ----------------
def test_config_defaults_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("MP_AGENTS_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("MP_TOKENS", "agent-a:tok-0123456789abcdefgh")
    monkeypatch.setenv("MP_DB_PATH", str(tmp_path / "d" / "m.db"))
    for k in [k for k in os.environ if k.startswith(("MP_FILE", "MP_FILES"))]:
        monkeypatch.delenv(k)
    f = load_settings().files
    assert (f.enabled, f.max_bytes, f.ttl_days, f.scan_paranoid) == (True, 5 * 1024 * 1024, 7.0, False)
    assert f.quota_bytes == 100 * 1024 * 1024 and f.dir == str(tmp_path / "d" / "files")
    monkeypatch.setenv("MP_FILE_MAX_BYTES", "1048576")
    monkeypatch.setenv("MP_FILE_TTL_DAYS", "2.5")
    monkeypatch.setenv("MP_FILE_SCAN_PARANOID", "1")
    f = load_settings().files
    assert (f.max_bytes, f.ttl_days, f.scan_paranoid) == (1048576, 2.5, True)
    monkeypatch.setenv("MP_FILE_MAX_BYTES", str(26 * 1024 * 1024))
    with pytest.raises(ValueError, match="plafond"):
        load_settings()
    monkeypatch.setenv("MP_FILE_MAX_BYTES", "abc")
    with pytest.raises(ValueError, match="nombre"):
        load_settings()
    monkeypatch.setenv("MP_FILE_MAX_BYTES", "2000000")
    monkeypatch.setenv("MP_FILES_QUOTA_BYTES", "1000")
    with pytest.raises(ValueError, match="QUOTA"):
        load_settings()
