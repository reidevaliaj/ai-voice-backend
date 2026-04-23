from contextlib import contextmanager

from sqlalchemy import create_engine
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
