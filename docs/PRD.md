# Clarity — produit

**Clarity.** Une plateforme pour l'intelligence KYC et AML d'une banque.

Clarity réunit, dans une seule application, l'entrée en relation et la décision documentaire attendues d'une plateforme de type Ballerine, et la surveillance, le criblage et l'investigation attendus d'une plateforme de type Marble. Ce n'est pas deux produits derrière un menu. Un client a un seul dossier, quel que soit le canal d'API.

## Utilisateurs

- Analyste d'entrée en relation
- Investigateur AML
- Checker, second regard
- Administrateur du tenant
- Systèmes d'engagement, via les API de compatibilité

## Parcours livrés en 0.1.0

1. Ouvrir une personne physique ou une personne morale, avec bénéficiaires effectifs, documents et finalité de la relation.
2. Enregistrer une vérification explicitement non productive tant qu'aucun prestataire n'est connecté.
3. Cribbler contre un jeu synthétique sanctions, PEP, média défavorable et liste interne. Une correspondance reste candidate tant qu'un humain ne la qualifie pas.
4. Calculer un score explicable, version `clarity-risk-1`, avec dépassement réservé au checker.
5. Faire avancer le dossier dans un processus BPMN Flowable : collecte, vérification, criblage, score, analyste, checker. Le maker ne peut pas être le checker.
6. Ingérer des transactions synthétiques, déclencher des scénarios de seuil, de vélocité, transfrontaliers et de montants ronds, et ouvrir une alerte dédupliquée.
7. Investiguer, noter, escalader et préparer un projet de déclaration. Aucun dépôt TRACFIN n'est effectué.
8. Lire la même réalité depuis `/v1`, `/api/v1` et `/api/clarity/v1` lorsque la route existe.

## Hors produit, pour l'instant

Voir `KNOWN_LIMITATIONS.md`. En particulier : pas de Veriff, pas d'OpenSanctions, pas de registre officiel, pas de soumission TRACFIN, pas de l'API console Marble, pas de certification réglementaire.
