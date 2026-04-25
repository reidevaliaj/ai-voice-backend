from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app_config import OUTGOING_DATABASE_URL


class OutgoingBase(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if OUTGOING_DATABASE_URL.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


outgoing_engine = create_engine(OUTGOING_DATABASE_URL, future=True, **_engine_kwargs())
OutgoingSessionLocal = sessionmaker(
    bind=outgoing_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def init_outgoing_db() -> None:
    from outgoing_models import OutgoingBase as ModelsBase

    ModelsBase.metadata.create_all(bind=outgoing_engine)
    _upgrade_outgoing_schema()


def _upgrade_outgoing_schema() -> None:
    inspector = inspect(outgoing_engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("outgoing_tenant_profiles")}
    except Exception:
        return

    pending_columns = {
        "assistant_language": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN assistant_language VARCHAR(16) NOT NULL DEFAULT ''",
        "stt_language": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN stt_language VARCHAR(16) NOT NULL DEFAULT ''",
        "llm_model": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN llm_model VARCHAR(128) NOT NULL DEFAULT ''",
        "tts_voice": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN tts_voice VARCHAR(128) NOT NULL DEFAULT ''",
        "tts_speed": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN tts_speed FLOAT NOT NULL DEFAULT 1.0",
    }
    missing = [statement for name, statement in pending_columns.items() if name not in columns]
    if not missing:
        return

    with outgoing_engine.begin() as connection:
        for statement in missing:
            connection.execute(text(statement))


@contextmanager
def outgoing_db_session() -> Session:
    session = OutgoingSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_outgoing_db():
    session = OutgoingSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
