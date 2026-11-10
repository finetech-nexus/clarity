# Adverse media

Moteur indépendant d'OpenSanctions. Le référentiel reste `Customer` et `Party`.

## Cycle

1. `collect_source` lit une source active. Une source inactive, en erreur ou hors quota n'enregistre aucun article.
2. `ingest_article` déduplique par URL et hash de contenu.
3. `process_queue` lance `lexicon-1.0` une fois par hash. Un échec se relance explicitement, avec trois tentatives et un délai croissant.
4. Le rapprochement compare les entités extraites aux personnes physiques, sociétés, dirigeants, représentants, bénéficiaires effectifs, actionnaires et autres parties.
5. Une alerte exige un score d'identité et un score médiatique au-dessus des seuils. Une donnée absente n'ajoute pas de point. Une date de naissance contradictoire écarte le rapprochement.
6. Le score client `clarity-risk-3.4` n'ajoute le facteur `media` qu'après une décision humaine `confirmed_relevant` : 8 pour allégation, enquête ou poursuites, 16 pour une condamnation rapportée, plafond 24.

Le moteur ne produit pas de conclusion de culpabilité.

## Connecteurs

GDELT, RSS, presse tunisienne, presse internationale, publications officielles, sources institutionnelles, API publique JSON. Le catalogue est créé inactif. GDELT reste inactif tant qu'une requête n'est pas enregistrée. Aucun article n'est simulé.

## Planification

`worker.run_adverse_media` et `POST /api/clarity/v1/adverse-media/cycle`. Hors tests, un thread vérifie les sources dues chaque minute. La fréquence par défaut est 60 minutes. La réévaluation client est quotidienne, hebdomadaire ou mensuelle selon le niveau de risque, et rejoue le rapprochement sans relancer l'analyse déjà faite.

## Fiche vérification

L'onglet Adverse media lit `adverse_media` sur `GET /api/clarity/v1/desks/kyc/{runtime_id}`. Le bouton appelle `POST .../adverse-media`.

## Limites

Pas de modèle neuronal. Pas de chiffrement de champ au-delà du stockage déjà utilisé pour le KYC. Les déclarations de soupçon ne sont ni lues ni écrites ici. GDELT ne fournit que le titre dans le mode `artlist`.
