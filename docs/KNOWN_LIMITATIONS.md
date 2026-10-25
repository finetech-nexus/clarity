# Limites connues

- Les 196 chemins de la console Marble sont montés. Le criblage OpenSanctions, la session Veriff et le modèle de langage ne partent que si `CLARITY_OPENSANCTIONS_API_KEY`, `CLARITY_VERIFF_API_KEY` avec `CLARITY_VERIFF_SHARED_SECRET`, ou `CLARITY_AI_API_KEY` sont définis. Sans clé, le criblage reste le jeu synthétique, Veriff répond 501 sans URL inventée, et la revue est produite par l'analyste local `clarity-local-analyst` avec `production: false`. Aucun dépôt TRACFIN n'est effectué.
- Il n'y a pas de fichier OpenAPI Ballerine original, parce que l'amont n'en versionne pas. Ne pas traiter l'inventaire des contrôleurs comme une spec exécutable.
- `ingestion.yml` ne déclare pas de serveur. Le montage sous `/v1` est une hypothèse.
- Le criblage de développement utilise cinq fiches synthétiques. Ce n'est pas OpenSanctions, ni une liste officielle, ni un prestataire payant. Le texte `SYNTHETIC FIXTURE` est volontaire.
- `POST /api/v1/external/kyc` avec `vendor=veriff` répond 501. Aucune URL de session n'est fabriquée.
- Les contrôles d'identité, de document, de vivacité et de registre des sociétés sont des enregistrements `production: false`.
- Le moteur de workflow par défaut en local est `in-process-fallback`. Flowable n'est actif que si `CLARITY_FLOWABLE_URL` répond et accepte le déploiement BPMN.
- Les scénarios de surveillance détectent des configurations chiffrées. Ils ne détectent pas le blanchiment.
- Le brouillon TRACFIN n'est pas déposé. Il n'existe pas d'adaptateur homologué.
- La rétention, la suppression GDPR, la sauvegarde et la restauration sont documentées comme travaux restants. `retention_days` n'est pas exécuté.
- Le MFA est un attribut, pas un second facteur.
- La limitation de débit est en mémoire, sur un seul processus.
- Les noms de paramètres OpenAPI FastAPI ne reprennent pas toujours la casse du contrat amont. Les URL, elles, portent l'identifiant.
- La clé d'API est acceptée aussi bien en `X-API-KEY` qu'en `Authorization: Bearer` pour Marble, ce que le contrat permet. Elle n'est pas un jeton à durée courte.
- Aucun test n'a été joué contre les bacs à sable `api.checkmarble.com` ou `api.ballerine.app`.
- Clarity n'est pas certifié, agréé, ni déclaré conforme à une obligation de banque.
