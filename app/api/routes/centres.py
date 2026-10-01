import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.dependencies import AdminUser, DbSession, PaginationParams
from app.core.cache import cached_catalogue
from app.core.errors import ConflictError, NotFoundError
from app.models import CentreTest, DiagnosticCentre, DiagnosticTest
from app.schemas.catalogue import (
    CentreCreate,
    CentreDetailOut,
    CentreOut,
    CentreUpdate,
    OfferingCreate,
    OfferingOut,
    OfferingUpdate,
)
from app.schemas.common import Page
from app.services.catalogue_service import commit_catalogue_change

router = APIRouter(prefix="/centres", tags=["diagnostic centres"])


def _get_centre(db: DbSession, centre_id: uuid.UUID) -> DiagnosticCentre:
    centre = db.get(DiagnosticCentre, centre_id)
    if centre is None:
        raise NotFoundError("Diagnostic centre not found", code="CENTRE_NOT_FOUND")
    return centre


def _get_offering(db: DbSession, centre_id: uuid.UUID, test_id: uuid.UUID) -> CentreTest:
    offering = db.scalar(
        select(CentreTest).where(CentreTest.centre_id == centre_id, CentreTest.test_id == test_id)
    )
    if offering is None:
        _get_centre(db, centre_id)  # a missing centre is the more useful error, if that's the case
        raise NotFoundError("This centre does not offer this test", code="OFFERING_NOT_FOUND")
    return offering


# ---- Centres ----


@router.get("", response_model=Page[CentreOut])
def list_centres(db: DbSession, page: PaginationParams) -> dict:
    def load() -> dict:
        total = db.scalar(select(func.count()).select_from(DiagnosticCentre)) or 0
        centres = db.scalars(
            select(DiagnosticCentre)
            .order_by(DiagnosticCentre.name, DiagnosticCentre.id)
            .limit(page.limit)
            .offset(page.offset)
        ).all()
        return Page(
            items=[CentreOut.model_validate(c) for c in centres],
            total=total,
            limit=page.limit,
            offset=page.offset,
        ).model_dump(mode="json")

    return cached_catalogue(f"centres:{page.limit}:{page.offset}", load)


@router.get("/{centre_id}", response_model=CentreDetailOut)
def get_centre(centre_id: uuid.UUID, db: DbSession) -> dict:
    """A centre together with the tests it offers and their prices."""

    def load() -> dict:
        centre = db.scalar(
            select(DiagnosticCentre)
            .where(DiagnosticCentre.id == centre_id)
            .options(selectinload(DiagnosticCentre.offerings))
        )
        if centre is None:
            raise NotFoundError("Diagnostic centre not found", code="CENTRE_NOT_FOUND")
        return CentreDetailOut(
            **CentreOut.model_validate(centre).model_dump(),
            tests=[OfferingOut.from_model(o) for o in centre.offerings],
        ).model_dump(mode="json")

    return cached_catalogue(f"centre:{centre_id}", load)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CentreOut)
def create_centre(data: CentreCreate, db: DbSession, _: AdminUser) -> DiagnosticCentre:
    centre = DiagnosticCentre(**data.model_dump())
    db.add(centre)
    commit_catalogue_change(db, ConflictError("Could not create centre"))
    return centre


@router.patch("/{centre_id}", response_model=CentreOut)
def update_centre(
    centre_id: uuid.UUID, data: CentreUpdate, db: DbSession, _: AdminUser
) -> DiagnosticCentre:
    centre = _get_centre(db, centre_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(centre, field, value)
    commit_catalogue_change(db, ConflictError("Could not update centre"))
    return centre


@router.delete("/{centre_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_centre(centre_id: uuid.UUID, db: DbSession, _: AdminUser) -> Response:
    """Also removes the centre's offerings. Refused if any booking references them."""
    db.delete(_get_centre(db, centre_id))
    commit_catalogue_change(
        db, ConflictError("Centre has bookings and cannot be deleted", code="CENTRE_IN_USE")
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Tests offered by a centre ----


@router.get("/{centre_id}/tests", response_model=list[OfferingOut])
def list_centre_tests(centre_id: uuid.UUID, db: DbSession) -> list:
    def load() -> list:
        centre = _get_centre(db, centre_id)
        return [OfferingOut.from_model(o).model_dump(mode="json") for o in centre.offerings]

    return cached_catalogue(f"centre:{centre_id}:tests", load)


@router.post("/{centre_id}/tests", status_code=status.HTTP_201_CREATED, response_model=OfferingOut)
def add_centre_test(
    centre_id: uuid.UUID, data: OfferingCreate, db: DbSession, _: AdminUser
) -> OfferingOut:
    _get_centre(db, centre_id)
    if db.get(DiagnosticTest, data.test_id) is None:
        raise NotFoundError("Diagnostic test not found", code="TEST_NOT_FOUND")

    offering = CentreTest(centre_id=centre_id, test_id=data.test_id, price=data.price)
    db.add(offering)
    commit_catalogue_change(
        db,
        ConflictError(
            "This centre already offers this test; use PATCH to change the price",
            code="OFFERING_EXISTS",
        ),
    )
    return OfferingOut.from_model(offering)


@router.patch("/{centre_id}/tests/{test_id}", response_model=OfferingOut)
def update_centre_test(
    centre_id: uuid.UUID, test_id: uuid.UUID, data: OfferingUpdate, db: DbSession, _: AdminUser
) -> OfferingOut:
    """Change the price. Existing bookings keep the amount they were created with."""
    offering = _get_offering(db, centre_id, test_id)
    offering.price = data.price
    commit_catalogue_change(db, ConflictError("Could not update price"))
    return OfferingOut.from_model(offering)


@router.delete("/{centre_id}/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_centre_test(
    centre_id: uuid.UUID, test_id: uuid.UUID, db: DbSession, _: AdminUser
) -> Response:
    db.delete(_get_offering(db, centre_id, test_id))
    commit_catalogue_change(
        db,
        ConflictError("This offering has bookings and cannot be removed", code="OFFERING_IN_USE"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
