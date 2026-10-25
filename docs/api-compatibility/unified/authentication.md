# Authentification

Les trois surfaces n'ont pas le même schéma.

## Marble `/v1` et `/v1beta`

Contrat : `BearerTokenAuth` (header `Authorization: Bearer`) ou `ApiKeyAuth` (header `X-API-KEY`). Un jeton Clarity ou une clé Ballerine est refusé. Réponse 401 sans corps, comme la spec qui ne définit pas de schéma pour 401.

Clé de démonstration : `clry_demo_marble_v1`.

## Ballerine `/api/v1`

Bearer uniquement, clé distincte. Le garde client amont ne partage pas le secret Marble.

Clé de démonstration : `clry_demo_ballerine_v1`.

Le hook `POST /api/v1/external/workflows/{id}/hook/{event}` est public et exige `x-hmac-signature`. Le secret de développement est `clarity-dev-webhook-secret`. L'algorithme reprend la fonction publique `verifySignature` : HMAC-SHA256 hexadécimal du `JSON.stringify` équivalent.

## Clarity `/api/clarity/v1`

JWT HS256 émis par `POST /api/clarity/v1/auth/login`. Rôles : `admin`, `analyst`, `investigator`, `checker`. Le contrôle maker-checker interdit au même utilisateur d'instruire puis d'approuver.

Comptes de démonstration, mot de passe `Clarity-demo-2026!` :

- `iris.p@example.org`
- `camille.morel@clarity.bank`
- `lea.martin@clarity.bank`
- `hugo.bernard@clarity.bank`

MFA : les comptes de démonstration ont le drapeau `mfa_enabled`. Ce drapeau ne remplace pas un fournisseur OIDC. Brancher un IdP OIDC avec MFA est requis avant une mise en production bancaire.

Limitation de débit : 120 requêtes par minute et par secret, réponse 429. Ce code n'est pas dans le contrat Marble ; il est assumé comme contrôle de plateforme.
