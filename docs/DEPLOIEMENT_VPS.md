# Déploiement et exploitation sur un VPS Linux

Ce document s'adresse à un **humain** (ou à un agent qui l'assiste, avec accord explicite à chaque étape irréversible). Pour une installation autonome menée par un agent IA, suis plutôt `INSTALL_AGENT.md` dans l'ordre, et lis `SECURITY.md` avant de commencer.

**Règles**
- N'installe, ne démarre, ne expose et ne supprime rien sans l'accord explicite de l'humain propriétaire.
- Aucun secret (jeton d'agent, `CLOUDFLARE_TUNNEL_TOKEN`, secret de webhook, contenu de `.env`) n'est copié, affiché ni collé dans un chat. Les jetons vont dans des fichiers en mode `0600` (`scripts/gen_token.py --token-file …`).
- **Aucun port public** : le `docker-compose.yml` fourni publie uniquement `127.0.0.1:8765`. Ne le modifie pas pour écouter sur `0.0.0.0`.
- Interrupteur de secours à tout moment : `MP_FILES_ENABLED=0` dans `.env` puis `docker compose up -d memoire` (les 4 outils fichiers disparaissent ; le reste continue).

Les commandes supposent l'installation dans `/opt/agent-mailbox-mcp`. À adapter si le chemin diffère.

---

## Première installation

1. Vérifier l'intégrité de l'archive ou du dossier : `sha256sum -c SHA256SUMS` (tout doit afficher `OK`).
2. Faire relire l'arbre (surtout `app/`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`) par un agent ou un humain, en s'appuyant sur `SECURITY.md`.
3. Suivre `INSTALL_AGENT.md` (prérequis Docker, `.env`, jetons, `agents.json`, Cloudflare Tunnel ou Tailscale, `docker compose --profile cloudflared up -d --build`, tests de fumée).
4. Planifier la sauvegarde : `crontab` de `scripts/backup.sh` (voir ci-dessous).

---

## Sauvegarde

Les sauvegardes contiennent la base SQLite. Elles ne contiennent **pas** `data/files/` (fichiers éphémères). `.env` et `config/agents.json` ne sont pas inclus par `backup.sh` : copie-les à part, dans un répertoire `700`, et ne les sors pas du VPS sans chiffrement.

```bash
cd /opt/agent-mailbox-mcp
./scripts/backup.sh                                 # backups/memoire-AAAAMMJJ-HHMMSS.db.gz, rotation 30 jours
# cron conseillé :
# 15 3 * * * /opt/agent-mailbox-mcp/scripts/backup.sh
```

**Restaurer la base** (seulement si elle est abîmée) :

```bash
cd /opt/agent-mailbox-mcp
docker compose stop memoire
# décompresser la sauvegarde vers data/memoire.db
gunzip -c backups/memoire-AAAAMMJJ-HHMMSS.db.gz > data/memoire.db
rm -f data/memoire.db-wal data/memoire.db-shm
docker compose start memoire
curl -sS http://127.0.0.1:8765/health               # {"status":"ok"}
```

---

## Mise à jour du code (même majeur, ex. 1.0.0 → 1.1.0)

1. Sauvegarde complète avant toute copie : `./scripts/backup.sh`, plus une copie de l'arbre (hors `data/` si tu veux) dans un répertoire `700`.
2. Vérifier `sha256sum -c SHA256SUMS` sur la **nouvelle** arborescence.
3. Copier les fichiers nouveaux en préservant `.env`, `config/agents.json`, `data/` et `backups/` :

```bash
# NEW = dossier source de la nouvelle version (déjà extraite, jamais lancée depuis ici)
# OLD = /opt/agent-mailbox-mcp
rsync -a --exclude data --exclude backups --exclude .env --exclude config/agents.json \
  --exclude tokens --exclude __pycache__ --exclude .pytest_cache --exclude .venv \
  "$NEW/" "$OLD/"
cd "$OLD" && sha256sum -c SHA256SUMS
docker compose build memoire                        # construit, ne redémarre rien
curl -sS http://127.0.0.1:8765/health               # l'ancien processus répond toujours
docker compose up -d memoire                        # courte coupure
sleep 5
docker compose logs --tail 30 memoire               # pas de Traceback
curl -sS http://127.0.0.1:8765/health
```

4. Tests de fumée : `python3 scripts/smoke_test.py --base http://127.0.0.1:8765 --token-file-a tokens/agent-a.token --token-file-b tokens/agent-b.token`, plus un `list_agents` / `recall` depuis un client déjà branché.

La 1.1.0 **ajoute** une table `files` et n'altère pas les tables existantes. Revenir à une 1.0.0 ne demande pas de restaurer la base (la table `files` sera simplement ignorée).

---

## Retour arrière

```bash
cd /opt/agent-mailbox-mcp
docker compose stop memoire
# restaurer l'arbre de code depuis la copie prise avant la mise à jour,
# en excluant data/, backups/, .env, config/agents.json, tokens/
docker compose build memoire && docker compose up -d memoire
sleep 5 && curl -sS http://127.0.0.1:8765/health
# optionnel : effacer les fichiers transférés (éphémères)
# rm -rf data/files
```

---

## Exploitation courante

- **Logs** : `docker compose logs -f --tail 100 memoire` (id, expéditeur, destinataire, taille, SHA-256 pour les fichiers ; jamais le contenu ni le nom).
- **Voir les fichiers stockés** (métadonnées seulement) :
  `docker compose exec -T memoire python -c "import sqlite3;[print(r) for r in sqlite3.connect('/data/memoire.db').execute('SELECT id,from_agent,to_agent,size,sha256,created_at,expires_at FROM files')]"`
- **Fichier envoyé par erreur** : l'expéditeur ou le destinataire appelle `delete_file(id)`. En urgence : `docker compose exec memoire rm /data/files/<id>.bin` (la ligne est nettoyée à la purge suivante).
- **Un secret a transité malgré la barrière** : le considérer comme compromis (le révoquer chez son émetteur), `delete_file`, puis prévenir l'humain propriétaire. La barrière n'est qu'une précaution.
- **Limites fichiers** : modifier `.env` puis `docker compose up -d memoire`.
- **Changer un jeton** : `python3 scripts/gen_token.py <agent> --token-file tokens/<agent>.token --agents-file config/agents.json`, puis `docker compose restart memoire`, et mettre à jour le client (hors chat).
- **Désinstallation** : voir `INSTALL_AGENT.md`. Demander l'accord de l'humain avant `docker compose down -v` ou la suppression de `/opt/agent-mailbox-mcp`.
