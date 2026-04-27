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
        profile_columns = {column["name"] for column in inspector.get_columns("outgoing_tenant_profiles")}
    except Exception:
        return

    pending_profile_columns = {
        "provider": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN provider VARCHAR(20) NOT NULL DEFAULT 'telnyx'",
        "assistant_language": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN assistant_language VARCHAR(16) NOT NULL DEFAULT ''",
        "stt_language": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN stt_language VARCHAR(16) NOT NULL DEFAULT ''",
        "llm_model": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN llm_model VARCHAR(128) NOT NULL DEFAULT ''",
        "tts_voice": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN tts_voice VARCHAR(128) NOT NULL DEFAULT ''",
        "tts_speed": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN tts_speed FLOAT NOT NULL DEFAULT 1.0",
        "min_endpointing_delay": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN min_endpointing_delay FLOAT NOT NULL DEFAULT 0.3",
        "max_endpointing_delay": "ALTER TABLE outgoing_tenant_profiles ADD COLUMN max_endpointing_delay FLOAT NOT NULL DEFAULT 1.2",
    }
    pending_number_columns = {
        "provider": "ALTER TABLE outgoing_caller_numbers ADD COLUMN provider VARCHAR(20) NOT NULL DEFAULT 'telnyx'",
    }
    pending_call_columns = {
        "provider": "ALTER TABLE outgoing_calls ADD COLUMN provider VARCHAR(20) NOT NULL DEFAULT 'telnyx'",
        "provider_call_sid": "ALTER TABLE outgoing_calls ADD COLUMN provider_call_sid VARCHAR(255) NOT NULL DEFAULT ''",
        "twilio_call_sid": "ALTER TABLE outgoing_calls ADD COLUMN twilio_call_sid VARCHAR(255) NOT NULL DEFAULT ''",
        "twilio_event_type": "ALTER TABLE outgoing_calls ADD COLUMN twilio_event_type VARCHAR(80) NOT NULL DEFAULT ''",
        "twilio_hangup_cause": "ALTER TABLE outgoing_calls ADD COLUMN twilio_hangup_cause VARCHAR(120) NOT NULL DEFAULT ''",
    }
    pending_event_columns = {
        "provider": "ALTER TABLE outgoing_call_events ADD COLUMN provider VARCHAR(20) NOT NULL DEFAULT 'telnyx'",
        "provider_call_sid": "ALTER TABLE outgoing_call_events ADD COLUMN provider_call_sid VARCHAR(255) NOT NULL DEFAULT ''",
    }

    def _missing_statements(table_name: str, pending: dict[str, str]) -> list[str]:
        try:
            table_columns = {column["name"] for column in inspector.get_columns(table_name)}
        except Exception:
            return []
        return [statement for name, statement in pending.items() if name not in table_columns]

    missing = []
    missing.extend([statement for name, statement in pending_profile_columns.items() if name not in profile_columns])
    missing.extend(_missing_statements("outgoing_caller_numbers", pending_number_columns))
    missing.extend(_missing_statements("outgoing_calls", pending_call_columns))
    missing.extend(_missing_statements("outgoing_call_events", pending_event_columns))
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
