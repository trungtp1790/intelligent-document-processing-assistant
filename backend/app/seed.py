from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db.models import Membership, Organization, PurchaseOrder, PurchaseOrderLine, Supplier, User
from app.db.session import SessionLocal
from app.rag.corpus import seed_policy_corpus
from app.security import hash_password, verify_password


def seed() -> None:
    if settings.app_env == "production":
        raise SystemExit("Demo seeding is disabled in production.")
    password = os.environ.get("DEMO_PASSWORD", "")
    if not password:
        raise SystemExit("Set DEMO_PASSWORD in the local .env before seeding demo accounts.")
    operator_email = os.environ.get("DEMO_OPERATOR_EMAIL", "operator@demo.local").strip().casefold()
    reviewer_email = os.environ.get("DEMO_REVIEWER_EMAIL", "reviewer@demo.local").strip().casefold()
    db = SessionLocal()
    try:
        organizations = {}
        for slug, name in (("demo-a", "Demo Organization A"), ("demo-b", "Demo Organization B")):
            org = db.scalar(select(Organization).where(Organization.slug == slug))
            if org is None:
                org = Organization(slug=slug, name=name)
                db.add(org)
                db.flush()
            organizations[slug] = org

        users = {}
        for email, role in ((operator_email, "operator"), (reviewer_email, "reviewer")):
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, password_hash=hash_password(password), active=True)
                db.add(user)
                db.flush()
            elif not verify_password(password, user.password_hash):
                user.password_hash = hash_password(password)
            users[email] = (user, role)

        for org in organizations.values():
            for user, role in users.values():
                membership = db.scalar(
                    select(Membership).where(Membership.user_id == user.id, Membership.org_id == org.id)
                )
                if membership is None:
                    db.add(Membership(user_id=user.id, org_id=org.id, role=role))
            supplier = db.scalar(
                select(Supplier).where(
                    Supplier.org_id == org.id, Supplier.tax_id_normalized == "DEMO-SUP-001"
                )
            )
            if supplier is None:
                supplier = Supplier(org_id=org.id, tax_id_normalized="DEMO-SUP-001", name="Demo Supplier")
                db.add(supplier)
                db.flush()
            rows = [
                ("PO-DEMO-001", "KB-001", "Keyboard", "piece", "10", "200000"),
                ("PO-DEMO-002", "KB-001", "Keyboard", "piece", "10", "200000"),
                ("PO-DEMO-003", "MOUSE-001", "Mouse", "piece", "5", "120000"),
            ]
            manifest_path = Path(__file__).resolve().parents[2] / "data" / "demo" / "fixture_manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for sample in manifest.get("fixtures", []):
                    po = sample.get("po_reference") or {}
                    line = po.get("line") or {}
                    number = po.get("po_number")
                    if number:
                        rows.append(
                            (
                                number,
                                line.get("sku") or "PAPER-001",
                                line.get("description") or "Synthetic demo item",
                                line.get("unit") or "piece",
                                line.get("quantity") or "1",
                                line.get("unit_price") or "1",
                            )
                        )
            for number, sku, desc, unit, quantity, price in rows:
                po = db.scalar(
                    select(PurchaseOrder).where(
                        PurchaseOrder.org_id == org.id, PurchaseOrder.po_number == number
                    )
                )
                if po is None:
                    po = PurchaseOrder(
                        org_id=org.id, supplier_id=supplier.id, po_number=number, currency="VND", state="OPEN"
                    )
                    db.add(po)
                    db.flush()
                line = db.scalar(
                    select(PurchaseOrderLine).where(
                        PurchaseOrderLine.org_id == org.id,
                        PurchaseOrderLine.po_id == po.id,
                        PurchaseOrderLine.line_no == 1,
                    )
                )
                if line is None:
                    db.add(
                        PurchaseOrderLine(
                            org_id=org.id,
                            po_id=po.id,
                            line_no=1,
                            sku=sku,
                            description=desc,
                            unit=unit,
                            quantity=Decimal(quantity),
                            unit_price=Decimal(price),
                        )
                    )
            seed_policy_corpus(db, org.id)
        db.commit()
        print("Demo seed complete for organizations demo-a and demo-b.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
