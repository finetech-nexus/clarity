# Versions d'API

Trois surfaces cohabitent dans le même processus :

| Surface | Préfixe | Version | Source du contrat |
| --- | --- | --- | --- |
| Marble publique | `/v1` | document amont 1.0.0 | `marble/original/v1.yml` |
| Marble beta | `/v1beta` | document amont 1.0.0 | `marble/original/v1beta.yml` |
| Ballerine workflows | `/api/v1` | code amont annoncé 1.3.10, spec statique absente | contrôleurs à `4303cd3e0` |
| Clarity | `/api/clarity/v1` | 0.1.0 | `unified-openapi.yaml` et `/openapi.json` |

Une évolution de l'API native incrémente `/api/clarity/v1` vers `v2` sans changer les préfixes de compatibilité. Un changement de comportement Marble ou Ballerine se fait dans l'adaptateur de cette surface, pas dans le modèle canonique.
