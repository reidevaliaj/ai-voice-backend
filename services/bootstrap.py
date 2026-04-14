from sqlalchemy import inspect, select

from app_config import ADMIN_BOOTSTRAP_EMAIL, ADMIN_BOOTSTRAP_PASSWORD
from db import engine
from models import AdminUser
from security import hash_password, verify_password
from services.tenants import seed_default_tenant


def ensure_bootstrap_state(session) -> None:
    inspector = inspect(engine)
    required_tables = {"admin_users", "tenants", "tenant_agent_configs", "tenant_integrations"}
    if not required_tables.issubset(set(inspector.get_table_names())):
        return

    admin = session.scalar(select(AdminUser).where(AdminUser.email == ADMIN_BOOTSTRAP_EMAIL))
    if admin is None:
        admin = AdminUser(
            email=ADMIN_BOOTSTRAP_EMAIL,
            password_hash=hash_password(ADMIN_BOOTSTRAP_PASSWORD),
            is_active=True,
        )
        session.add(admin)
    elif not verify_password(ADMIN_BOOTSTRAP_PASSWORD, admin.password_hash):
        admin.password_hash = hash_password(ADMIN_BOOTSTRAP_PASSWORD)
        admin.is_active = True

    seed_default_tenant(session)
    session.flush()
