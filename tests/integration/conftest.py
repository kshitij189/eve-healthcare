from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.redis_client import get_redis
from app.core.security import hash_password
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import CentreTest, DiagnosticCentre, DiagnosticTest, User, UserRole
from tests.helpers import auth_headers


def _create_database_if_missing() -> None:
    url = engine.url
    admin_engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Build the schema through the real migrations (and exercise the downgrade too)."""
    _create_database_if_missing()
    config = Config("alembic.ini")
    config.attributes["configure_logger"] = False
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def clean_state() -> Iterator[None]:
    # Deleting rows (instead of wrapping each test in a rolled-back transaction) lets tests
    # use several connections at once, which the concurrency tests need.
    get_redis().flushdb()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    get_redis().flushdb()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


@pytest.fixture
def make_user(db: Session) -> Callable[..., User]:
    counter = iter(range(1, 1000))

    def _make_user(role: UserRole = UserRole.USER, password: str = "password123") -> User:
        n = next(counter)
        user = User(
            name=f"User {n}",
            email=f"user{n}@example.com",
            password_hash=hash_password(password),
            role=role,
        )
        db.add(user)
        db.commit()
        return user

    return _make_user


@pytest.fixture
def user_headers(make_user) -> dict[str, str]:
    return auth_headers(make_user())


@pytest.fixture
def other_user_headers(make_user) -> dict[str, str]:
    return auth_headers(make_user())


@pytest.fixture
def admin_headers(make_user) -> dict[str, str]:
    return auth_headers(make_user(role=UserRole.ADMIN))


@dataclass
class Catalogue:
    alpha: DiagnosticCentre  # offers CBC (500) and lipid profile (800)
    beta: DiagnosticCentre  # offers CBC (450) only
    cbc: DiagnosticTest
    lipid: DiagnosticTest
    vitamin_d: DiagnosticTest  # offered nowhere


@pytest.fixture
def catalogue(db: Session) -> Catalogue:
    alpha = DiagnosticCentre(name="Alpha Labs", location="Bengaluru")
    beta = DiagnosticCentre(name="Beta Diagnostics", location="Mumbai")
    cbc = DiagnosticTest(name="Complete Blood Count")
    lipid = DiagnosticTest(name="Lipid Profile")
    vitamin_d = DiagnosticTest(name="Vitamin D")
    db.add_all([alpha, beta, cbc, lipid, vitamin_d])
    db.flush()
    db.add_all(
        [
            CentreTest(centre_id=alpha.id, test_id=cbc.id, price=Decimal("500.00")),
            CentreTest(centre_id=alpha.id, test_id=lipid.id, price=Decimal("800.00")),
            CentreTest(centre_id=beta.id, test_id=cbc.id, price=Decimal("450.00")),
        ]
    )
    db.commit()
    return Catalogue(alpha=alpha, beta=beta, cbc=cbc, lipid=lipid, vitamin_d=vitamin_d)
