# Matrice de compatibilité Ballerine

Pas de spécification OpenAPI statique officielle : voir `contract-inventory.md`. La compatibilité ci-dessous porte sur les routes lues dans les contrôleurs à la révision `4303cd3e0`.

| Capacité | Route amont | Service Clarity | Statut |
| --- | --- | --- | --- |
| Lancer un workflow KYC/KYB | POST `/api/v1/external/workflows/run` | Client canonique + processus Flowable `clarityKycIndividual` ou `clarityKybBusiness` | Partiel, testé. Les plugins, checks Veriff et webhooks sortants ne sont pas exécutés. |
| Lister et lire un runtime | GET workflows et GET `/{id}` | `workflow_instances`, identifiant externe distinct de l'UUID interne | Partiel, testé sur la liste. |
| Envoyer un événement | POST `/{id}/event` | Tâche Flowable courante | Partiel. Les noms d'événements reconnus sont approve, reject, revision, remediate, return, close, escalate. |
| Hook signé | POST `/{id}/hook/{event}` | Vérification HMAC | Testé pour accepter et refuser la signature. |
| Documents | POST et PATCH decision | `identity_documents`, stockage privé local | Partiel. |
| Transactions | POST, POST `/alt`, GET | Normalisation puis règles de surveillance | Partiel. Les variantes bulk ne sont pas servies. |
| KYC Veriff | POST `/external/kyc` | Aucun appel Veriff | Non compatible en succès. 501 explicite. |
| Définitions de workflow | GET/POST `/api/v1/workflow-definition`, upgrade, copy | Documents de configuration, `processKey` Flowable | Exécuté par `POST /external/workflows/run` quand `workflowId` est l'id ou le nom publié. |
| Définitions d'interface, filtres, alertes | `/ui-definition`, `/external/filters`, `/external/alert-definition` | Documents de configuration et règles de surveillance | Partiel. |
| Personnes, sociétés, transactions en lot, notes, évaluations, stockage, collection flow | routes `external/*` et `collection-flow/*` lues sur les contrôleurs | Même client et mêmes processus | Partiel. Le jeton de session collection flow Ballerine n'est pas reproduit : l'adaptateur exige la clé API et `workflowRuntimeId`. |

Les statuts de runtime renvoyés restent dans l'énumération lue dans Prisma : `active`, `completed`, `failed`. L'état métier (`state`) porte la tâche BPMN ou `approved` / `rejected`.

Un même client créé par cette surface est visible dans l'API native. L'identifiant client Ballerine (`entity.id`) est conservé comme référence externe. `ballerineEntityId` est l'identifiant Clarity, distinct.
