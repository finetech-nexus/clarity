# Clarity

Plateforme bancaire d'entrée en relation et de surveillance AML. Un client, trois accès : l'API native, une surface compatible avec le contrat public Marble, une surface compatible avec les routes Ballerine qui ont pu être lues.

Les données livrées avec le tenant de démonstration sont synthétiques.

## Démarrage local

```bash
cd clarity
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/uvicorn app.main:app --port 8095
```

Dans un autre terminal :

```bash
cd clarity/frontend
npm install
npm run dev
```

Interface : `http://localhost:3010`. API : `http://localhost:8095`. Santé : `GET /api/clarity/v1/health`.

Compte analyste : `camille.morel@clarity.bank`. Checker : `hugo.bernard@clarity.bank`. Mot de passe : `Clarity-demo-2026!`.

Clé Marble : `clry_demo_marble_v1` dans `X-API-KEY`. Clé Ballerine : `Bearer clry_demo_ballerine_v1`.

Sans `CLARITY_FLOWABLE_URL`, le moteur de workflow est le repli de développement. Le champ `workflow_engine` de la santé le dit.

## Tests

```bash
cd clarity/backend
../.venv/bin/pytest
```

## Docker

`docker compose up --build` démarre PostgreSQL, Redis, Flowable, l'API et l'interface. Définir `CLARITY_FLOWABLE_URL=http://flowable:8080` pour que l'API déploie les BPMN. Si Flowable n'est pas prêt, l'API reste disponible et le signale.

## Documentation

- `docs/PRD.md`
- `docs/ARCHITECTURE.md`
- `docs/SECURITY.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/KNOWN_LIMITATIONS.md`
- `docs/api-compatibility/`

Les YAML sous `docs/api-compatibility/marble/original/` sont les contrats Marble copiés sans modification. Ils restent sous Elastic License 2.0. Voir `PROVENANCE.md`.
