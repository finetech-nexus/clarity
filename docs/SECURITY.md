# Sécurité

Ce document décrit les contrôles présents et ceux qui restent à faire. Il ne constitue pas une attestation de conformité LCB-FT, DORA, GDPR ou PCI.

## En place dans cette version

- Isolation des lectures et écritures par `tenant_id`, couverte par un test qui crée un client d'un autre tenant et vérifie qu'il est invisible.
- Mots de passe démo dérivés par PBKDF2-HMAC-SHA256. Jetons JWT HS256. Clés d'API stockées par empreinte SHA-256.
- Séparation des clés Marble et Ballerine.
- Maker-checker sur l'approbation de dossier.
- Journal d'audit des connexions, ouvertures de dossier, décisions, dépassements de score, notes, escalades, preuves et préparations déclaratives.
- Lecture d'un client journalisée.
- Plafond de téléversement à 10 Mo, stockage hors de la racine web, empreinte SHA-256.
- Filtre de logs qui masque un message contenant un secret, un bearer ou un mot de passe.
- Vérification HMAC du hook Ballerine.
- Limitation de débit en mémoire, 120 requêtes par minute et par secret. Elle ne survit pas à plusieurs processus.
- Les rapports de type TRACFIN restent des brouillons. Le canal de dépôt est `not_connected`.

## À faire avant une banque réelle

- OIDC avec MFA effectif, rotation des secrets, coffre-fort, et fin du secret JWT de développement.
- TLS terminé devant l'API, chiffrement au repos de PostgreSQL et du stockage objet.
- Limitation de débit distribuée, verrouillage de compte, journalisation d'accès aux brouillons déclaratifs vers un SIEM.
- Revue de menace sur les adaptateurs, tests de charge, sauvegarde restaurée réellement, politique de rétention exécutée. Le champ `retention_days` existe et n'efface encore rien.
- Connecteurs d'identité et de listes sous contrat, avec interdiction de repli silencieux vers le jeu synthétique.
- Le drapeau `mfa_enabled` des utilisateurs de démonstration n'est pas une authentification forte.

## Données de démonstration

Le tenant `Banque Démonstration Clarity` et ses listes sont synthétiques. Les noms qui ressemblent à des personnes sanctionnées ou politiquement exposées sont des fixtures. Ils ne doivent pas sortir d'un environnement de développement.
