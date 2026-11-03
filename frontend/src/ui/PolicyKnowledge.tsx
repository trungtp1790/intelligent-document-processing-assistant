import { FormEvent, useEffect, useState } from "react";
import { api, ApiError, Session } from "../api";

type PolicySource = {
  slug: string;
  title: string;
  version: string;
  source_sha256: string;
  chunk_count: number;
  synthetic: boolean;
};

type PolicyCitation = {
  citation_id: string;
  source_slug: string;
  source_title: string;
  source_version: string;
  source_sha256: string;
  section: string;
  page_no: number;
  quote: string;
  content_sha256: string;
  synthetic: boolean;
};

type PolicyAnswer = {
  status: "answered" | "insufficient_evidence";
  answer: string;
  citations: PolicyCitation[];
  synthetic: boolean;
  generation: "extractive";
};

const examples = [
  "Cần làm gì khi số lượng hóa đơn khác PO?",
  "Operator có quyền phê duyệt hóa đơn không?",
  "Sửa dữ liệu rồi có giữ lịch sử phiên bản không?",
];

function messageFor(error: unknown): string {
  if (error instanceof ApiError) return error.message + " (" + error.code + ")";
  return error instanceof Error ? error.message : "Không thể tải dữ liệu hướng dẫn.";
}

export function PolicyKnowledge({ session }: { session: Session }) {
  const [sources, setSources] = useState<PolicySource[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [sourcesError, setSourcesError] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<PolicyAnswer | null>(null);
  const [queryError, setQueryError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    setSourcesLoading(true);
    setSourcesError("");
    void api<{ sources: PolicySource[]; synthetic: boolean }>(session, "/policies/sources")
      .then(result => { if (active) setSources(result.sources); })
      .catch(error => { if (active) setSourcesError(messageFor(error)); })
      .finally(() => { if (active) setSourcesLoading(false); });
    return () => { active = false; };
  }, [session]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setQueryError("");
    setAnswer(null);
    setBusy(true);
    try {
      const result = await api<PolicyAnswer>(session, "/policies/query", {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      setAnswer(result);
    } catch (error) {
      setQueryError(messageFor(error));
    } finally {
      setBusy(false);
    }
  }

  return <div className="policy-workspace">
    <section className="policy-intro">
      <div>
        <p className="eyebrow">THƯ VIỆN HƯỚNG DẪN DEMO</p>
        <h2>Tra cứu theo nguồn</h2>
        <p>Hệ thống tìm trong các đoạn hướng dẫn của tổ chức và hiển thị trích dẫn đúng phiên bản.</p>
      </div>
      <span className="policy-demo-mark">DỮ LIỆU TỔNG HỢP</span>
    </section>

    <div className="policy-columns">
      <section className="policy-query-card" aria-labelledby="policy-query-title">
        <p className="eyebrow">TRA CỨU</p>
        <h3 id="policy-query-title">Bạn cần tìm hướng dẫn nào?</h3>
        <form className="policy-query-form" onSubmit={event => void submit(event)}>
          <label htmlFor="policy-question">Câu hỏi</label>
          <textarea
            id="policy-question"
            value={question}
            maxLength={500}
            rows={4}
            placeholder="Ví dụ: Cần làm gì khi số lượng trên hóa đơn khác PO?"
            onChange={event => setQuestion(event.target.value)}
          />
          <div className="policy-query-controls">
            <span>{question.length}/500</span>
            <button className="primary" type="submit" disabled={busy || question.trim().length < 3}>
              {busy ? "Đang tìm nguồn…" : "Tìm trong hướng dẫn"}
            </button>
          </div>
        </form>
        {queryError && <p className="banner-error" role="alert">{queryError}</p>}
        <div className="policy-examples">
          <span className="eyebrow">CÂU HỎI GỢI Ý</span>
          <div>{examples.map(example => <button key={example} type="button" onClick={() => setQuestion(example)}>{example}</button>)}</div>
        </div>
      </section>

      <aside className="policy-sources-card" aria-labelledby="policy-sources-title">
        <p className="eyebrow">NGUỒN ĐANG DÙNG</p>
        <h3 id="policy-sources-title">Tài liệu của tổ chức</h3>
        {sourcesLoading ? <p className="muted">Đang tải danh mục nguồn…</p>
          : sourcesError ? <p className="inline-error" role="alert">{sourcesError}</p>
            : sources.length ? <ul className="policy-source-list">{sources.map(source => <li key={source.slug}>
              <span className="policy-source-icon">DEMO</span>
              <span><b>{source.title}</b><small>Phiên bản {source.version} · {source.chunk_count} đoạn</small></span>
            </li>)}</ul>
              : <p className="muted">Tổ chức này chưa có tài liệu hướng dẫn.</p>}
      </aside>
    </div>

    {answer && <section className={"policy-answer-card " + answer.status} aria-live="polite" aria-labelledby="policy-answer-title">
      <div className="policy-answer-head">
        <div><p className="eyebrow">{answer.status === "answered" ? "KẾT QUẢ CÓ TRÍCH DẪN" : "CHƯA ĐỦ NGUỒN"}</p><h3 id="policy-answer-title">{answer.status === "answered" ? "Đoạn hướng dẫn liên quan" : "Chưa tìm thấy căn cứ"}</h3></div>
        <span className="policy-demo-mark">TRÍCH XUẤT · DEMO</span>
      </div>
      <p className="policy-answer-copy">{answer.answer}</p>
      {answer.citations.length > 0 && <ol className="policy-citation-list">{answer.citations.map(citation => <li key={citation.source_slug + ":" + citation.source_version + ":" + citation.page_no + ":" + citation.content_sha256}>
        <div className="policy-citation-title"><span>{citation.citation_id}</span><b>{citation.source_title}</b></div>
        <p className="policy-citation-meta">Phiên bản {citation.source_version} · {citation.section} · Trang {citation.page_no} · Nội dung tổng hợp</p>
        <blockquote>{citation.quote}</blockquote>
        <code>Nguồn SHA-256: {citation.source_sha256} · Đoạn SHA-256: {citation.content_sha256}</code>
      </li>)}</ol>}
      <p className="policy-boundary-note">Tài liệu tổng hợp chỉ hỗ trợ tra cứu. Không thay đổi kết quả đối soát hoặc quyết định phê duyệt.</p>
    </section>}
  </div>;
}
