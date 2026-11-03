import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "../api";
import { PolicyKnowledge } from "./PolicyKnowledge";

const session: Session = {
  access_token: "test-token",
  user_id: "user-a",
  org_id: "org-a",
  org_slug: "demo-a",
  org_name: "Demo Organization A",
  role: "operator",
  expires_in: 900,
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/policies/sources")) {
      return new Response(JSON.stringify({
        synthetic: true,
        sources: [{
          slug: "discrepancy-handling",
          title: "[DEMO] Quy trình xử lý chênh lệch hóa đơn và PO",
          version: "1.0",
          source_sha256: "a".repeat(64),
          chunk_count: 2,
          synthetic: true,
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify({
      status: "answered",
      answer: "Tìm thấy đoạn hướng dẫn tổng hợp có liên quan.",
      generation: "extractive",
      synthetic: true,
      citations: [{
        citation_id: "S1",
        source_slug: "discrepancy-handling",
        source_title: "[DEMO] Quy trình xử lý chênh lệch hóa đơn và PO",
        source_version: "1.0",
        source_sha256: "a".repeat(64),
        section: "So sánh dòng hàng",
        page_no: 1,
        quote: "Kiểm tra số lượng trên hóa đơn với purchase order.",
        content_sha256: "b".repeat(64),
        synthetic: true,
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PolicyKnowledge", () => {
  it("shows source versions and the exact cited passage after a query", async () => {
    render(<PolicyKnowledge session={session} />);
    expect(await screen.findByText("[DEMO] Quy trình xử lý chênh lệch hóa đơn và PO")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Câu hỏi"), {
      target: { value: "Cần làm gì khi số lượng hóa đơn khác PO?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Tìm trong hướng dẫn" }));

    expect(await screen.findByText("Kiểm tra số lượng trên hóa đơn với purchase order.")).toBeInTheDocument();
    expect(screen.getByText("Phiên bản 1.0 · So sánh dòng hàng · Trang 1 · Nội dung tổng hợp")).toBeInTheDocument();
    expect(screen.getByText("TRÍCH XUẤT · DEMO")).toBeInTheDocument();
  });

  it("shows an explicit no-answer state without inventing a citation", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/policies/sources")) {
        return new Response(JSON.stringify({ synthetic: true, sources: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({
        status: "insufficient_evidence",
        answer: "Chưa tìm thấy đủ nguồn trong bộ hướng dẫn demo để trả lời.",
        generation: "extractive",
        synthetic: true,
        citations: [],
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));

    render(<PolicyKnowledge session={session} />);
    fireEvent.change(screen.getByLabelText("Câu hỏi"), {
      target: { value: "Một câu hỏi ngoài phạm vi tài liệu demo" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Tìm trong hướng dẫn" }));

    expect(await screen.findByText("Chưa tìm thấy căn cứ")).toBeInTheDocument();
    expect(screen.getByText("Chưa tìm thấy đủ nguồn trong bộ hướng dẫn demo để trả lời.")).toBeInTheDocument();
    expect(screen.queryByText("S1")).not.toBeInTheDocument();
  });
});
