# Plan d'implémentation

## Fait dans 0.1.0

1. Découverte des contrats. Specs Marble copiées sans modification. Absence de spec statique Ballerine consignée.
2. Adaptateurs `/v1`, `/v1beta` (sous-ensemble), `/api/v1` (sous-ensemble) et `/api/clarity/v1`.
3. Modèle unique, tenant, audit, maker-checker.
4. Processus BPMN et client Flowable, avec repli explicite.
5. Entrée en relation personne physique et morale, criblage explicable, score, surveillance, alertes, dossiers, brouillon déclaratif.
6. Interface Clarity et tests Pytest du périmètre déclaré ci-dessus.

## Ensuite

1. Exécuter Schemathesis sur `original/v1.yml` contre un serveur Clarity, et publier le rapport d'écarts dans `compatibility-report.json`.
2. Couvrir par des tests les routes v1 déjà codées mais non exercées : snooze, refine, search, batch, createAllDecisions, review de match.
3. Brancher un vrai Flowable dans l'intégration continue et comparer les tâches rendues par le moteur au graphe BPMN, au-delà de la comparaison statique des identifiants.
4. Remplacer le repli d'identité et le jeu de listes par des connecteurs nommés, désactivés tant que les secrets ne sont pas présents.
5. Étendre `/v1beta` seulement opération par opération, à partir du YAML, sans inventer de champ.
6. Émettre les webhooks Ballerine signés.
7. OIDC, sauvegarde restaurée, chiffrement, rétention effective.
8. Qualifier un canal TRACFIN seulement avec la documentation officielle et un environnement autorisé. Jusque-là le statut reste `draft` et `not_connected`.

La compatibilité n'est pas un chantier ultérieur optionnel : une route non testée reste marquée comme telle dans la matrice.
