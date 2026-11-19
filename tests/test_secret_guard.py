"""Barrière anti-secrets. Les faux secrets sont construits à l'exécution (concaténation) : le dépôt ne contient
aucune valeur ressemblant à un vrai secret, et l'archive du dépôt elle-même passe le contrôle."""
import base64
import gzip
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from app import secret_guard as sg
from app.secret_guard import SecretRefused, check_filename, check_upload

ROOT = Path(__file__).resolve().parents[1]
BODY = base64.b64encode(bytes(range(256)) * 2).decode()          # faux corps de clé, 64 car. par ligne ci-dessous
BODY_LINES = "\n".join(BODY[i:i + 64] for i in range(0, 200, 64))


def pem(kind="", header_extra=""):
    return ("-----BEGIN " + kind + "PRIVATE KEY-----\n" + header_extra + BODY_LINES + "\n-----END " + kind + "PRIVATE KEY-----\n").encode()


def refused(name, data, **kw):
    with pytest.raises(SecretRefused) as e:
        check_upload(name, data, **kw)
    return str(e.value)


# ---------------- noms ----------------
@pytest.mark.parametrize("name", [
    "AuthKey_ABC123.p8", "cle.P8", "cert.pem", "server.key", ".env", "prod.env", ".env.local", "x.p12", "x.pfx",
    "app.jks", "x.keystore", "putty.ppk", "base.kdbx", "backup.pem.txt", "a/b/../c.p8", "C:\\tmp\\k.p8", "id_rsa",
    "id_ed25519", ".netrc", ".pgpass", "agents.json", "tokens.json", "credentials", "secrets.json", "mes.secrets",
    "AuthKey.p8 ", "x.p8.",
])
def test_blocked_names(name):
    with pytest.raises(SecretRefused):
        check_filename(name)


@pytest.mark.parametrize("name", [
    "exemple-projet-1.0.0.tar.gz", "rapport.pdf", ".env.example", ".env.sample", "agents.example.json", "id_rsa.pub",
    "README.md", "keyboard.txt", "monkey.png", "environment.txt", "envoi.zip", "data.csv",
])
def test_allowed_names(name):
    check_filename(name)


# ---------------- contenu ----------------
@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED "])
def test_pem_blocks_refused(kind):
    msg = refused("notes.txt", b"hello\n" + pem(kind))
    assert "clé privée" in msg and BODY[:30] not in msg


def test_pem_with_proc_type_headers_refused():
    refused("n.txt", pem("RSA ", "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00112233445566778899AABBCCDDEEFF\n\n"))


def test_pem_crlf_and_pgp_refused():
    refused("n.txt", pem("").replace(b"\n", b"\r\n"))
    refused("n.txt", b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: X\n\n" + BODY.encode() + b"\n-----END PGP PRIVATE KEY BLOCK-----")  # gitleaks:allow  # fixture factice pour tester le refus


def test_pem_base64_wrapped_refused():
    blob = base64.b64encode(pem(""))
    refused("n.txt", b"data:" + blob)
    refused("n.txt", b"xx" + base64.b64encode(b"x" + pem("")))       # autre alignement
    refused("n.txt", b"xx" + base64.b64encode(b"xx" + pem("")))


def test_header_mention_alone_allowed_unless_paranoid():
    script = b'if [ "$(head -n 1 "$P8")" != "-----BEGIN PRIVATE KEY-----" ]; then echo bad; fi\n'
    check_upload("setup.sh", script)
    assert "en-tête" in refused("setup.sh", script, paranoid=True)


TOKENS = {
    "github": b"ghp_" + b"A1b2" * 9,
    "github-pat": b"github_pat_" + b"A1b2C3" * 10,
    "slack": b"xoxb-" + b"1234567890-" + b"abcdefghij",
    "sk": b"sk-ant-" + b"abcDEF0123456789" * 2,
    "aws": b"AKIA" + b"ABCDEFGHIJKLMNOP",
    "google": b"AIza" + b"A" * 35,
    "stripe": b"sk_" + b"live_" + b"abcdefghijklmnop1234",
    "npm": b"npm_" + b"a1" * 18,
    "telegram": b"123456789:AA" + b"a1B2c3" * 5 + b"abc",
    "mp": b"mp_" + b"aB3-" * 10,
    "jwt": b"eyJ" + b"hbGciOiJFUzI1NiJ9" + b".eyJ" + b"pc3MiOiJhYmNkZWYifQ" + b"." + b"c2lnbmF0dXJlYWJj",
    "auth-header": b"Authorization: Bearer " + b"abcDEF123456" * 3,
    "url-creds": b"https://user:" + b"pa55w0rdXYZ" + b"@example.org/x",
    "assign": b'API_KEY = "' + b"k3y" * 10 + b'"',
    "assign2": b"password: " + b"Zz9" * 10,
}


@pytest.mark.parametrize("label", sorted(TOKENS))
def test_token_patterns_refused(label):
    msg = refused("notes.txt", b"intro\n" + TOKENS[label] + b"\nfin\n")
    assert TOKENS[label].decode() not in msg


@pytest.mark.parametrize("text", [
    b"token: <TOKEN>", b"api_key = os.environ['ASC_KEY']", b"password: changeme", b"secret = hunter2",
    b"sha256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    b"Authorization: Bearer <jeton>", b"https://example.org/path", b"https://user@example.org",
    "Bonjour, voici le rapport d'activité de RemplApp — téléchargements et avis.".encode(),
    bytes(range(256)) * 50,
])
def test_benign_content_allowed(text):
    check_upload("rapport.txt", text)


# ---------------- archives ----------------
def make_tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n, d in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            tf.addfile(ti, io.BytesIO(d))
    return buf.getvalue()


def make_zip(files: dict[str, bytes], flag=0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in files.items():
            zf.writestr(zipfile.ZipInfo(n), d)
    data = bytearray(buf.getvalue())
    if flag:  # marque les membres « chiffrés » en patchant les en-têtes local (+6) et central (+8)
        for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            i = data.find(sig)
            while i != -1:
                data[i + off] |= flag
                i = data.find(sig, i + 4)
    return bytes(data)


def test_tar_gz_with_secret_member_refused_naming_member_only():
    data = make_tar_gz({"proj/readme.md": b"ok", "proj/conf/k.txt": b"x\n" + pem("")})
    msg = refused("proj.tar.gz", data)
    assert "proj/conf/k.txt" in msg and "clé privée" in msg and BODY[:30] not in msg


def test_tar_gz_with_blocked_member_name_refused():
    assert "extension .p8" in refused("p.tgz", make_tar_gz({"a/AuthKey_X.p8": b"vide"}))
    assert ".env" in refused("p.tgz", make_tar_gz({"a/.env": b"A=1"}))


def test_zip_with_secret_refused_and_renamed_zip_still_inspected():
    data = make_zip({"a.txt": b"ok", "d/b.txt": TOKENS["github"]})
    assert "d/b.txt" in refused("vacances.png", data)       # extension trompeuse : on se fie aux octets
    refused("x.bin", make_zip({"k.p8": b"x"}))


def test_nested_archive_refused():
    inner = make_zip({"deep.txt": pem("")})
    outer = make_tar_gz({"inner.zip": inner})
    assert "deep.txt" in refused("o.tar.gz", outer)


def test_too_deep_nesting_refused():
    data = b"texte"
    for _ in range(5):
        data = gzip.compress(data)
    assert "imbriquées" in refused("n.gz", data)


def test_encrypted_zip_member_refused():
    assert "chiffré" in refused("e.zip", make_zip({"a.txt": b"x"}, flag=0x1))


def test_corrupt_and_truncated_archives_refused():
    good = make_tar_gz({"a": b"x" * 5000})
    refused("a.tgz", good[:len(good) // 2])
    refused("a.gz", b"\x1f\x8b" + b"garbage" * 20)
    refused("a.zip", b"PK\x03\x04" + b"garbage" * 20)


def test_decompression_bomb_refused(monkeypatch):
    monkeypatch.setattr(sg, "MAX_MEMBER_BYTES", 1024 * 1024)
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
    assert len(bomb) < 20000
    assert "trop volumineux" in refused("b.gz", bomb)
    z = make_zip({"z.bin": b"\0" * (8 * 1024 * 1024)})
    assert "trop volumineux" in refused("b.zip", z)


def test_total_budget_and_member_count(monkeypatch):
    monkeypatch.setattr(sg, "MAX_MEMBERS", 10)
    refused("many.zip", make_zip({f"f{i}.txt": b"a" for i in range(11)}))
    monkeypatch.setattr(sg, "MAX_MEMBERS", 5000)
    monkeypatch.setattr(sg, "MAX_INSPECT_BYTES", 100_000)
    refused("big.zip", make_zip({f"f{i}.bin": bytes([i]) * 40_000 for i in range(5)}))


def test_clean_archives_pass():
    check_upload("ok.tar.gz", make_tar_gz({"a/readme.md": b"Bonjour", "a/.env.example": b"TOKEN=\n"}))
    check_upload("ok.zip", make_zip({"a.txt": b"Bonjour"}))
    check_upload("ok.tgz", gzip.compress(b"juste du texte"))


def test_this_repository_archive_passes():
    """Le dépôt (tests compris) ne contient aucun faux secret littéral : son archive doit passer."""
    skip = {"__pycache__", ".pytest_cache", ".git", ".venv", "data", "backups", "tokens"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(ROOT.rglob("*")):
            if p.is_file() and not (skip & set(p.relative_to(ROOT).parts)) and p.name not in ("agents.json", ".env"):
                tf.add(p, arcname="memoire/" + str(p.relative_to(ROOT)))
    check_upload("memoire-partagee.tar.gz", buf.getvalue())
