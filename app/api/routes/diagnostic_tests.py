import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import func, select

from app.api.dependencies import AdminUser, DbSession, PaginationParams
from app.core.cache import cached_catalogue
from app.core.errors import ConflictError, NotFoundError
from app.models import DiagnosticTest
from app.schemas.catalogue import DiagnosticTestCreate, DiagnosticTestOut, DiagnosticTestUpdate
from app.schemas.common import Page
from app.services.catalogue_service import commit_catalogue_change

router = APIRouter(prefix="/tests", tags=["diagnostic tests"])


def _name_taken() -> ConflictError:
    return ConflictError("A diagnostic test with this name already exists", code="TEST_NAME_TAKEN")


def _get_test(db: DbSession, test_id: uuid.UUID) -> DiagnosticTest:
    test = db.get(DiagnosticTest, test_id)
    if test is None:
        raise NotFoundError("Diagnostic test not found", code="TEST_NOT_FOUND")
    return test


@router.get("", response_model=Page[DiagnosticTestOut])
def list_tests(db: DbSession, page: PaginationParams) -> dict:
    def load() -> dict:
        total = db.scalar(select(func.count()).select_from(DiagnosticTest)) or 0
        tests = db.scalars(
            select(DiagnosticTest)
            .order_by(DiagnosticTest.name)
            .limit(page.limit)
            .offset(page.offset)
        ).all()
        return Page(
            items=[DiagnosticTestOut.model_validate(t) for t in tests],
            total=total,
            limit=page.limit,
            offset=page.offset,
        ).model_dump(mode="json")

    return cached_catalogue(f"tests:{page.limit}:{page.offset}", load)


@router.get("/{test_id}", response_model=DiagnosticTestOut)
def get_test(test_id: uuid.UUID, db: DbSession) -> dict:
    return cached_catalogue(
        f"test:{test_id}",
        lambda: DiagnosticTestOut.model_validate(_get_test(db, test_id)).model_dump(mode="json"),
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DiagnosticTestOut)
def create_test(data: DiagnosticTestCreate, db: DbSession, _: AdminUser) -> DiagnosticTest:
    test = DiagnosticTest(**data.model_dump())
    db.add(test)
    commit_catalogue_change(db, _name_taken())
    return test


@router.patch("/{test_id}", response_model=DiagnosticTestOut)
def update_test(
    test_id: uuid.UUID, data: DiagnosticTestUpdate, db: DbSession, _: AdminUser
) -> DiagnosticTest:
    test = _get_test(db, test_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(test, field, value)
    commit_catalogue_change(db, _name_taken())
    return test


@router.delete("/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test(test_id: uuid.UUID, db: DbSession, _: AdminUser) -> Response:
    """Also removes the test from every centre. Refused if any booking references it."""
    db.delete(_get_test(db, test_id))
    commit_catalogue_change(
        db, ConflictError("Test has bookings and cannot be deleted", code="TEST_IN_USE")
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
