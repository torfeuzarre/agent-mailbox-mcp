# Changelog

## 1.1.0 — transfert de fichiers entre agents
Ajouté
- Outils MCP `put_file`, `get_file`, `list_files`, `delete_file`.
  - Taille max configurable (5 Mo par défaut après décodage, plafond absolu 25 Mo), quota total (100 Mo), nombre max de fichiers (200).
  - SHA-256 calculé au dépôt, revérifié (avec la taille) et renvoyé à chaque récupération ; contenu modifié sur le disque = refus + journal.
  - Seul le destinataire récupère le contenu (même identification par jeton que le reste du serveur) ; l'expéditeur peut retirer un envoi.
  - Expiration automatique (7 jours, configurable) avec purge à chaque appel et en tâche de fond ; balayage des orphelins.
  - Stockage sur disque `data/files/<id>.bin` (0700 / 0600), noms assainis et jamais utilisés comme chemin.
  - Notification : message dans la boîte du destinataire + webhook, même mécanisme et mêmes signatures que `send_message` (le payload gagne `file_id`, `file_name`, `file_size`, `file_sha256`, jamais le contenu).
  - Journal sans contenu ni nom : `audit` (id, expéditeur, destinataire, taille, SHA-256, action) et logs `memoire.files`.
- Barrière anti-secrets (`app/secret_guard.py`) : extensions et noms de fichiers sensibles, blocs de clés privées, formats de jetons courants, inspection en mémoire des `.tar/.tar.gz/.gz/.bz2/.xz/.zip` (imbriqués, plafonds anti-bombe). Barrière de précaution, pas une garantie.
- Limite de taille des requêtes HTTP (413) alignée sur la taille max d'un fichier encodé ; alignement du plafond propre au SDK MCP quand il existe (1 Mio par défaut dans le SDK 1.30).
- Variables `MP_FILES_ENABLED`, `MP_FILES_DIR`, `MP_FILE_MAX_BYTES`, `MP_FILES_QUOTA_BYTES`, `MP_FILES_MAX_COUNT`, `MP_FILE_TTL_DAYS`, `MP_FILE_PURGE_INTERVAL`, `MP_FILE_SCAN_PARANOID`.
- `docs/DEPLOIEMENT_VPS.md` (déploiement, sauvegarde et retour arrière), `INSTALL_AGENT.md` (installation autonome par un agent IA), licence MIT.
- 135 tests : les 9 d'origine (inchangés) + 126 nouveaux (barrière anti-secrets 85, stockage 30, bout en bout 11).

Inchangé
- Les 10 outils existants, leurs schémas, l'authentification, le webhook, la protection du Host, le schéma des tables existantes.
- Migration de la base : une seule table ajoutée (`files`), `CREATE TABLE IF NOT EXISTS`, rien n'est modifié ni supprimé. Revenir à la 1.0.0 ne demande pas de restaurer la base.

## 1.0.0
Version initiale : mémoire partagée (FTS5), boîte aux lettres inter-agents, webhooks signés, jetons Bearer par agent.
