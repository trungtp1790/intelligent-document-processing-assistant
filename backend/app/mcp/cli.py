from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import select

from app.db.models import Organization
from app.db.session import SessionLocal
from app.mcp.tokens import MCP_TOKEN_SCOPES, issue_mcp_token, revoke_mcp_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue or revoke local IDP read-only MCP service tokens.")
    commands = parser.add_subparsers(dest="command", required=True)

    issue = commands.add_parser("issue", help="Issue a scoped token (the plaintext is printed once).")
    issue.add_argument("--org", required=True, help="Organization slug, for example demo-a.")
    issue.add_argument("--name", required=True, help="Short label for the token.")
    issue.add_argument("--scope", action="append", required=True, choices=sorted(MCP_TOKEN_SCOPES))
    issue.add_argument("--expires-in-days", type=int, default=30)

    revoke = commands.add_parser("revoke", help="Revoke a token by its token ID.")
    revoke.add_argument("--token-id", required=True, type=UUID)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "issue":
        with SessionLocal() as db:
            organization = db.scalar(
                select(Organization).where(Organization.slug == args.org.strip().casefold())
            )
            if organization is None:
                print("Organization not found.")
                return 2
            try:
                plaintext, record = issue_mcp_token(
                    db,
                    org_id=organization.id,
                    name=args.name,
                    scopes=args.scope,
                    expires_in_days=args.expires_in_days,
                )
            except ValueError as exc:
                print(str(exc))
                return 2
        print("Token ID:", record.id)
        print("Organization:", args.org.strip().casefold())
        print("Scopes:", ", ".join(record.scopes))
        print("Expires:", record.expires_at.isoformat() if record.expires_at else "never")
        print("Token (shown once; store securely):", plaintext)
        return 0

    if args.command == "revoke":
        with SessionLocal() as db:
            revoked = revoke_mcp_token(db, args.token_id)
        if not revoked:
            print("Token not found or already revoked.")
            return 1
        print("Token revoked:", args.token_id)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
