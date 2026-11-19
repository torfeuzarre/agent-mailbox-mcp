#!/usr/bin/env python3
"""Génère un jeton pour un agent et prépare l'entrée à mettre dans agents.json.

Usage (humain, au terminal) :
  python3 scripts/gen_token.py agent-a
      Affiche le jeton une seule fois, plus l'entrée JSON (hash seulement).

Usage (agent d'installation, suivez INSTALL_AGENT.md) :
  python3 scripts/gen_token.py agent-a --token-file tokens/agent-a.token \\
      --agents-file config/agents.json
      Écrit le jeton dans un fichier en mode 0600, ne l'affiche PAS.
      Fusionne le hash dans agents.json (crée le fichier s'il n'existe pas).
      N'imprime que le nom de l'agent et le chemin du fichier jeton.

Le jeton en clair ne doit jamais être commité, collé dans un chat, ni journalisé.
Donnez le fichier (ou le jeton) à l'humain propriétaire, hors conversation.
Côté serveur, ne conservez que le hash SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère un jeton d'agent pour agent-mailbox-mcp.")
    parser.add_argument("name", nargs="?", default="agent", help="Nom de l'agent (ex. agent-a)")
    parser.add_argument("--token-file", help="Écrire le jeton ici (mode 0600) sans l'afficher")
    parser.add_argument("--agents-file", help="Fusionner le hash dans ce agents.json")
    args = parser.parse_args()

    name = args.name.strip().lower()
    if not NAME_RE.match(name):
        print("Nom d'agent invalide : lettres minuscules, chiffres, _ ou - ; 1 à 32 caractères.", file=sys.stderr)
        return 2

    token = "mp_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    entry = {name: {"token_sha256": digest}}

    if args.token_file:
        path = Path(args.token_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
        print(f"Jeton pour {name} écrit dans {path} (mode 0600). Ne l'affichez pas, ne le commitez pas.")
        print("Donnez ce fichier à l'humain propriétaire, hors conversation.")
    else:
        print(f"Jeton pour {name} (à donner à l'humain, affiché une seule fois) :\n  {token}\n")
        print("Entrée pour config/agents.json :")
        print(json.dumps(entry, indent=2))

    if args.agents_file:
        agents_path = Path(args.agents_file)
        if agents_path.is_file():
            data = json.loads(agents_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                print(f"{agents_path} : JSON objet attendu", file=sys.stderr)
                return 2
            agents = data.setdefault("agents", {})
        else:
            data = {"agents": {}}
            agents = data["agents"]
        existing = agents.get(name) or {}
        existing["token_sha256"] = digest
        existing.pop("token", None)  # ne jamais laisser un jeton en clair
        agents[name] = existing
        tmp = agents_path.with_suffix(agents_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(agents_path)
        os.chmod(agents_path, 0o600)
        print(f"Hash fusionné dans {agents_path} (mode 0600).")
    elif args.token_file:
        print("Entrée pour config/agents.json :")
        print(json.dumps(entry, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
