"""Barrière de précaution contre le transit de secrets (clés privées, jetons) dans les fichiers.

⚠️ Ce n'est PAS une garantie : un contenu chiffré, encodé ou obfusqué passera. Le garde-fou réel,
c'est la consigne donnée aux agents et à l'humain propriétaire : ne jamais envoyer de secret par ce canal.

Ce module ne journalise ni ne renvoie JAMAIS le contenu suspect : le motif de refus ne nomme que
la catégorie (ex. « bloc de clé privée ») et, pour une archive, le nom du membre concerné.
Aucune archive n'est jamais extraite sur le disque : on lit les membres en mémoire, avec des plafonds.
"""
from __future__ import annotations

import bz2
import io
import lzma
import re
import tarfile
import zipfile
import zlib
from dataclasses import dataclass

# --- noms de fichiers -------------------------------------------------------------------------
# Extensions refusées (n'importe quel segment après le premier point : « sauvegarde.pem.txt » aussi).
BLOCKED_EXTENSIONS = frozenset({
    "p8", "pem", "key", "env", "p12", "pfx", "jks", "keystore", "ppk", "kdbx", "pkcs8", "pkcs12",
    "secret", "secrets", "token", "tokens",
})
# Noms entiers (minuscules) refusés.
BLOCKED_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", "_netrc", ".npmrc", ".pgpass", ".htpasswd",
    "credentials", "credentials.json", "secrets.json", "tokens.json", "agents.json", "config.json.local",
})
# « .env.example » & co sont des modèles sans valeur : autorisés.
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

def _b64_needles(lit: bytes) -> list[bytes]:
    """Sous-chaînes base64 d'un littéral, pour chacun des 3 alignements possibles dans un flux encodé."""
    import base64
    out = []
    for o in range(3):
        enc = base64.b64encode(b"\x00" * o + lit)
        g0 = 0 if o == 0 else 1
        g1 = (o + len(lit)) // 3
        out.append(enc[4 * g0: 4 * g1])
    return out


# --- contenu ----------------------------------------------------------------------------------
_B64 = rb"[A-Za-z0-9+/=\r\n \t]"
PATTERNS: list[tuple[str, re.Pattern]] = [
    # Bloc PEM de clé privée : l'en-tête SUIVI d'un corps base64. Une simple mention de l'en-tête dans un
    # script ou une doc (ex. `if head -1 key.p8 | grep BEGIN PRIVATE KEY`) n'est pas un secret.
    ("bloc de clé privée (PEM)", re.compile(
        rb"-----BEGIN [A-Z0-9 ]{0,30}PRIVATE KEY(?: BLOCK)?-----[ \t]*\r?\n"
        rb"(?:[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n|[ \t]+[^\r\n]*\r?\n|[ \t]*\r?\n)*"
        + _B64 + rb"{40,}")),
    # « PRIVATE KEY----- » encodé une fois en base64 (3 alignements possibles) : voir _b64_needles()
    ("bloc de clé privée encodé en base64", re.compile(b"|".join(re.escape(n) for n in _b64_needles(b"PRIVATE KEY-----")))),
    ("clé OpenSSH privée", re.compile(rb"openssh-key-v1\x00")),
    ("jeton GitHub", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b")),
    ("jeton Slack", re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("clé API de type sk-… (OpenAI/Anthropic/…)", re.compile(rb"\bsk-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{24,}")),
    ("clé Stripe live", re.compile(rb"\b[sr]k_live_[A-Za-z0-9]{16,}")),
    ("clé d'accès AWS", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("clé API Google", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jeton npm", re.compile(rb"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jeton de bot Telegram", re.compile(rb"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b")),
    ("jeton de cette mémoire partagée (mp_…)", re.compile(rb"\bmp_[A-Za-z0-9_-]{32,}")),
    ("jeton JWT", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("en-tête Authorization avec jeton", re.compile(rb"(?i)\bauthorization\s*[:=]\s*[\"']?(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("URL avec identifiants intégrés", re.compile(rb"(?i)\b[a-z][a-z0-9+.-]{1,15}://[^\s/:@\"']{1,64}:[^\s/@\"']{6,64}@[A-Za-z0-9.-]+")),
    ("affectation secret = valeur longue", re.compile(
        rb"(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|passw(?:or)?d|access[_-]?token|auth[_-]?token|token)"
        rb"[\"']?\s*[:=]\s*[\"']?(?=[A-Za-z0-9/+_=.-]*[A-Za-z])(?=[A-Za-z0-9/+_=.-]*\d)[A-Za-z0-9/+_=.-]{24,}")),
]
# Mode « paranoïaque » (MP_FILE_SCAN_PARANOID=1) : refuse aussi la simple mention d'un en-tête de clé privée.
PARANOID_PATTERN = ("mention d'un en-tête de clé privée", re.compile(rb"-----BEGIN [A-Z0-9 ]{0,30}PRIVATE KEY"))

# --- plafonds d'inspection --------------------------------------------------------------------
MAX_INSPECT_BYTES = 64 * 1024 * 1024   # total décompressé lu pour un dépôt
MAX_MEMBER_BYTES = 32 * 1024 * 1024    # par membre d'archive
MAX_MEMBERS = 5000
MAX_DEPTH = 3                           # archives imbriquées


class SecretRefused(ValueError):
    """Dépôt refusé par la barrière anti-secrets (le message ne contient jamais le secret)."""


@dataclass
class _Budget:
    remaining: int = MAX_INSPECT_BYTES
    members: int = 0

    def take(self, n: int, what: str) -> None:
        self.remaining -= n
        if self.remaining < 0:
            raise SecretRefused(f"{what} : trop volumineux une fois décompressé pour être inspecté "
                                f"(plafond {MAX_INSPECT_BYTES // (1024 * 1024)} Mo) ; refusé par précaution")

    def member(self, what: str) -> None:
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise SecretRefused(f"{what} : plus de {MAX_MEMBERS} fichiers dans l'archive ; refusé par précaution")


def check_filename(name: str) -> None:
    """Refuse les noms/extensions de fichiers de secrets. `name` = nom ou chemin interne d'archive."""
    base = re.split(r"[\\/]", name)[-1].strip().lower()
    if not base:
        return
    template = base.endswith(_ENV_TEMPLATE_SUFFIXES)
    if template:  # « .env.example » : modèle sans valeur ; on contrôle le reste du nom normalement
        base = base[: base.rfind(".")]
    if base in BLOCKED_NAMES:
        raise SecretRefused(f"nom de fichier refusé ({base}) : ce canal ne doit pas transporter de secrets")
    if base.startswith("id_rsa") or base.startswith("id_ed25519") or base.startswith("id_ecdsa"):
        if not base.endswith(".pub"):
            raise SecretRefused("clé SSH privée refusée : ce canal ne doit pas transporter de secrets")
    parts = base.split(".")[1:] if "." in base else []
    for ext in parts:
        if ext in BLOCKED_EXTENSIONS and not (template and ext == "env"):
            raise SecretRefused(f"extension .{ext} refusée : ce canal ne doit pas transporter de secrets")
    if base.startswith(".env") and not template:
        raise SecretRefused("fichier .env refusé : ce canal ne doit pas transporter de secrets")


def _scan_text(label: str, data: bytes, paranoid: bool) -> None:
    pats = PATTERNS + ([PARANOID_PATTERN] if paranoid else [])
    for what, rx in pats:
        if rx.search(data):
            raise SecretRefused(f"{label} : contenu refusé ({what}) ; ce canal ne doit pas transporter de secrets")


def _read_limited(fobj, limit: int, label: str) -> bytes:
    data = fobj.read(limit + 1)
    if len(data) > limit:
        raise SecretRefused(f"{label} : membre trop volumineux pour être inspecté (> {limit // (1024 * 1024)} Mo) ; refusé par précaution")
    return data


def _decompress(kind: str, data: bytes, label: str, budget: _Budget) -> bytes:
    limit = min(MAX_MEMBER_BYTES, max(budget.remaining, 0)) + 1
    too_big = SecretRefused(f"{label} : trop volumineux une fois décompressé pour être inspecté ; refusé par précaution")
    try:
        if kind == "gzip":
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif kind == "bzip2":
            d = bz2.BZ2Decompressor()
        else:
            d = lzma.LZMADecompressor()
        out = d.decompress(data, limit)
        if len(out) >= limit:
            raise too_big
        if not d.eof:  # flux tronqué
            raise ValueError("flux tronqué")
    except SecretRefused:
        raise
    except Exception as exc:  # flux corrompu / tronqué
        raise SecretRefused(f"{label} : archive illisible ({type(exc).__name__}), impossible à inspecter ; refusé par précaution")
    budget.take(len(out), label)
    return out


def _kind(data: bytes) -> str | None:
    if data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06":
        return "zip"
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:3] == b"BZh":
        return "bzip2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    return None


def _scan(label: str, data: bytes, depth: int, budget: _Budget, paranoid: bool) -> None:
    kind = _kind(data)
    if kind is None:
        _scan_text(label, data, paranoid)
        return
    if depth >= MAX_DEPTH:
        raise SecretRefused(f"{label} : archives imbriquées sur plus de {MAX_DEPTH} niveaux ; refusé par précaution")
    if kind in ("gzip", "bzip2", "xz"):
        inner = _decompress(kind, data, label, budget)
        _scan(label, inner, depth + 1, budget, paranoid)
    elif kind == "tar":
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
                for m in tf:
                    budget.member(label)
                    inner_label = f"{label}:{m.name}"
                    check_filename(m.name)
                    if not m.isreg():
                        continue
                    if m.size > MAX_MEMBER_BYTES:
                        raise SecretRefused(f"{inner_label} : membre trop volumineux pour être inspecté ; refusé par précaution")
                    f = tf.extractfile(m)
                    content = _read_limited(f, MAX_MEMBER_BYTES, inner_label) if f else b""
                    budget.take(len(content), inner_label)
                    _scan(inner_label, content, depth + 1, budget, paranoid)
        except SecretRefused:
            raise
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise SecretRefused(f"{label} : archive tar illisible ({type(exc).__name__}) ; refusé par précaution")
    elif kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    budget.member(label)
                    inner_label = f"{label}:{info.filename}"
                    check_filename(info.filename)
                    if info.is_dir():
                        continue
                    if info.flag_bits & 0x1:
                        raise SecretRefused(f"{inner_label} : membre chiffré, impossible à inspecter ; refusé par précaution")
                    with zf.open(info) as f:
                        content = _read_limited(f, MAX_MEMBER_BYTES, inner_label)
                    budget.take(len(content), inner_label)
                    _scan(inner_label, content, depth + 1, budget, paranoid)
        except SecretRefused:
            raise
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, zlib.error) as exc:
            raise SecretRefused(f"{label} : archive zip illisible ({type(exc).__name__}) ; refusé par précaution")
        _scan_text(label, data, paranoid)  # commentaires, membres stockés, octets hors membres


def check_upload(name: str, data: bytes, *, paranoid: bool = False) -> None:
    """Lève SecretRefused si le nom ou le contenu (archives comprises) ressemble à un secret."""
    check_filename(name)
    _scan(name or "fichier", data, 0, _Budget(MAX_INSPECT_BYTES), paranoid)
