# Guide de migration

Les clients Marble et Ballerine déjà autorisés continuent d'appeler leur préfixe. Ils ne sont pas redirigés vers `/api/clarity/v1`.

Pour un nouveau canal bancaire :

1. Créer le client une fois, par l'API native ou par `POST /api/v1/external/workflows/run`.
2. Conserver `entity.id` comme référence de système d'engagement et `ballerineEntityId` ou l'UUID Clarity comme identifiant interne. Ils ne sont pas interchangeables.
3. Lire le dossier, le criblage et le risque dans `/api/clarity/v1/customers/{id}`.
4. Envoyer les paiements soit par l'API native, soit par l'ingestion Marble `/v1/ingest/transactions`, soit par `POST /api/v1/external/transactions`. Les trois alimentent la même surveillance.
5. Ne pas migrer un appel Veriff vers l'API native en le faisant passer pour une vérification réelle. L'identité de production reste un connecteur à qualifier.

Les specs historiques Marble `0.1.0` (`/ingestion`, `/screening/whitelist`) ne sont pas des alias. Un client sur ces chemins doit passer au contrat v1 copié dans `marble/original/v1.yml`.
