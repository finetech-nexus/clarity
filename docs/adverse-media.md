# Adverse media

Moteur indépendant d'OpenSanctions. Le référentiel reste `Customer` et `Party`.

## Cycle

1. `collect_source` lit une source active. Une source inactive, en erreur ou hors quota n'enregistre aucun article.
2. `ingest_article` déduplique par URL et hash de contenu.
3. `process_queue` lance `lexicon-1.0` une fois par hash. Un échec se relance explicitement, avec trois tentatives et un délai croissant.
4. Le rapprochement compare les entités extraites aux personnes physiques, sociétés, dirigeants, représentants, bénéficiaires effectifs, actionnaires et autres parties.
5. Une alerte exige un score d'identité et un score médiatique au-dessus des seuils. Une donnée absente n'ajoute pas de point. Une date de naissance contradictoire écarte le rapprochement.
6. Le score client `clarity-risk-3.4` n'ajoute le facteur `media` qu'après une décision humaine `confirmed_relevant` : 8 pour allégation, enquête ou poursuites, 16 pour une condamnation rapportée, plafond 24.

Le moteur ne produit pas de conclusion de culpabilité. Une recherche sans article pertinent a le statut `NoRelevantMediaFound` : ce n'est pas la preuve qu'il n'existe aucun média défavorable. Un nom seul donne `ManualReviewRequired` et ne confirme jamais l'identité. `ConfirmedMatch` n'existe qu'après une décision humaine. Les autres statuts sont `PotentialMatch`, `InsufficientInformation` et `ScreeningFailed`.

Le rôle extrait est l'un de : convicted, charged, arrested, investigated, suspected, accused, witness, victim, mentioned, acquitted, unknown. Témoin et victime ne créent pas d'alerte. Le classifieur actif est le lexique `lexicon-1.0` : il ne retient qu'un passage présent dans le texte. Aucun modèle de langage n'est branché.

## Connecteurs

GDELT, RSS, presse tunisienne, presse internationale, publications officielles, sources institutionnelles, API publique JSON. Le catalogue est créé inactif. GDELT reste inactif tant qu'une requête n'est pas enregistrée. Aucun article n'est simulé.

## Planification

`worker.run_adverse_media` et `POST /api/clarity/v1/adverse-media/cycle`. Hors tests, un thread vérifie les sources dues chaque minute. La fréquence par défaut est 60 minutes. La réévaluation client est quotidienne, hebdomadaire ou mensuelle selon le niveau de risque, et rejoue le rapprochement sans relancer l'analyse déjà faite.

## Fiche vérification

L'onglet Adverse media lit `adverse_media` sur `GET /api/clarity/v1/desks/kyc/{runtime_id}`. Le bouton appelle `POST .../adverse-media`.

## Limites

L'analyse `lexicon-1.0` classe des expressions. Elle ne produit pas une lecture de modèle neuronal et ne conclut pas à la culpabilité.

GDELT publie le titre. Le collecteur courant ouvre ensuite la page de l'article. Si le site ne répond pas, seul le titre est enregistré.

La recherche d'un nom, `POST /api/clarity/v1/adverse-media/search`, interroge Google Actualités et GDELT sur un, trois ou cinq ans, par tranches d'un an. La requête ajoute les thèmes corruption, fraude, blanchiment, condamnation, arrestation, poursuites et terrorisme. Seuls les articles renvoyés sont enregistrés. `lexicon-1.0` tourne ensuite, avant le statut. Un article neutre reste `NoRelevantMediaFound`, avec le nombre d'articles lus. La source « Recherche internet » n'entre pas dans le batch horaire.

La recherche lointaine d'une source, `POST /api/clarity/v1/adverse-media/sources/{id}/deep-search`, ne concerne que GDELT. Elle parcourt un, trois ou cinq ans par fenêtres d'un mois et ajoute à la requête les thèmes corruption, fraude, blanchiment et terrorisme. Seuls les articles renvoyés par GDELT sont enregistrés. Pendant ce parcours, la page de l'article n'est pas ouverte : le titre fait office de texte. Une fenêtre qui renvoie 100 articles est coupée en deux, jusqu'à une semaine. Les appels GDELT sont espacés de six secondes et une réponse « trop de requêtes » est relancée.

GDELT est un projet de données public. L'API DOC se consulte sans clé. Ce n'est pas un logiciel à installer.

Les actionnaires sont des `Party` de rôle `shareholder`, au même titre que les dirigeants et les bénéficiaires effectifs.

Les déclarations de soupçon ne sont ni lues ni écrites ici. Les articles sont stockés comme le reste du KYC.

Les sources se configurent dans Administration, onglet Third party. Le catalogue public couvre la presse locale et économique, la presse internationale, les publications officielles, les institutions et GDELT. Dow Jones, LexisNexis et ComplyAdvantage apparaissent comme fournisseurs sous licence : sans connexion réelle, ils ne créent aucun article.

Le batch tourne périodiquement, et à la demande. Il expose un pourcentage d'avancement, pas le détail des articles. L'écran Adverse media sert à la recherche et à la revue des résultats.
