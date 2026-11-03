from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent read-only MCP client for the local IDP demo.")
    parser.add_argument("--url", default=os.getenv("IDP_MCP_URL", "http://localhost:5173/api/v1/mcp"))
    parser.add_argument("--limit", type=int, default=10, help="Maximum invoice rows to request (1–100).")
    parser.add_argument("--invoice-id", help="Optionally call get_invoice_summary for one invoice UUID.")
    return parser


async def _run(url: str, token: str, limit: int, invoice_id: str | None) -> int:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client:
        transport = streamable_http_client(url, http_client=http_client)
        async with Client(transport) as client:
            tools = await client.list_tools()
            print("Available read-only tools:")
            for tool in tools.tools:
                print(f"- {tool.name}: {tool.description or tool.title or ''}")

            if limit < 1 or limit > 100:
                raise ValueError("--limit must be between 1 and 100.")
            result = await client.call_tool("list_invoices", {"limit": limit})
            if result.is_error:
                print("list_invoices failed:", _result_json(result), file=sys.stderr)
                return 1
            print("\nlist_invoices result:")
            print(_result_json(result))

            if invoice_id:
                summary = await client.call_tool("get_invoice_summary", {"invoice_id": invoice_id})
                if summary.is_error:
                    print("get_invoice_summary failed:", _result_json(summary), file=sys.stderr)
                    return 1
                print("\nget_invoice_summary result:")
                print(_result_json(summary))
    return 0


def _result_json(result: object) -> str:
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def main() -> int:
    args = _parser().parse_args()
    token = os.getenv("IDP_MCP_TOKEN", "")
    if not token:
        print("Set IDP_MCP_TOKEN in the environment; the client never prompts for or prints it.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args.url, token, args.limit, args.invoice_id))
    except (httpx2.HTTPError, OSError, ValueError) as exc:
        print(f"MCP request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
