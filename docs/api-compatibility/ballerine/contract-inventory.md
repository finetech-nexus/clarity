# Inventaire Ballerine

## Spécification officielle

Ballerine ne publie pas de fichier OpenAPI statique dans le dépôt `ballerine-io/ballerine` à la révision inspectée `4303cd3e0` (`hotfix-workflow-service@v0.6.733-1-g4303cd3e0`).

Le document est généré au démarrage par NestJS Swagger :

- Titre : Ballerine Backend API
- Version annoncée dans le code : `1.3.10` (`services/workflows-service/src/swagger/swagger.ts`)
- OpenAPI : 3.1.0
- Préfixe global : `/api`
- Version d'URI par défaut : `1`
- Authentification documentée : Bearer
- Documentation externe citée par le code : `https://docs.ballerine.com`

Clarity ne fabrique pas de fichier `original-openapi.yaml`. Inventaire établi par lecture des contrôleurs publics, pas par un export Swagger exécuté.

Licence du dépôt : mixte, Elastic License 2.0 par défaut, portions MIT. Clarity ne copie pas ce code.

## Routes lues sur les contrôleurs externes

Préfixe réel : `/api/v1`.

| Méthode | Chemin | Statut Clarity |
| --- | --- | --- |
| POST | `/external/workflows/run` | Implémenté et testé. Réponse `workflowDefinitionId`, `workflowRuntimeId`, `ballerineEntityId`, `entity`. |
| GET | `/external/workflows` | Implémenté et testé. Forme `{results, meta:{pages,total}}`. |
| GET | `/external/workflows/{id}` | Implémenté. |
| GET | `/external/workflows/{id}/context` | Implémenté. Le contexte déposé est restitué. |
| POST | `/external/workflows/{id}/event` | Implémenté. Corps `{name, payload?}`. |
| POST | `/external/workflows/{id}/send-event` | Même traitement que `/event`. |
| POST | `/external/workflows/{id}/hook/{event}` | Implémenté et testé. Signature `x-hmac-signature`, HMAC-SHA256 du JSON re-sérialisé, comme `verify-signature.ts`. |
| POST | `/external/documents` | Implémenté. Multipart, fichier obligatoire, `metadata` et `properties` en JSON texte. Réponse tableau. |
| PATCH | `/external/documents/{documentId}/decision` | Implémenté. `approve`, `reject`, `revision` ou `null`, mappés vers `approved`, `rejected`, `revisions`. |
| GET | `/external/documents/tracker/{workflowId}` | Implémenté. Forme `{business, individuals:{ubos, directors}}`. |
| POST | `/external/transactions` | Implémenté. 201 `{id, correlationId}`. |
| POST | `/external/transactions/alt` | Implémenté. Enveloppe `{data:{...}}`. |
| GET | `/external/transactions` | Partiel. Liste `{results, meta}`. Le schéma amont complet n'a pas été rejoué. |
| POST | `/external/kyc` | Dépendance Veriff. 501. Aucune session ni e-mail n'est créé. Testé. |

## Non pris en charge

Workflow definition, plugins, collection flow, case management interne, analytics, stockage `external/storage`, transactions bulk, `by-alert`, documents batch decision, authentification session `internal/auth`, et le modèle multi-projets porté par le jeton Ballerine. Une clé Clarity est liée à un seul tenant, qui tient lieu de projet.

## Webhooks sortants

Les événements documentés (`workflow.completed`, `workflow.state.changed`, etc.) sont décrits dans le code Swagger. Clarity conserve le contexte et vérifie la signature entrante du hook. L'émission signée vers l'URL d'abonnement du client n'est pas encore livrée.
