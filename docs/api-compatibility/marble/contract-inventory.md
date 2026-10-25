# Inventaire des contrats Marble

Sources copiées sans modification le 2026-10-25 depuis le clone local `checkmarble/marble` (remote origin), révision `6b55a5f` (`v1.11.0-10-g6b55a5f`).

Licence du logiciel amont : Elastic License 2.0. Les fichiers YAML sont des références de contrat, pas le code applicatif de Clarity.

## `v1.yml`

- Titre : Marble decision engine API
- Version du document : 1.0.0
- OpenAPI : 3.1.0
- Serveurs : https://api.checkmarble.com/v1
- Sécurité : BearerTokenAuth, ApiKeyAuth

| Méthode | Chemin | operationId | Statut Clarity |
| --- | --- | --- | --- |
| GET | `/decisions` | `listDecision` | implémenté, couvert par un test automatisé |
| POST | `/decisions` | `createDecision` | implémenté, couvert par un test automatisé |
| POST | `/decisions/all` | `createAllDecisions` | implémenté, pas encore couvert par un test dédié |
| GET | `/decisions/{decisionId}` | `getDecision` | implémenté, couvert par un test automatisé |
| POST | `/decisions/{decisionId}/snooze` | `snoozeRule` | implémenté, pas encore couvert par un test dédié |
| GET | `/decisions/{decisionId}/screenings` | `getScreeningForDecision` | implémenté, pas encore couvert par un test dédié |
| GET | `/screening/entities/{entityId}` | `getScreeningEntity` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/{screeningId}/refine` | `refineScreening` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/{screeningId}/search` | `searchScreening` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/search` | `searchScreeningFreeformCheck` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/matches/{matchId}` | `reviewScreeningMatch` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/whitelists` | `addScreeningWhitelist` | implémenté, couvert par un test automatisé |
| DELETE | `/screening/whitelists` | `deleteScreeningWhitelist` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/whitelists/search` | `searchScreeningWhitelist` | implémenté, couvert par un test automatisé |
| GET | `/batch-executions` | `listBatchExecutions` | implémenté, pas encore couvert par un test dédié |

## `v1beta.yml`

- Titre : Marble decision engine API
- Version du document : 1.0.0
- OpenAPI : 3.1.0
- Serveurs : https://api.checkmarble.com/v1beta
- Sécurité : BearerTokenAuth, ApiKeyAuth

| Méthode | Chemin | operationId | Statut Clarity |
| --- | --- | --- | --- |
| GET | `/ingest/{objectType}/uploads` | `listBatchIngestionLogs` | non pris en charge dans cette version |
| PUT | `/ingest/{objectType}/uploads` | `asyncIngestCsv` | non pris en charge dans cette version |
| POST | `/decisions/async` | `createAsyncDecisionBatch` | non pris en charge dans cette version |
| GET | `/decisions/async/{executionId}` | `getAsyncDecisionExecution` | non pris en charge dans cette version |
| GET | `/cases` | `listCases` | implémenté, pas encore couvert par un test dédié |
| POST | `/cases` | `createCase` | implémenté, couvert par un test automatisé |
| GET | `/cases/{caseId}` | `getCase` | implémenté, pas encore couvert par un test dédié |
| PATCH | `/cases/{caseId}` | `updateCase` | implémenté, pas encore couvert par un test dédié |
| POST | `/cases/{caseId}/close` | `closeCase` | implémenté, pas encore couvert par un test dédié |
| POST | `/cases/{caseId}/reopen` | `reopenCase` | implémenté, pas encore couvert par un test dédié |
| POST | `/cases/{caseId}/escalate` | `escalateCase` | implémenté, pas encore couvert par un test dédié |
| GET | `/cases/{caseId}/comments` | `listCaseComments` | implémenté, pas encore couvert par un test dédié |
| POST | `/cases/{caseId}/comments` | `createCaseComment` | implémenté, couvert par un test automatisé |
| GET | `/cases/{caseId}/files` | `listCaseFiles` | non pris en charge dans cette version |
| POST | `/cases/{caseId}/files` | `uploadCaseFile` | non pris en charge dans cette version |
| GET | `/cases/{caseId}/files/{fileId}/download` | `downloadCaseFile` | non pris en charge dans cette version |
| GET | `/cases/{caseId}/ai_reviews` | `listAiCaseReviews` | non pris en charge dans cette version |
| POST | `/cases/{caseId}/ai_reviews` | `enqueueCaseReview` | non pris en charge dans cette version |
| GET | `/cases/{caseId}/ai_reviews/{aiReviewId}` | `getAiCaseReview` | non pris en charge dans cette version |
| GET | `/tags` | `listTags` | implémenté, pas encore couvert par un test dédié |
| POST | `/cases/{caseId}/tags` | `addCaseTags` | non pris en charge dans cette version |
| DELETE | `/cases/{caseId}/tags/{tagId}` | `removeCaseTag` | non pris en charge dans cette version |
| POST | `/continuous-screenings/objects` | `addContinuousScreeningObject` | non pris en charge dans cette version |
| DELETE | `/continuous-screenings/objects` | `removeContinuousScreeningObject` | non pris en charge dans cette version |
| GET | `/records/{recordType}/{recordId}/annotations` | `listRecordAnnotations` | non pris en charge dans cette version |
| POST | `/records/{recordType}/{recordId}/annotations` | `createRecordAnnotation` | non pris en charge dans cette version |
| POST | `/records/{recordType}/{recordId}/annotations/files` | `createRecordFileAnnotation` | non pris en charge dans cette version |
| GET | `/records/annotations/{id}/files/{partId}/download` | `downloadRecordAnnotationFile` | non pris en charge dans cette version |
| DELETE | `/records/annotations` | `deleteRecordAnnotations` | non pris en charge dans cette version |
| GET | `/risk-levels/{objectType}/{objectId}` | `getActiveRiskLevel` | implémenté, pas encore couvert par un test dédié |
| POST | `/risk-levels/{objectType}/{objectId}` | `overrideRiskLevel` | implémenté, pas encore couvert par un test dédié |

## `ingestion.yml`

- Titre : non renseigné
- Version du document : non renseignée
- OpenAPI : None
- Serveurs : aucun serveur déclaré dans le fichier
- Sécurité : héritée / non déclarée dans ce fichier

| Méthode | Chemin | operationId | Statut Clarity |
| --- | --- | --- | --- |
| POST | `/ingest/{objectType}` | `ingestObject` | implémenté, couvert par un test automatisé |
| PATCH | `/ingest/{objectType}` | `ingestPatchObject` | implémenté, pas encore couvert par un test dédié |
| POST | `/ingest/{objectType}/batch` | `ingestObjectBatch` | implémenté, pas encore couvert par un test dédié |
| PATCH | `/ingest/{objectType}/batch` | `ingestPatchObjectBatch` | implémenté, pas encore couvert par un test dédié |

## `public_api.yaml`

- Titre : Marble decision engine API
- Version du document : 0.1.0
- OpenAPI : 3.0.3
- Serveurs : api.checkmarble.com
- Sécurité : ApiKeyAuth

| Méthode | Chemin | operationId | Statut Clarity |
| --- | --- | --- | --- |
| POST | `/ingestion/{object_type}` | `` | non pris en charge dans cette version |
| PATCH | `/ingestion/{object_type}` | `` | non pris en charge dans cette version |
| POST | `/ingestion/{object_type}/multiple` | `` | non pris en charge dans cette version |
| PATCH | `/ingestion/{object_type}/multiple` | `` | non pris en charge dans cette version |
| POST | `/decisions` | `` | non pris en charge dans cette version |
| GET | `/decisions` | `` | non pris en charge dans cette version |
| POST | `/decisions/all` | `` | non pris en charge dans cette version |
| GET | `/decisions/{decision_id}` | `` | non pris en charge dans cette version |
| GET | `/scheduled-executions` | `` | non pris en charge dans cette version |

## `v1_public_api.yaml`

- Titre : Marble decision engine API
- Version du document : 1.0.0
- OpenAPI : 3.0.0
- Serveurs : https://api.checkmarble.com/v1beta
- Sécurité : BearerTokenAuth, ApiKeyAuth

| Méthode | Chemin | operationId | Statut Clarity |
| --- | --- | --- | --- |
| POST | `/decisions/{decisionId}/snooze` | `snoozeRule` | implémenté, pas encore couvert par un test dédié |
| GET | `/decisions/{decisionId}/screenings` | `getScreeningForDecision` | implémenté, pas encore couvert par un test dédié |
| GET | `/screening/entities/{entityId}` | `getScreeningEntity` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/{screeningId}/refine` | `refineScreening` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/{screeningId}/search` | `searchScreening` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/search` | `searchScreeningFreeformCheck` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/matches/{matchId}` | `reviewScreeningMatch` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/whitelist` | `addScreeningWhitelist` | implémenté, couvert par un test automatisé |
| DELETE | `/screening/whitelist` | `deleteScreeningWhitelist` | implémenté, pas encore couvert par un test dédié |
| POST | `/screening/whitelists/search` | `searchScreeningWhitelist` | implémenté, couvert par un test automatisé |

## API console `console/marblecore-api.yaml`

Index OpenAPI 3.0.3, titre Marble Core API, serveur déclaré `http://localhost:8080`. 196 chemins, chacun en `$ref` vers un fichier voisin. Ce n'est pas un contrat autonome. Clarity ne publie pas cette surface. Les fonctions produit correspondantes (scénarios, dossiers, criblage, scoring, audit) sont exposées par l'API native lorsque elles sont construites.
