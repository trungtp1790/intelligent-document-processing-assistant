from __future__ import annotations

import argparse
import getpass
import re
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import validate_runtime_settings
from app.db.models import Membership, Organization, User
from app.db.session import SessionLocal
from app.security import hash_password
from app.services.common import audit

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+\Z")
_ALLOWED_ROLES = {"operator", "reviewer"}
_MIN_PASSWORD_LENGTH = 12


def normalize_org(slug: str, name: str) -> tuple[str, str]:
    normalized_slug = slug.strip().casefold()
    normalized_name = name.strip()
    if len(normalized_slug) > 64 or not _SLUG.fullmatch(normalized_slug):
        raise ValueError("Organization slug must use lowercase letters, digits, and single hyphens")
    if not normalized_name or len(normalized_name) > 200:
        raise ValueError("Organization name must contain 1 to 200 characters")
    return normalized_slug, normalized_name


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 320 or not _EMAIL.fullmatch(normalized):
        raise ValueError("Email address is invalid")
    return normalized


def provision_organization(db: Session, slug: str, name: str) -> tuple[UUID, bool]:
    normalized_slug, normalized_name = normalize_org(slug, name)
    existing = db.scalar(select(Organization).where(Organization.slug == normalized_slug).with_for_update())
    if existing is not None:
        if existing.name != normalized_name:
            raise ValueError("Organization slug already exists with a different name")
        return existing.id, False

    organization = Organization(slug=normalized_slug, name=normalized_name)
    db.add(organization)
    db.flush()
    audit(
        db,
        None,
        organization.id,
        "organization",
        organization.id,
        "ORGANIZATION_PROVISIONED",
    )
    return organization.id, True


def provision_user(
    db: Session,
    *,
    email: str,
    org_slug: str,
    role: str,
    password: str | None = None,
) -> tuple[UUID, bool]:
    normalized_email = normalize_email(email)
    normalized_slug, _ = normalize_org(org_slug, org_slug)
    if role not in _ALLOWED_ROLES:
        raise ValueError("Membership role must be operator or reviewer")

    organization = db.scalar(select(Organization).where(Organization.slug == normalized_slug))
    if organization is None:
        raise ValueError("Organization does not exist")
    user = db.scalar(select(User).where(User.email == normalized_email).with_for_update())
    if user is not None and not user.active:
        raise ValueError("Inactive users must be reactivated through the approved account recovery process")

    if user is None:
        if password is None or len(password) < _MIN_PASSWORD_LENGTH or len(password) > 256:
            raise ValueError("New users need a password between 12 and 256 characters")
        if not password.strip():
            raise ValueError("Password cannot contain only whitespace")
        user = User(email=normalized_email, password_hash=hash_password(password), active=True)
        db.add(user)
        db.flush()

    membership = db.scalar(
        select(Membership).where(
            Membership.org_id == organization.id,
            Membership.user_id == user.id,
        )
    )
    if membership is not None:
        if membership.role != role:
            raise ValueError("User already has a different role in this organization")
        return user.id, False

    db.add(Membership(user_id=user.id, org_id=organization.id, role=role))
    audit(
        db,
        None,
        organization.id,
        "user",
        user.id,
        "USER_MEMBERSHIP_PROVISIONED",
        metadata={"role": role},
    )
    return user.id, True


def _password_for_new_user(email: str) -> str | None:
    with SessionLocal() as db:
        existing = db.scalar(select(User.id).where(User.email == email))
    if existing is not None:
        return None

    password = getpass.getpass("New user password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    if len(password) < _MIN_PASSWORD_LENGTH or len(password) > 256 or not password.strip():
        raise ValueError("Password must contain 12 to 256 non-whitespace characters")
    return password


def _create_org(slug: str, name: str) -> int:
    with SessionLocal.begin() as db:
        organization_id, created = provision_organization(db, slug, name)
    print(f"Organization {'created' if created else 'already exists'}: {organization_id}")
    return 0


def _create_user(email: str, org_slug: str, role: str) -> int:
    normalized_email = normalize_email(email)
    with SessionLocal() as db:
        statement = select(Organization.id).where(Organization.slug == org_slug.strip().casefold())
        organization = db.scalar(statement)
        if organization is None:
            raise ValueError("Organization does not exist")
    password = _password_for_new_user(normalized_email)
    with SessionLocal.begin() as db:
        user_id, created = provision_user(
            db,
            email=normalized_email,
            org_slug=org_slug,
            role=role,
            password=password,
        )
    print(f"User membership {'created' if created else 'already exists'}: {user_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision production organizations and memberships.")
    commands = parser.add_subparsers(dest="command", required=True)
    create_org = commands.add_parser("create-org", help="create an organization")
    create_org.add_argument("--slug", required=True)
    create_org.add_argument("--name", required=True)
    create_user = commands.add_parser("create-user", help="create an operator or reviewer membership")
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--org", required=True)
    create_user.add_argument("--role", choices=sorted(_ALLOWED_ROLES), required=True)
    args = parser.parse_args(argv)

    try:
        validate_runtime_settings()
        if args.command == "create-org":
            return _create_org(args.slug, args.name)
        return _create_user(args.email, args.org, args.role)
    except (IntegrityError, RuntimeError, ValueError) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
