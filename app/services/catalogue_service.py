from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.cache import invalidate_catalogue
from app.core.errors import ConflictError


def commit_catalogue_change(db: Session, on_integrity_error: ConflictError) -> None:
    """Commit an admin change to centres/tests/offerings and invalidate the catalogue cache.

    Every catalogue write goes through here, so no write path can forget the invalidation.
    The cache is invalidated only after a successful commit.
    """
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise on_integrity_error from None
    invalidate_catalogue()
