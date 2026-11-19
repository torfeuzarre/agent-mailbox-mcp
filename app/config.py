"""Chargement de la configuration des agents (fichier JSON et/ou variables d'environnement).

Format du fichier agents.json :
{
  "agents": {
    "agent-a": {"token_sha256": "<hash>", "webhook": {"url": "...", "secret": "...", "headers": {}, "include_body": false}},
    "agent-b":  {"token_sha256": "<hash>"},
    "agent-c": {"token": "<jeton en clair, déconseillé>"}
  }
}
Variable d'environnement alternative : MP_TOKENS="agent-a:<jeton>,agent-b:<jeton>"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class Webhook:
    url: str
    secret: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    include_body: bool = False


@dataclass
class Agent:
    name: str
    token_sha256: str
    webhook: Webhook | None = None


MIB = 1024 * 1024
FILE_MAX_HARD_CAP = 25 * MIB  # plafond absolu, quoi que dise MP_FILE_MAX_BYTES (les fichiers transitent en base64 dans MCP)


@dataclass
class FileSettings:
    """Transfert de fichiers entre agents (voir README §Fichiers)."""
    enabled: bool = True
    dir: str = "/data/files"
    max_bytes: int = 5 * MIB          # taille max d'un fichier APRÈS décodage base64
    quota_bytes: int = 100 * MIB      # total stocké (tous fichiers non expirés)
    max_files: int = 200              # nombre total de fichiers stockés
    ttl_days: float = 7.0             # expiration automatique
    purge_interval_s: float = 3600.0  # balayage en arrière-plan (en plus du balayage à chaque appel)
    scan_paranoid: bool = False       # refuse aussi la simple mention d'un en-tête de clé privée


@dataclass
class Settings:
    agents: dict[str, Agent]
    db_path: str
    allow_query_token: bool = False
    allowed_hosts: list[str] = field(default_factory=list)
    files: FileSettings = field(default_factory=FileSettings)

    def authenticate(self, token: str | None) -> Agent | None:
        if not token:
            return None
        digest = sha256_hex(token)
        found = None
        for agent in self.agents.values():
            # comparaison à temps constant, on parcourt tout
            if hmac.compare_digest(agent.token_sha256, digest):
                found = agent
        return found


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "oui", "on"}


def _env_num(name: str, default, cast, minimum):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = cast(raw.strip())
    except ValueError:
        raise ValueError(f"{name} : nombre attendu, reçu {raw!r}") from None
    if val < minimum:
        raise ValueError(f"{name} : valeur minimale {minimum}, reçu {val}")
    return val


def load_file_settings(db_path: str) -> FileSettings:
    fs = FileSettings(
        enabled=_env_bool("MP_FILES_ENABLED", True),
        dir=os.environ.get("MP_FILES_DIR") or (str(Path(db_path).parent / "files") if db_path != ":memory:" else "/data/files"),
        max_bytes=_env_num("MP_FILE_MAX_BYTES", 5 * MIB, int, 1),
        quota_bytes=_env_num("MP_FILES_QUOTA_BYTES", 100 * MIB, int, 1),
        max_files=_env_num("MP_FILES_MAX_COUNT", 200, int, 1),
        ttl_days=_env_num("MP_FILE_TTL_DAYS", 7.0, float, 0.00001),
        purge_interval_s=_env_num("MP_FILE_PURGE_INTERVAL", 3600.0, float, 0.05),
        scan_paranoid=_env_bool("MP_FILE_SCAN_PARANOID", False),
    )
    if fs.max_bytes > FILE_MAX_HARD_CAP:
        raise ValueError(f"MP_FILE_MAX_BYTES : plafond absolu {FILE_MAX_HARD_CAP} octets (25 Mo)")
    if fs.quota_bytes < fs.max_bytes:
        raise ValueError("MP_FILES_QUOTA_BYTES doit être >= MP_FILE_MAX_BYTES")
    return fs


def load_settings() -> Settings:
    agents: dict[str, Agent] = {}
    path = os.environ.get("MP_AGENTS_FILE", "/config/agents.json")
    if path and Path(path).is_file():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for name, spec in (data.get("agents") or {}).items():
            name = name.strip().lower()
            if not NAME_RE.match(name):
                raise ValueError(f"Nom d'agent invalide : {name!r}")
            if spec.get("token_sha256"):
                digest = spec["token_sha256"].strip().lower()
            elif spec.get("token"):
                digest = sha256_hex(spec["token"])
            else:
                raise ValueError(f"Agent {name} : 'token_sha256' (ou 'token') requis")
            wh = None
            if spec.get("webhook") and spec["webhook"].get("url"):
                w = spec["webhook"]
                wh = Webhook(
                    url=w["url"],
                    secret=w.get("secret") or None,
                    headers={str(k): str(v) for k, v in (w.get("headers") or {}).items()},
                    include_body=bool(w.get("include_body", False)),
                )
            agents[name] = Agent(name=name, token_sha256=digest, webhook=wh)

    for pair in filter(None, (p.strip() for p in os.environ.get("MP_TOKENS", "").split(","))):
        name, _, token = pair.partition(":")
        name = name.strip().lower()
        if not NAME_RE.match(name) or len(token.strip()) < 16:
            raise ValueError("MP_TOKENS : format attendu 'agent:jeton' (jeton >= 16 caractères)")
        existing = agents.get(name)
        agents[name] = Agent(name=name, token_sha256=sha256_hex(token.strip()),
                             webhook=existing.webhook if existing else None)

    if not agents:
        raise RuntimeError("Aucun agent configuré : renseignez MP_AGENTS_FILE ou MP_TOKENS.")

    hashes = [a.token_sha256 for a in agents.values()]
    if len(set(hashes)) != len(hashes):
        raise ValueError("Deux agents partagent le même jeton : un jeton par agent.")

    hosts = [h.strip() for h in os.environ.get("MP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if hosts:  # accès local (healthcheck, curl depuis le VPS) toujours permis
        hosts += ["127.0.0.1:*", "localhost:*", "memoire:*"]
    db_path = os.environ.get("MP_DB_PATH", "/data/memoire.db")
    return Settings(
        agents=agents,
        db_path=db_path,
        allow_query_token=_env_bool("MP_ALLOW_QUERY_TOKEN"),
        allowed_hosts=hosts,
        files=load_file_settings(db_path),
    )
