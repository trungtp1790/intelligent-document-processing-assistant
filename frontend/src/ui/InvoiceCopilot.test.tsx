import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Session } from "../api";
import { InvoiceCopilot } from "./InvoiceCopilot";

const session: Session = {
  access_token: "test-token",
  user_id: "user-a",
  org_id: "org-a",
  org_slug: "demo-a",
  org_name: "Demo Organization A",
  role: "operator",
  expires_in: 900,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("InvoiceCopilot", () => {
  it("clearly explains when chat is disabled and does not invent a demo answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      enabled: false,
      provider: null,
      max_rounds: 4,
      max_tool_calls: 6,
      synthetic: false,
      requires_live_provider: true,
    }), { status: 200, headers: { "Content-Type": "application/json" } })));

    render(<InvoiceCopilot session={session} invoiceId="invoice-1" />);
    expect(await screen.findByText("Trợ lý AI chưa được bật trong môi trường này.")).toBeInTheDocument();
    expect(screen.getByText(/Fixture DEMO, màn review và tra cứu hướng dẫn vẫn chạy không cần API key/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Câu hỏi")).not.toBeInTheDocument();
  });

  it("shows live read-only tool trace after a chat turn", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input).endsWith("/copilot/status")) {
        return new Response(JSON.stringify({
          enabled: true,
          provider: "foundry",
          max_rounds: 4,
          max_tool_calls: 6,
          synthetic: false,
          requires_live_provider: true,
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({
        status: "answered",
        answer: "Kết quả đối soát có một cảnh báo cần reviewer kiểm tra.",
        synthetic: true,
        provider: "foundry",
        trace: {
          model_rounds: [{ round: 1, finish_reason: "tool_calls", latency_ms: 160, input_tokens: 20, output_tokens: 5 }],
          tool_calls: [{ round: 1, call_id: "call-1", tool: "get_reconciliation", status: "success", duration_ms: 3 }],
          tool_call_count: 1,
          tool_calls_requested: 1,
          tool_executions: 1,
          input_tokens: 20,
          output_tokens: 5,
          elapsed_ms: 200,
          stop_reason: "completed",
        },
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<InvoiceCopilot session={session} invoiceId="invoice-1" />);
    const input = await screen.findByLabelText("Câu hỏi");
    fireEvent.change(input, { target: { value: "Vì sao hồ sơ này có cảnh báo?" } });
    fireEvent.click(screen.getByRole("button", { name: "Gửi câu hỏi" }));

    expect(await screen.findByText("Kết quả đối soát có một cảnh báo cần reviewer kiểm tra.")).toBeInTheDocument();
    expect(screen.getByText("Trợ lý · Hồ sơ DEMO")).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Luồng xử lý/));
    expect(screen.getByText("Đọc kết quả đối soát")).toBeInTheDocument();
    const chatRequest = fetchMock.mock.calls.find(call => String(call[0]).endsWith("/invoices/invoice-1/copilot"));
    expect(chatRequest).toBeDefined();
    expect(String(chatRequest?.[1]?.body)).toContain('"question":"Vì sao hồ sơ này có cảnh báo?"');
  });
});
