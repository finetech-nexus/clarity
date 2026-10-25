# Matrice de compatibilité Marble

Date de constat : 2026-10-25. Révision amont inspectée : `6b55a5f`.

Clarity n'est pas compatible à 100 % avec Marble. Le rapport chiffré est dans `../unified/compatibility-report.json`. Aucune campagne Schemathesis complète n'a été exécutée contre un déploiement Marble officiel.

## Surfaces

| Surface | Source | Décision |
| --- | --- | --- |
| API publique v1 | `original/v1.yml`, OpenAPI 3.1.0, version 1.0.0, serveur `https://api.checkmarble.com/v1` | Routes présentes sous `/v1`. Authentification `Authorization: Bearer` ou `X-API-KEY`, comme le contrat. Les corps d'erreur suivent `{code, messages, detail?}`. |
| Ingestion | `original/ingestion.yml`, sans bloc `info` ni `servers` | Montée sur le même hôte `/v1`, hypothèse documentée : le fichier ne déclare pas d'URL. Chemins `/ingest/{objectType}` conservés. |
| API publique v1beta | `original/v1beta.yml`, serveur `https://api.checkmarble.com/v1beta` | Sous-ensemble dossiers, tags et niveaux de risque sous `/v1beta`. Le reste répond 404 FastAPI, donc non compatible. |
| Anciennes specs | `public_api.yaml` 0.1.0 et `v1_public_api.yaml` | Conservées comme historique. Non servies. Le chemin historique `/ingestion` et `/screening/whitelist` (singulier) ne sont pas des alias. |
| API console | `original/console/marblecore-api.yaml`, 196 chemins en `$ref` | Sous-ensemble de paramétrage et d'exécution servi à la racine : scénarios, itérations, règles AST, publication, modèle de données, listes, scoring, criblage continu synthétique, webhooks, boîtes, tags, utilisateurs, clés, décisions, exécutions planifiées, essais. L'IA, OpenSanctions, l'administration d'organisation et l'analytique ne sont pas servis. |

## Écarts connus sur les routes implémentées

- Les noms de paramètres de chemin dans l'OpenAPI généré par FastAPI sont en snake_case (`decision_id`). Les URL appelées par un client restent `/v1/decisions/{uuid}`.
- `GET /screening/entities/{entityId}` renvoie une fiche du jeu synthétique Clarity, jamais une entité OpenSanctions. Le champ `synthetic` et la note dans `properties.notes` le disent. C'est une compatibilité partielle.
- Le criblage ne confirme jamais une correspondance tout seul. Un score au-dessus du seuil crée un match `pending` et force `block_and_review`, pas `decline`.
- `POST /screening/whitelists` répond 201 sans corps, comme le contrat qui ne définit pas de schéma de réponse.
- L'ingestion rejette les champs absents du modèle de données du tenant. Le modèle de démonstration couvre `transactions`, `customers` et `accounts`.
- Les opérations v1beta de fichiers, revues IA, annotations, criblage continu et décisions asynchrones ne sont pas implémentées.
- Aucune clé Marble de production n'est acceptée. La clé de démonstration est locale.

## Dépendances externes

Le criblage Marble documenté s'appuie sur OpenSanctions. Clarity n'appelle pas ce service. Un connecteur de production reste à brancher et à qualifier. Tant qu'il n'existe pas, aucun résultat de criblage ne doit être traité comme une liste officielle.
