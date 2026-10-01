"""Load deterministic development data. Safe to run repeatedly.

    python -m app.seed

Creates an admin from SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD if both are set.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import SessionLocal
from app.models import CentreTest, DiagnosticCentre, DiagnosticTest, User, UserRole

TESTS = {
    "Complete Blood Count": "Measures red cells, white cells, haemoglobin and platelets.",
    "Lipid Profile": "Cholesterol (total, HDL, LDL) and triglycerides. Requires 10-12h fasting.",
    "HbA1c": "Average blood glucose over the past 2-3 months.",
    "Thyroid Profile (T3, T4, TSH)": "Screens thyroid function.",
    "Vitamin D (25-OH)": "Measures vitamin D levels.",
}

# The same test is priced differently at different centres, which is why price lives on CentreTest.
CENTRES = {
    ("EVE Diagnostics - Indiranagar", "Bengaluru"): {
        "Complete Blood Count": "450.00",
        "Lipid Profile": "800.00",
        "HbA1c": "550.00",
        "Thyroid Profile (T3, T4, TSH)": "700.00",
    },
    ("EVE Diagnostics - Andheri West", "Mumbai"): {
        "Complete Blood Count": "400.00",
        "Lipid Profile": "850.00",
        "Vitamin D (25-OH)": "1200.00",
    },
}


def seed(db: Session) -> None:
    tests: dict[str, DiagnosticTest] = {}
    for name, description in TESTS.items():
        test = db.scalar(select(DiagnosticTest).where(DiagnosticTest.name == name))
        if test is None:
            test = DiagnosticTest(name=name, description=description)
            db.add(test)
        tests[name] = test
    db.flush()

    for (centre_name, location), prices in CENTRES.items():
        centre = db.scalar(
            select(DiagnosticCentre).where(
                DiagnosticCentre.name == centre_name, DiagnosticCentre.location == location
            )
        )
        if centre is None:
            centre = DiagnosticCentre(name=centre_name, location=location)
            db.add(centre)
            db.flush()
        for test_name, price in prices.items():
            test = tests[test_name]
            exists = db.scalar(
                select(CentreTest).where(
                    CentreTest.centre_id == centre.id, CentreTest.test_id == test.id
                )
            )
            if exists is None:
                db.add(CentreTest(centre_id=centre.id, test_id=test.id, price=Decimal(price)))

    settings = get_settings()
    admin_email = (settings.seed_admin_email or "").strip().lower()
    admin_password = settings.seed_admin_password
    if admin_email and admin_password:
        if db.scalar(select(User).where(User.email == admin_email)) is None:
            db.add(
                User(
                    name="Admin",
                    email=admin_email,
                    password_hash=hash_password(admin_password),
                    role=UserRole.ADMIN,
                )
            )
        print(f"Admin account: {admin_email}")
    else:
        print("SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD not set; skipping admin user.")

    db.commit()
    print(f"Seeded {len(TESTS)} tests and {len(CENTRES)} centres.")


if __name__ == "__main__":
    with SessionLocal() as session:
        seed(session)
