from __future__ import annotations

import anyio
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

from app.mcp.tokens import McpPrincipal, authenticate_mcp_token, mcp_principal

MCP_ENDPOINT_PATH = "/api/v1/mcp"


class McpBearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") != MCP_ENDPOINT_PATH:
            await self.app(scope, receive, send)
            return

        authorization_values = [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == b"authorization"
        ]
        authorization = authorization_values[0] if len(authorization_values) == 1 else ""
        scheme, separator, plaintext = authorization.partition(" ")
        if (
            scheme.casefold() != "bearer"
            or not separator
            or not plaintext
            or any(c.isspace() for c in plaintext)
        ):
            await self._unauthorized(scope, receive, send)
            return

        try:
            principal = await anyio.to_thread.run_sync(authenticate_mcp_token, plaintext)
        except SQLAlchemyError:
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "AUTH_DEPENDENCY_UNAVAILABLE",
                        "message": "Authentication is temporarily unavailable.",
                    }
                },
            )
            await response(scope, receive, send)
            return
        if principal is None:
            await self._unauthorized(scope, receive, send)
            return

        context_token = mcp_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            mcp_principal.reset(context_token)

    @staticmethod
    async def _unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "UNAUTHENTICATED",
                    "message": "A valid MCP bearer token is required.",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def require_mcp_principal(scope_name: str) -> McpPrincipal:
    principal = mcp_principal.get()
    if principal is None:
        raise PermissionError("Authentication required.")
    if scope_name not in principal.scopes:
        raise PermissionError("Token does not have the required read scope.")
    return principal
