import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, Session } from "../api";

type CopilotStatus = {
  enabled: boolean;
  provider: "foundry" | null;
  max_rounds: number;
  max_tool_calls: number;
  synthetic: false;
  requires_live_provider: true;
};

type ToolTrace = { round: number; call_id: string; tool: string; status: string; duration_ms: number };
type CopilotReply = {
  status: "answered" | "budget_exhausted";
  answer: string;
  synthetic: boolean;
  provider: string;
  trace: {
    model_rounds: { round: number; finish_reason: string; latency_ms: number; input_tokens: number | null; output_tokens: number | null }[];
    tool_calls: ToolTrace[];
    tool_call_count: number;
    tool_executions: number;
    input_tokens: number | null;
    output_tokens: number | null;
    elapsed_ms: number;
    stop_reason: string;
  };
};
type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  synthetic?: boolean;
  trace?: CopilotReply["trace"];
};

const examples = ["Vì sao hồ sơ này bị chặn?", "Dữ liệu nào chưa được xác minh?", "Có cảnh báo nào cần reviewer xem không?"];
const toolLabels: Record<string, string> = {
  get_invoice_summary: "Đọc dữ liệu hóa đơn",
  get_reconciliation: "Đọc kết quả đối soát",
  get_purchase_order: "Đọc đơn đặt hàng đã liên kết",
  get_invoice_evidence: "Đọc dẫn chứng theo trang",
  search_policy: "Tra cứu hướng dẫn có nguồn",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message} (${error.code})`;
  return error instanceof Error ? error.message : "Không thể gửi câu hỏi.";
}

function toolStatus(status: string): string {
  if (status === "success") return "Đã đọc";
  if (status === "budget_denied") return "Đã chạm giới hạn";
  if (status === "denied") return "Bị từ chối";
  return "Không thực hiện";
}

export function InvoiceCopilot({ session, invoiceId }: { session: Session; invoiceId: string }) {
  const [availability, setAvailability] = useState<CopilotStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    void api<CopilotStatus>(session, "/copilot/status")
      .then(result => { if (active) setAvailability(result); })
      .catch(cause => { if (active) setStatusError(errorMessage(cause)); });
    return () => { active = false; };
  }, [session]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const submitted = question.trim();
    if (!submitted || busy) return;
    const previous = messages;
    setQuestion(""); setError(""); setBusy(true);
    setMessages([...previous, { role: "user", content: submitted }]);
    try {
      const result = await api<CopilotReply>(session, `/invoices/${invoiceId}/copilot`, {
        method: "POST",
        body: JSON.stringify({
          question: submitted,
          history: previous.slice(-8).map(item => ({ role: item.role, content: item.content })),
        }),
      });
      setMessages(current => [...current, {
        role: "assistant", content: result.answer, synthetic: result.synthetic, trace: result.trace,
      }]);
    } catch (cause) {
      setError(errorMessage(cause));
      setMessages(previous);
      setQuestion(submitted);
    } finally { setBusy(false); }
  }

  return <section className="copilot-card" aria-labelledby="invoice-copilot-title">
    <div className="copilot-heading">
      <div><p className="eyebrow">HỖ TRỢ REVIEW</p><h3 id="invoice-copilot-title">Trợ lý giải thích hóa đơn</h3></div>
      <span className="copilot-readonly">Chỉ đọc</span>
    </div>
    {statusError ? <p className="inline-error" role="alert">Không kiểm tra được trạng thái trợ lý: {statusError}</p>
      : !availability ? <p className="muted">Đang kiểm tra cấu hình trợ lý…</p>
        : !availability.enabled ? <div className="copilot-disabled" role="status">
          <b>Trợ lý AI chưa được bật trong môi trường này.</b>
          <p>Để tránh tạo câu trả lời giả, chat chỉ mở khi một model live được cấu hình. Fixture DEMO, màn review và tra cứu hướng dẫn vẫn chạy không cần API key.</p>
        </div> : <>
          <p className="copilot-disclaimer">Trợ lý dùng model Foundry đã cấu hình và chỉ được đọc hóa đơn đang mở, đối soát, PO liên kết, dẫn chứng cùng hướng dẫn. Kết quả không thay thế reviewer.</p>
          <div className="copilot-messages" role="log" aria-label="Trao đổi với trợ lý" aria-live="polite">
            {!messages.length && <div className="copilot-welcome"><b>Hỏi về hồ sơ này</b><div>{examples.map(example => <button type="button" key={example} onClick={() => setQuestion(example)}>{example}</button>)}</div></div>}
            {messages.map((message, index) => <article className={`copilot-message ${message.role}`} key={`${index}-${message.role}`}>
              <span>{message.role === "user" ? "Bạn" : "Trợ lý"}{message.role === "assistant" && message.synthetic ? " · Hồ sơ DEMO" : ""}</span>
              <p>{message.content}</p>
              {message.trace && <details className="copilot-trace">
                <summary>Luồng xử lý · {message.trace.model_rounds.length} lượt · {message.trace.tool_executions} lần đọc · {message.trace.elapsed_ms} ms</summary>
                <ul>{message.trace.tool_calls.map((call, callIndex) => <li key={`${call.round}-${call.call_id}-${callIndex}`}>
                  <span>{toolLabels[call.tool] ?? "Tool không xác định"}</span><small>{toolStatus(call.status)} · lượt {call.round} · {call.duration_ms} ms</small>
                </li>)}</ul>
                <small>Token đầu vào / đầu ra: {message.trace.input_tokens ?? "—"} / {message.trace.output_tokens ?? "—"}.</small>
              </details>}
            </article>)}
            {busy && <p className="muted copilot-wait" role="status">Đang đọc dữ liệu và chuẩn bị giải thích…</p>}
          </div>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <form className="copilot-form" onSubmit={event => void submit(event)}>
            <label htmlFor="copilot-question">Câu hỏi</label>
            <textarea id="copilot-question" value={question} maxLength={1000} rows={3} disabled={busy} placeholder="Ví dụ: Vì sao kết quả đối soát đang bị chặn?" onChange={event => setQuestion(event.target.value)} />
            <div><small>Model live · tối đa {availability.max_rounds} lượt và {availability.max_tool_calls} lần đọc</small><button className="primary" type="submit" disabled={busy || question.trim().length < 3}>{busy ? "Đang xử lý…" : "Gửi câu hỏi"}</button></div>
          </form>
        </>}
  </section>;
}
