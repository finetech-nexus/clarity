# Architecture

Clarity est un monolithe modulaire.

```text
Interfaces web et clients existants
        |
        +-- /v1 et /v1beta          adaptateur Marble
        +-- /api/v1                 adaptateur Ballerine
        +-- /api/clarity/v1         API native
        |
        Services de domaine (client, criblage, risque, surveillance, dossiers)
        |
        +-- PostgreSQL
        +-- Flowable (BPMN) ou repli explicite en développement
        +-- fichiers privés locaux, remplaçables par un stockage S3
```

## Modèle canonique

Tenant, utilisateur, client, profil personne physique, profil personne morale, partie (dirigeant ou bénéficiaire), document, contrôle de vérification, instance de workflow, tâche, approbation, entrée de liste, criblage, correspondance, scénario de décision, décision, objet ingéré, évaluation de risque, règle de surveillance, transaction, alerte, dossier, commentaire, preuve, rapport réglementaire, connecteur, événement d'audit.

L'identifiant servi à Ballerine (`ballerine_runtime_id`) n'est pas la clé primaire du workflow. L'identifiant client externe (`external_ref`) n'est pas l'UUID interne. Le contexte amont qui ne tient pas dans les colonnes reste dans `workflow.context` et `customer.extension`, après validation du corps.

## Workflow

Les définitions exécutables sont les fichiers `flowable/processes/*.bpmn20.xml`. Les tâches utilisateur sont `collect`, `verify`, `screen`, `score`, `analystReview`, `checkerReview`, `remediate` pour l'entrée en relation, et `investigate`, `checkerReview` pour l'investigation.

Si `CLARITY_FLOWABLE_URL` est vide ou si Flowable ne répond pas au déploiement, l'API démarre quand même avec le moteur `in-process-fallback`. Le point de santé le dit. Ce repli suit le même graphe de tâches. Il ne faut pas le décrire comme Flowable.

Les tâches linéaires sont complétées par la plateforme. Les tâches humaines exigent un utilisateur nommé. Le second regard est refusé si l'identité du checker est celle du maker.

## Décision et criblage

Le moteur de scénario lit les règles versionnées du tenant. Les issues possibles sont celles du contrat Marble : `approve`, `review`, `block_and_review`, `decline`. Une correspondance de nom au-dessus du seuil ne choisit pas `decline`. Elle laisse le match `pending` et place la décision en `block_and_review` avec `review_status=pending`.

Le score de nom normalise casse, accents et un tableau cyrillique réduit, puis compare la chaîne et les jetons. Le détail est stocké avec le match. Le jeu de listes livré est synthétique et étiqueté.

## Surveillance

Les règles `threshold`, `velocity`, `cross_border` et `round_amount` produisent des alertes dont le texte dit qu'il s'agit d'un schéma à investiguer. Une alerte ouverte de même client, même règle et même jour est dédupliquée. Le rejeu historique ne crée pas d'alerte.

## États

L'état interne d'entrée en relation (`collecting`, `screening_pending`, `in_review`, `remediation`, `approved`, `rejected`) est distinct du statut de dossier Marble (`pending`, `investigating`, `closed`) et du statut runtime Ballerine (`active`, `completed`, `failed`).
