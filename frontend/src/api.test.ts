import { afterEach, expect, it, vi } from "vitest";
import { protectedBlob, type Session } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("preserves the API error code and HTTP status for private document previews", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ error: { code: "NOT_FOUND", message: "Không tìm thấy tài liệu." } }),
    }),
  );

  await expect(
    protectedBlob({ access_token: "test-token" } as Session, "/documents/document-id/content"),
  ).rejects.toMatchObject({ code: "NOT_FOUND", status: 404, message: "Không tìm thấy tài liệu." });
});
