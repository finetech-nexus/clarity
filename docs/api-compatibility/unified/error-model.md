# Modèle d'erreur

Les erreurs ne sont pas unifiées entre les surfaces.

| Surface | Corps |
| --- | --- |
| Marble | `{ "code": "invalid_payload\|unprocessable_entity\|not_found\|feature_disabled\|feature_not_configured\|server_error", "messages": [], "detail": {} }` |
| Ballerine | Forme NestJS `{ "statusCode", "message", "error" }` sur les routes adaptées |
| Clarity | `{ "code", "message" }` |

Un 401 Marble n'a pas de corps. Un 401 Ballerine a un corps NestJS.
