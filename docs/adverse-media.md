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

L'analyse `lexicon-1.0` classe des expressions. Elle ne produit pas une lecture de modèle neuronal et ne conclut pas à la culpabilité.

GDELT publie le titre. Le collecteur ouvre ensuite la page de l'article. Si le site ne répond pas, seul le titre est enregistré.

Les actionnaires sont des `Party` de rôle `shareholder`, au même titre que les dirigeants et les bénéficiaires effectifs.

Les déclarations de soupçon ne sont ni lues ni écrites ici. Les articles sont stockés comme le reste du KYC.

L'écran AML Adverse media couvre le tableau de bord, la recherche, les sources, les articles, les alertes et le journal. Au démarrage, les sept sources sont enregistrées actives avec un flux public vérifié : GDELT (requête Tunisie), La Presse, BBC World, France 24, les communiqués de la Commission européenne, le fil RSS de l'ONU et Jeune Afrique. Une source déjà enregistrée sans URL reçoit ce flux. Une URL saisie par un administrateur n'est pas remplacée.
