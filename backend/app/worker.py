"""Points d'entrée des travaux asynchrones.

Les fonctions sont appelées dans le processus de requête pour cette version.
Un worker Celery peut les envelopper lorsque CLARITY_BROKER_URL est défini.
Le module n'exige pas Celery pour démarrer l'API.
"""

from sqlalchemy import select

from app.models import Customer, Tenant
from app.services import _screen_customer, assess_customer


def rescreen_tenant(db, tenant_id: str) -> int:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return 0
    count = 0
    for customer in db.scalars(select(Customer).where(Customer.tenant_id == tenant_id)).all():
        _screen_customer(db, tenant, customer)
        assess_customer(db, tenant, customer)
        count += 1
    return count


def run_adverse_media(db, tenant_id: str | None = None) -> dict:
    """Collect due sources, analyze new articles once, then rescreen the portfolio."""
    from app.adverse_media import run_due

    return run_due(db, tenant_id)
