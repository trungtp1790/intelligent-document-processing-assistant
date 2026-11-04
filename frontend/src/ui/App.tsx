import { FormEvent, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { API_URL, api, ApiError, idempotencyKey, protectedBlob, Session } from "../api";
import { FixtureBadge } from "./FixtureBadge";
import { PolicyKnowledge } from "./PolicyKnowledge";
import { InvoiceCopilot } from "./InvoiceCopilot";

type Fixture = { file: string; display_name: string; label: string; synthetic: boolean; expected_findings: string[]; content_type: string };
type PO = {
  id: string; po_number: string; supplier_id: string; supplier_tax_id: string;
  supplier_name: string; currency: string; state: string;
  lines: { id: string; line_no: number; sku: string | null; description: string; unit: string; quantity: string; unit_price: string }[];
};
type Issue = { code: string; severity: "BLOCK" | "WARN"; field_path?: string };
type Evidence = {
  field_path: string; page_no: number | null; raw_value: string | null; normalized_value: string | null;
  raw_text: string | null; bbox: Record<string, unknown> | null; confidence: number | null;
  normalization_issue: string | null; origin: string; verification_state: string;
  actor_id: string | null; note: string | null;
};
type DocumentPageRef = { page_no: number; page_hash: string; preview_url: string };
type InvoiceRow = {
  id: string; document_id: string; filename: string; mime: string; pages: number;
  invoice_number: string | null; invoice_date: string | null; supplier_name: string | null;
  supplier_tax_id: string | null; grand_total: string | null; currency: string | null;
  workflow_status: string; version_id: string | null; version_no: number;
  snapshot_hash: string | null; fixture_mode: boolean; provider: string | null;
};
type InvoiceDetail = InvoiceRow & {
  content_url: string;
  document_pages: DocumentPageRef[];
  job: { id: string; status: string; step: string; attempts: number; error_code: string | null } | null;
  version: { id: string; version_no: number; snapshot_hash: string; origin: string; reason: string | null; data: Draft } | null;
  evidence: Evidence[];
  reconciliation: { id: string; status: string; issues: Issue[]; mapping: Record<string, string>; po_id: string; snapshot_hash: string; policy_version: string } | null;
  allowed_actions: { edit: boolean; reconcile: boolean; approve: boolean; reject: boolean; export: boolean };
};
type LineDraft = { line_id?: string | null; sku: string | null; description: string | null; unit: string | null; quantity: string | null; unit_price: string | null; net_amount: string | null };
type Draft = {
  schema_version: string; invoice_number: string | null; invoice_series: string | null;
  invoice_series_absent_confirmed: boolean; invoice_date: string | null; supplier_tax_id: string | null;
  supplier_name: string | null; po_number: string | null; currency: string | null;
  subtotal: string | null; tax_total: string | null; grand_total: string | null;
  discount_total: string | null; shipping_total: string | null; lines: LineDraft[];
  document_id?: string;
};

const statusText: Record<string, string> = {
  UPLOADED: "Đã nhận", PROCESSING: "Đang trích xuất", PROCESSING_FAILED: "Xử lý thất bại",
  REVIEW_REQUIRED: "Cần kiểm tra", READY_FOR_APPROVAL: "Chờ người duyệt",
  APPROVED: "Đã duyệt nội bộ", REJECTED: "Đã từ chối",
};
const reconciliationStatusText: Record<string, string> = {
  PASS: "Đạt", WARNING: "Có cảnh báo", BLOCKED: "Bị chặn",
};
const issueText: Record<string, string> = {
  REQUIRED_FIELD_MISSING: "Thiếu trường bắt buộc", AMBIGUOUS_NUMBER: "Số chưa rõ cách đọc",
  AMBIGUOUS_DATE: "Ngày chưa rõ cách đọc", UNSUPPORTED_CASE: "Trường hợp ngoài phạm vi M0/M1",
  INVALID_NUMBER: "Giá trị số không đúng định dạng", NUMERIC_OVERFLOW: "Giá trị vượt giới hạn số cho phép",
  INVALID_DATE: "Ngày không hợp lệ", UNSUPPORTED_LOCALE: "Chưa hỗ trợ locale chuẩn hóa",
  SOURCE_PAGE_MISSING: "Thiếu trang nguồn — reviewer phải xác minh trước khi duyệt",
  SOURCE_PAGE_INVALID: "Trang nguồn không hợp lệ",
  SUPPLIER_MISMATCH: "Nhà cung cấp không khớp PO", PO_NOT_FOUND: "Không tìm thấy PO",
  PO_CLOSED: "PO đã đóng", LINE_MAPPING_REQUIRED: "Cần ánh xạ dòng hàng",
  QUANTITY_MISMATCH: "Số lượng không khớp", UNIT_PRICE_MISMATCH: "Đơn giá không khớp",
  UNIT_MISMATCH: "Đơn vị tính không khớp", LINE_AMOUNT_MISMATCH: "Thành tiền dòng không khớp",
  SUBTOTAL_MISMATCH: "Tạm tính không khớp tổng dòng", GRAND_TOTAL_MISMATCH: "Tổng cộng không khớp",
  DUPLICATE_APPROVED_INVOICE: "Hóa đơn trùng đã được duyệt", DUPLICATE_PENDING_INVOICE: "Số hóa đơn trùng với hồ sơ đang xử lý",
  DESCRIPTION_DIFFERENCE: "Mô tả hàng hóa khác", SOURCE_NOT_TEXT_VERIFIED: "Chưa xác minh nội dung chữ trên ảnh scan",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "FIXTURE_NOT_FOUND") {
      return "Tệp này không nằm trong bộ mẫu DEMO. Hãy chọn một mẫu có sẵn bên dưới; tệp riêng cần nhà cung cấp trích xuất thật.";
    }
    if (error.code === "MANUAL_SOURCE_PAGE_REQUIRED") {
      return "Hãy nhập trang bạn đã đối chiếu cho giá trị vừa chỉnh sửa.";
    }
    return `${error.message}${error.code ? ` (${error.code})` : ""}`;
  }
  return error instanceof Error ? error.message : "Đã xảy ra lỗi.";
}

function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [email, setEmail] = useState("operator@demo.local");
  const [password, setPassword] = useState("");
  const [org, setOrg] = useState("demo-a");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      const result = await api<Session>(null, "/auth/login", { method: "POST", body: JSON.stringify({ email, password, org_slug: org }) });
      onLogin(result);
    } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  }
  return <main className="login-shell">
    <header className="login-brandline"><span className="brand-mark">ID</span><span><b>IDP Assistant</b><small>Quản lý hồ sơ hóa đơn</small></span></header>
    <div className="login-content">
      <section className="login-context">
        <p className="eyebrow">QUẢN LÝ HÓA ĐƠN</p>
        <h2>Theo dõi hóa đơn từ lúc tiếp nhận đến khi được duyệt.</h2>
        <p>Kiểm tra dữ liệu trên chứng từ gốc, đối chiếu với đơn đặt hàng và lưu lại từng lần chỉnh sửa.</p>
        <ol className="process-list">
          <li><span>01</span><div><b>Tiếp nhận</b><small>Lưu tệp vào hồ sơ của tổ chức</small></div></li>
          <li><span>02</span><div><b>Kiểm tra</b><small>So sánh dữ liệu với chứng từ gốc</small></div></li>
          <li><span>03</span><div><b>Phê duyệt nội bộ</b><small>Người duyệt quyết định sau đối soát</small></div></li>
        </ol>
      </section>
      <section className="login-card">
        <p className="eyebrow">TÀI KHOẢN NỘI BỘ</p>
        <h1>Đăng nhập hệ thống</h1>
        <p className="muted">Dùng tài khoản local đã seed. Mật khẩu nằm trong file <code>.env</code>.</p>
        <FixtureBadge />
        <form onSubmit={submit} className="stack form-login">
          <label>Email<input type="email" autoComplete="username" required value={email} onChange={e => setEmail(e.target.value)} /></label>
          <label>Mật khẩu<input type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /></label>
          <label>Tổ chức<select value={org} onChange={e => setOrg(e.target.value)}><option value="demo-a">Demo Organization A</option><option value="demo-b">Demo Organization B</option></select></label>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button className="primary full" disabled={busy}>{busy ? "Đang đăng nhập…" : "Đăng nhập"}</button>
        </form>
        <p className="microcopy">Các mẫu có nhãn DEMO là dữ liệu tổng hợp. Không cần API key.</p>
      </section>
    </div>
    <footer className="login-footer">Quyết định cuối cùng thuộc về người duyệt.</footer>
  </main>;
}

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [invoiceId, setInvoiceId] = useState<string | null>(null);
  const [page, setPage] = useState<"documents" | "orders" | "policies">("documents");
  const queryClient = useQueryClient();
  function signOut() { setSession(null); setInvoiceId(null); queryClient.clear(); }
  if (!session) return <Login onLogin={value => { setSession(value); queryClient.clear(); }} />;
  if (invoiceId) return <InvoiceReview session={session} invoiceId={invoiceId} onBack={() => { setInvoiceId(null); queryClient.invalidateQueries({ queryKey: ["invoices"] }); }} />;
  return <Dashboard session={session} onOpen={setInvoiceId} onSignOut={signOut} page={page} setPage={setPage} />;
}

function Dashboard({ session, onOpen, onSignOut, page, setPage }: {
  session: Session; onOpen: (id: string) => void; onSignOut: () => void;
  page: "documents" | "orders" | "policies"; setPage: (page: "documents" | "orders" | "policies") => void;
}) {
  const client = useQueryClient();
  const invoices = useQuery({ queryKey: ["invoices"], queryFn: () => api<InvoiceRow[]>(session, "/invoices") });
  const fixtures = useQuery({ queryKey: ["fixtures"], queryFn: () => api<Fixture[]>(session, "/demo/fixtures") });
  const orders = useQuery({ queryKey: ["orders"], queryFn: () => api<PO[]>(session, "/purchase-orders") });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showPoForm, setShowPoForm] = useState(false);
  const [importMessage, setImportMessage] = useState("");
  const [poNumber, setPoNumber] = useState("");
  const [poSku, setPoSku] = useState("");
  const [poDescription, setPoDescription] = useState("");
  const [poQuantity, setPoQuantity] = useState("1");
  const [poPrice, setPoPrice] = useState("0");

  async function uploadFile(file: File) {
    setError(""); setBusy(true);
    try {
      const form = new FormData(); form.set("file", file); form.set("locale", "vi-VN");
      const response = await api<{ invoice_id: string }>(session, "/documents", { method: "POST", body: form }, idempotencyKey());
      await client.invalidateQueries({ queryKey: ["invoices"] }); onOpen(response.invoice_id);
    } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  }

  async function uploadFixture(filename: string) {
    setError(""); setBusy(true);
    try {
      const response = await api<{ invoice_id: string }>(session, `/demo/fixtures/${encodeURIComponent(filename)}/upload`, { method: "POST" }, idempotencyKey());
      await client.invalidateQueries({ queryKey: ["invoices"] }); onOpen(response.invoice_id);
    } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  }

  async function createPo(event: FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      await api(session, "/purchase-orders", { method: "POST", body: JSON.stringify({
        po_number: poNumber, supplier_tax_id: "DEMO-SUP-001", currency: "VND", state: "OPEN",
        lines: [{ sku: poSku || null, description: poDescription, unit: "piece", quantity: poQuantity, unit_price: poPrice }],
      }) }, idempotencyKey());
      setShowPoForm(false); setPoNumber(""); setPoSku(""); setPoDescription("");
      await client.invalidateQueries({ queryKey: ["orders"] });
    } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  }

  async function importPoCsv(file: File) {
    setError(""); setImportMessage(""); setBusy(true);
    try {
      const form = new FormData(); form.set("file", file);
      const result = await api<{ imported_count: number }>(session, "/purchase-orders/import", {
        method: "POST", body: form,
      }, idempotencyKey());
      setImportMessage(`Đã nhập ${result.imported_count} PO; dữ liệu đã được lưu trong tổ chức này.`);
      await client.invalidateQueries({ queryKey: ["orders"] });
    } catch (cause) { setError(errorMessage(cause)); } finally { setBusy(false); }
  }

  function downloadPoTemplate() {
    const content = "po_number,supplier_tax_id,currency,line_no,sku,description,unit,quantity,unit_price\n";
    const href = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a"); anchor.href = href; anchor.download = "purchase-orders-template.csv"; anchor.click(); URL.revokeObjectURL(href);
  }

  return <div className="app-shell dashboard-shell">
    <aside className="workspace-sidebar">
      <button className="sidebar-brand" onClick={() => setPage("documents")}>
        <span className="brand-mark small">ID</span>
        <span><b>IDP Assistant</b><small>Hóa đơn &amp; đối soát</small></span>
      </button>
      <p className="sidebar-nav-title">NGHIỆP VỤ</p>
      <nav className="sidebar-nav" aria-label="Điều hướng chính">
        <button className={page === "documents" ? "nav-link active" : "nav-link"} onClick={() => setPage("documents")}>Chứng từ</button>
        <button className={page === "orders" ? "nav-link active" : "nav-link"} onClick={() => setPage("orders")}>Đơn đặt hàng</button>
        <button className={page === "policies" ? "nav-link active" : "nav-link"} onClick={() => setPage("policies")}>Hướng dẫn</button>
      </nav>
      <div className="sidebar-bottom">
        <div className="sidebar-organization"><b>{session.org_name}</b><span>{session.role === "reviewer" ? "Người duyệt" : "Người nhập liệu"}</span></div>
        <button className="sidebar-signout" onClick={onSignOut}>Đăng xuất</button>
      </div>
    </aside>
    <div className="dashboard-main">
      <header className="dashboard-topbar">
        <div className="breadcrumb"><span>Không gian làm việc</span><span aria-hidden="true">/</span><b>{page === "documents" ? "Chứng từ" : page === "orders" ? "Đơn đặt hàng" : "Hướng dẫn"}</b></div>
        <FixtureBadge />
        <span className="topbar-identity">{session.org_name}</span>
      </header>
      <main className="page-wrap">
      <section className="page-heading">
        <div><p className="eyebrow">{session.org_name}</p><h1>{page === "documents" ? "Chứng từ cần xử lý" : page === "orders" ? "Đơn đặt hàng" : "Tra cứu hướng dẫn"}</h1><p className="muted">{page === "policies" ? "Tìm đoạn hướng dẫn tổng hợp có trích dẫn nguồn và phiên bản." : "Tiếp nhận hóa đơn, kiểm tra dữ liệu và đối chiếu với đơn đặt hàng."}</p></div>
        {page === "documents" && <div className="page-actions"><label className={`file-button${fixtures.data?.length ? " file-button-disabled" : ""}`} aria-disabled={Boolean(fixtures.data?.length)} title={fixtures.data?.length ? "Fixture demo chỉ xử lý các mẫu trong thư viện DEMO bên dưới." : undefined}>Thêm hóa đơn<input aria-label="Chọn tệp hóa đơn" type="file" accept="application/pdf,image/png,image/jpeg" disabled={busy || Boolean(fixtures.data?.length)} onChange={e => { const file = e.target.files?.[0]; if (file) void uploadFile(file); e.currentTarget.value = ""; }} /></label><span className="upload-constraints">PDF, PNG hoặc JPEG<br />Tối đa 15 MiB · 10 trang</span></div>}
      </section>

      {error && <div className="banner-error" role="alert">{error}</div>}
      {page === "documents" && Boolean(fixtures.data?.length) && <p className="fixture-upload-note" role="note">Fixture demo dùng dữ liệu tổng hợp và chỉ nhận mẫu trong thư viện bên dưới. Muốn trích xuất chứng từ riêng, cần cấu hình nhà cung cấp thật.</p>}
      {page === "documents" ? <>
        <details className="sample-library">
          <summary>
            <span className="sample-library-mark">DEMO</span>
            <span className="sample-library-copy"><b>Mẫu thử có sẵn</b><small>{fixtures.data?.length ?? 0} mẫu dữ liệu tổng hợp · dùng không cần API key</small></span>
            <span className="sample-library-action">Chọn mẫu <span aria-hidden="true">⌄</span></span>
          </summary>
          <div className="sample-list">{(fixtures.data ?? []).map(sample => <button className="sample-row" key={sample.file} disabled={busy} onClick={() => void uploadFixture(sample.file)}>
            <span className="sample-format">{sample.content_type === "image/png" ? "PNG" : "PDF"}</span>
            <span className="sample-row-main"><b>{sample.display_name}</b><small>{sample.file}</small></span>
            <span className="sample-row-type">{sample.content_type === "image/png" ? "Ảnh scan" : "Có văn bản"}</span>
            <span className="sample-cta">{busy ? "Đang thêm…" : "Dùng mẫu"}</span>
          </button>)}</div>
        </details>
        <section className="list-section">
          <div className="section-title"><div><p className="eyebrow">Hồ sơ trong tổ chức</p><h2>Danh sách chứng từ</h2></div><span className="count-pill">{invoices.data?.length ?? 0} hồ sơ</span></div>
          {invoices.isLoading ? <div className="loading">Đang tải hồ sơ…</div> : invoices.data?.length ? <div className="invoice-list">
            <div className="invoice-list-head" aria-hidden="true"><span></span><span>Chứng từ / nhà cung cấp</span><span>Trạng thái</span><span>Tổng tiền</span><span></span></div>
            {invoices.data.map(row => <button className="invoice-row" key={row.id} onClick={() => onOpen(row.id)}>
              <span className="file-type">{row.mime === "application/pdf" ? "PDF" : "IMG"}</span>
              <span className="invoice-main"><b>{row.invoice_number || row.filename}</b><span>{row.supplier_name || "Đang chờ trích xuất"} · {row.filename}</span></span>
              <span className={`status-pill ${row.workflow_status.toLowerCase()}`}>{statusText[row.workflow_status] ?? row.workflow_status}</span>
              <span className="amount">{row.grand_total ? `${row.grand_total} ${row.currency ?? ""}` : "—"}</span>
              <span className="arrow">↗</span>
            </button>)}
          </div> : <div className="empty-state"><div className="empty-icon">▤</div><h3>Chưa có chứng từ</h3><p>Chọn tệp của bạn hoặc bắt đầu bằng một mẫu demo.</p></div>}
        </section>
      </> : page === "orders" ? <section className="list-section">
        <div className="section-title"><div><p className="eyebrow">Dữ liệu có cấu trúc</p><h2>Đơn đặt hàng trong tổ chức</h2></div><div className="heading-actions"><button className="secondary" onClick={downloadPoTemplate}>Tải CSV mẫu</button><label className="file-button">Nhập CSV<input aria-label="Nhập CSV PO" type="file" accept="text/csv,.csv" disabled={busy} onChange={e => { const file = e.target.files?.[0]; if (file) void importPoCsv(file); e.currentTarget.value = ""; }} /></label><button className="primary" onClick={() => setShowPoForm(x => !x)}>{showPoForm ? "Đóng" : "+ Tạo PO"}</button></div></div>
        {importMessage && <div className="banner-success" role="status">{importMessage}</div>}
        {showPoForm && <form className="po-create-card" onSubmit={createPo}>
          <div className="section-title"><div><h3>Tạo đơn đặt hàng</h3><p className="muted">Nhà cung cấp mẫu đã có sẵn: DEMO-SUP-001. Đơn đã tạo không thể sửa.</p></div></div>
          <div className="field-grid three"><label>Mã PO<input required value={poNumber} onChange={e => setPoNumber(e.target.value)} /></label><label>SKU<input value={poSku} onChange={e => setPoSku(e.target.value)} /></label><label>Mô tả<input required value={poDescription} onChange={e => setPoDescription(e.target.value)} /></label><label>Số lượng<input required inputMode="decimal" value={poQuantity} onChange={e => setPoQuantity(e.target.value)} /></label><label>Đơn giá VND<input required inputMode="decimal" value={poPrice} onChange={e => setPoPrice(e.target.value)} /></label></div>
          <button className="primary" disabled={busy}>Lưu PO</button>
        </form>}
        <div className="po-list">{(orders.data ?? []).map(po => <article className="po-card" key={po.id}>
          <div className="po-head"><div><span className="eyebrow">{po.supplier_name} · MST {po.supplier_tax_id}</span><h3>{po.po_number}</h3></div><span className={`status-pill ${po.state === "OPEN" ? "open" : "closed"}`}>{po.state === "OPEN" ? "Đang mở" : "Đã đóng"}</span></div>
          {po.lines.map(line => <div className="po-line" key={line.id}><span><b>{line.sku || "Không SKU"}</b> · {line.description}</span><span>{line.quantity} {line.unit} × {line.unit_price} VND</span></div>)}
        </article>)}</div>
      </section> : <PolicyKnowledge session={session} />}
      <footer className="footer-note">Dữ liệu mẫu là dữ liệu tổng hợp. Phê duyệt trong ứng dụng là quyết định nội bộ, không xác nhận tính hợp pháp hay ghi nhận thanh toán.</footer>
      </main>
    </div>
  </div>;
}

function InvoiceReview({ session, invoiceId, onBack }: { session: Session; invoiceId: string; onBack: () => void }) {
  const client = useQueryClient();
  const detail = useQuery({
    queryKey: ["invoice", invoiceId],
    queryFn: () => api<InvoiceDetail>(session, `/invoices/${invoiceId}`),
    refetchInterval: query => {
      const status = query.state.data?.job?.status;
      if (status && ["PENDING", "PROCESSING", "WAITING_RETRY"].includes(status)) return document.hidden ? 8000 : 2000;
      return false;
    },
  });
  const orders = useQuery({ queryKey: ["orders"], queryFn: () => api<PO[]>(session, "/purchase-orders") });
  const versions = useQuery({ queryKey: ["versions", invoiceId], queryFn: () => api<{ id: string; version_no: number; origin: string; reason: string | null; created_at: string }[]>(session, `/invoices/${invoiceId}/versions`), enabled: Boolean(detail.data?.version) });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [poId, setPoId] = useState("");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("Sửa theo chứng từ gốc");
  const [sourcePage, setSourcePage] = useState(1);
  const [manualSourcePageInput, setManualSourcePageInput] = useState("");
  const [manualEvidencePageInputs, setManualEvidencePageInputs] = useState<Record<string, string>>({});
  const [manualEvidencePages, setManualEvidencePages] = useState<Record<string, number>>({});
  const [selectedField, setSelectedFieldState] = useState("");
  const [ackWarnings, setAckWarnings] = useState<string[]>([]);
  const [decisionNote, setDecisionNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const current = detail.data;
  function setSelectedField(path: string) {
    setSelectedFieldState(path);
    const source = current?.evidence.find(item => item.field_path === path);
    if (source?.page_no != null) setSourcePage(Math.max(1, Math.min(current?.pages ?? source.page_no, source.page_no)));
  }

  useEffect(() => {
    if (!current?.content_url) return;
    let active = true; let created = "";
    setError("");
    void protectedBlob(session, current.content_url).then(url => { created = url; if (active) { setSourceUrl(url); setError(""); } else URL.revokeObjectURL(url); }).catch(cause => { if (active) setError(errorMessage(cause)); });
    return () => { active = false; if (created) URL.revokeObjectURL(created); setSourceUrl(""); };
  }, [session, current?.content_url]);

  useEffect(() => {
    if (!current?.version?.data) return;
    const next = structuredClone(current.version.data);
    delete next.document_id;
    setDraft(next);
    setMapping({}); setAckWarnings([]); setManualSourcePageInput(""); setManualEvidencePageInputs({}); setManualEvidencePages({});
  }, [current?.version?.id]);

  useEffect(() => {
    if (poId || !current?.version?.data || !orders.data?.length) return;
    const data = current.version.data;
    const suggested = orders.data.find(po => po.po_number === data.po_number && po.state === "OPEN") ?? orders.data.find(po => po.supplier_tax_id === data.supplier_tax_id && po.state === "OPEN");
    if (suggested) setPoId(suggested.id);
  }, [current?.version?.id, orders.data, poId]);

  useEffect(() => {
    if (current?.reconciliation) setMapping(current.reconciliation.mapping);
  }, [current?.reconciliation?.id]);

  const chosenPo = useMemo(() => orders.data?.find(po => po.id === poId), [orders.data, poId]);
  const isBusy = busy || detail.isLoading;
  function update<K extends keyof Draft>(field: K, value: Draft[K]) { setDraft(old => old ? { ...old, [field]: value } : old); }
  function updateLine(index: number, field: keyof LineDraft, value: string | null) {
    setDraft(old => old ? { ...old, lines: old.lines.map((line, row) => row === index ? { ...line, [field]: value || null } : line) } : old);
  }
  async function refresh() {
    await Promise.all([client.invalidateQueries({ queryKey: ["invoice", invoiceId] }), client.invalidateQueries({ queryKey: ["invoices"] }), client.invalidateQueries({ queryKey: ["versions", invoiceId] }), client.invalidateQueries({ queryKey: ["orders"] })]);
  }
  async function run(label: string, action: () => Promise<unknown>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); await refresh(); setMessage(label); }
    catch (cause) { setError(errorMessage(cause)); if (cause instanceof ApiError && cause.status === 409) await refresh(); }
    finally { setBusy(false); }
  }
  function saveVersion() {
    if (!draft || !current?.version) return;
    const payload = { ...draft }; delete payload.document_id;
    void run("Đã tạo phiên bản mới; kết quả đối soát cũ không còn hiệu lực.", () => api(session, `/invoices/${invoiceId}/versions`, {
      method: "POST", body: JSON.stringify({ expected_version_id: current.version!.id, reason,
        manual_source_page: manualSourcePageInput.trim() ? Number(manualSourcePageInput) : null,
        manual_evidence_pages: manualEvidencePages, draft: payload }),
    }, idempotencyKey()));
  }
  function reconcile() {
    if (!current?.version || !poId) return;
    void run("Đã lưu kết quả đối soát cho snapshot hiện tại.", () => api(session, `/invoices/${invoiceId}/reconciliations`, {
      method: "POST", body: JSON.stringify({ expected_version_id: current.version!.id, po_id: poId, line_mapping: mapping }),
    }, idempotencyKey()));
  }
  function decide(action: "APPROVE" | "REJECT") {
    if (!current?.version || !current.reconciliation) return;
    void run(action === "APPROVE" ? "Đã phê duyệt nội bộ." : "Đã từ chối chứng từ.", () => api(session, `/invoices/${invoiceId}/decisions`, {
      method: "POST", body: JSON.stringify({ action, expected_version_id: current.version!.id,
        reconciliation_id: current.reconciliation!.id, expected_snapshot_hash: current.reconciliation!.snapshot_hash,
        acknowledged_warning_codes: ackWarnings, note: decisionNote || null }),
    }, idempotencyKey()));
  }
  async function download(format: "json" | "csv") {
    setError("");
    try {
      const response = await fetch(`${API_URL}/exports/invoices/${invoiceId}.${format}`, { headers: { Authorization: `Bearer ${session.access_token}` } });
      if (!response.ok) throw new Error("Không tải được file export.");
      const blob = await response.blob(); const href = URL.createObjectURL(blob); const anchor = document.createElement("a");
      anchor.href = href; anchor.download = `invoice-${current?.invoice_number ?? invoiceId}.${format}`; anchor.click(); URL.revokeObjectURL(href);
    } catch (cause) { setError(errorMessage(cause)); }
  }

  if (detail.isLoading || !current) return <div className="app-shell"><header className="topbar"><button className="ghost" onClick={onBack}>← Danh sách</button><FixtureBadge /></header><main className="page-wrap"><div className="loading">Đang tải hồ sơ…</div>{detail.error && <div className="banner-error">{errorMessage(detail.error)}</div>}</main></div>;
  const data = current.version?.data;
  const warnings = current.reconciliation?.issues.filter(issue => issue.severity === "WARN") ?? [];
  const pageUrl = sourceUrl && current.mime === "application/pdf" ? `${sourceUrl}#page=${sourcePage}` : sourceUrl;
  const pageEvidence = selectedField ? current.evidence.find(item => item.field_path === selectedField) : undefined;
  const missingPageEvidence = current.evidence.filter(item => item.page_no == null && ["MISSING_PROVENANCE", "UNVERIFIED"].includes(item.verification_state));
  const selectedPageNeedsAttestation = Boolean(selectedField && current.reconciliation?.issues.some(issue => ["SOURCE_PAGE_MISSING", "SOURCE_PAGE_INVALID"].includes(issue.code) && issue.field_path === selectedField));
  const manualPageInput = selectedField ? manualEvidencePageInputs[selectedField] ?? "" : "";
  const attestationPage = Number(manualPageInput);
  const canAttestSelectedPage = manualPageInput.trim() !== "" && Number.isInteger(attestationPage) && attestationPage >= 1 && attestationPage <= current.pages;

  return <div className="app-shell">
    <header className="topbar review-topbar"><button className="ghost back-button" onClick={onBack}>← <span>Danh sách</span></button>{current.fixture_mode ? <FixtureBadge /> : <span className="read-only-tag">Nhà cung cấp: {current.provider ?? "chưa rõ"} · Kết quả do model đề xuất</span>}<div className="user-menu"><span>{session.role === "reviewer" ? "Người duyệt" : "Người nhập liệu"}</span><button className="ghost" onClick={() => { /* session remains in memory; sign-out is available from list */ onBack(); }}>Đóng</button></div></header>
    <main className="review-page">
      <div className="review-heading"><div><p className="eyebrow">{current.filename} · {current.mime === "application/pdf" ? "PDF" : "Ảnh"}</p><h1>{current.invoice_number || "Đang xử lý chứng từ"}</h1><span className={`status-pill ${current.workflow_status.toLowerCase()}`}>{statusText[current.workflow_status] ?? current.workflow_status}</span></div><div className="heading-actions"><span className="version-label">Phiên bản {current.version_no || "—"}</span>{current.allowed_actions.export && <><button className="secondary" onClick={() => void download("json")}>Xuất JSON</button><button className="secondary" onClick={() => void download("csv")}>Xuất CSV</button></>}</div></div>
      {error && <div className="banner-error" role="alert">{error}</div>}{message && <div className="banner-success" role="status">{message}</div>}
      {current.workflow_status === "PROCESSING_FAILED" && <div className="banner-error"><div><b>Không xử lý được tệp.</b><span>Mã lỗi: {current.job?.error_code ?? "UNKNOWN"}.{current.fixture_mode ? " Chế độ Fixture chỉ nhận mẫu có sẵn." : " Hãy kiểm tra cấu hình nhà cung cấp rồi thử lại."}</span></div><button className="secondary" onClick={() => void run("Đã xếp lại job.", () => api(session, `/jobs/${current.job!.id}/retry`, { method: "POST" }, idempotencyKey()))}>Thử lại</button></div>}
      {current.job && ["PENDING", "PROCESSING", "WAITING_RETRY"].includes(current.job.status) && <div className="job-progress"><span className="spinner"/><div><b>{current.job.status === "PENDING" ? "Đang chờ xử lý" : current.fixture_mode ? "Đang dùng dữ liệu mẫu" : `Đang trích xuất bằng ${current.provider ?? "provider"}`}</b><span>Hồ sơ sẽ tự cập nhật khi hoàn tất.</span></div></div>}
      {!current.reconciliation && missingPageEvidence.length > 0 && <div className="issue-list" role="alert">{missingPageEvidence.map(item => <div className="issue block" key={item.field_path}><span aria-label="Lỗi chặn">!</span><div><b>{issueText.SOURCE_PAGE_MISSING}</b><button className="link-button" onClick={() => setSelectedField(item.field_path)}>Xem dữ liệu nguồn: {item.field_path}</button></div></div>)}</div>}
      <div className="review-grid">
        <section className="document-panel">
          <div className="panel-heading"><div><p className="eyebrow">Chứng từ gốc</p><h2>{current.filename}</h2></div><span className="page-count">Trang {sourcePage} / {current.pages}</span></div>
          <div className="viewer-toolbar"><button className="secondary" disabled={sourcePage <= 1} onClick={() => setSourcePage(p => Math.max(1, p - 1))}>← Trang trước</button><label>Đi tới trang<input type="number" aria-label="Số trang" min={1} max={current.pages} value={sourcePage} onChange={e => setSourcePage(Math.min(current.pages, Math.max(1, Number(e.target.value) || 1)))} /></label><button className="secondary" disabled={sourcePage >= current.pages} onClick={() => setSourcePage(p => Math.min(current.pages, p + 1))}>Trang sau →</button></div>
          <div className={`document-viewer ${current.mime === "application/pdf" ? "pdf-viewer" : ""}`}>{sourceUrl ? current.mime === "application/pdf" ? <><iframe title="Tài liệu PDF" src={pageUrl} /><div className="pdf-mobile-state"><span className="pdf-file-icon">PDF</span><div><b>Chứng từ PDF</b><p>Mở tài liệu trong tab mới để xem rõ và phóng to khi cần.</p><a href={sourceUrl} target="_blank" rel="noopener noreferrer">Mở tài liệu</a></div></div></> : <img alt={`Trang ${sourcePage} của ${current.filename}`} src={sourceUrl} /> : <div className="loading">Đang tải tài liệu riêng tư…</div>}</div>
          {selectedField && <div className="source-note">
            <b>Nguồn của {selectedField}</b>
            <span>{pageEvidence ? `${pageEvidence.page_no == null ? "Thiếu trang nguồn" : `Trang ${pageEvidence.page_no}`} · ${pageEvidence.origin} · ${pageEvidence.verification_state}` : "Chưa có evidence cho trường này."}</span>
            {pageEvidence?.raw_value != null && <small>Giá trị gốc: {pageEvidence.raw_value}</small>}
            {pageEvidence?.normalized_value != null && <small>Giá trị chuẩn hóa: {pageEvidence.normalized_value}</small>}
            {pageEvidence?.normalization_issue && <small role="alert">Cần kiểm tra: {issueText[pageEvidence.normalization_issue] ?? pageEvidence.normalization_issue}</small>}
            {pageEvidence?.raw_text && <small>{pageEvidence.raw_text}</small>}
            {pageEvidence?.confidence != null && <small>Điểm provider (chưa hiệu chuẩn): {pageEvidence.confidence}</small>}
            {pageEvidence?.bbox && <small>Vùng nguồn do provider trả về: {JSON.stringify(pageEvidence.bbox)}</small>}
            {session.role === "reviewer" && current.allowed_actions.edit && selectedPageNeedsAttestation && <div className="source-page-attestation">
              <label htmlFor="manual-evidence-page">Trang bạn đã trực tiếp đối chiếu</label>
              <input id="manual-evidence-page" type="number" min={1} max={current.pages} step={1} value={manualPageInput} onChange={event => {
                const value = event.target.value;
                setManualEvidencePageInputs(old => ({ ...old, [selectedField]: value }));
                setManualEvidencePages(old => { const next = { ...old }; delete next[selectedField]; return next; });
              }} />
              <small>Không điền sẵn trang, vùng hoặc độ tin cậy. Chỉ xác nhận sau khi đã xem chứng từ gốc.</small>
              <label className="check-row"><input type="checkbox" disabled={!canAttestSelectedPage} checked={manualEvidencePages[selectedField] != null} onChange={event => setManualEvidencePages(old => {
                const next = { ...old };
                if (event.target.checked && canAttestSelectedPage) next[selectedField] = attestationPage;
                else delete next[selectedField];
                return next;
              })} /> Tôi xác nhận giá trị này xuất hiện ở trang {manualEvidencePages[selectedField] ?? (canAttestSelectedPage ? attestationPage : "chưa chọn")}</label>
            </div>}
          </div>}
          <p className="viewer-disclaimer">{current.mime === "application/pdf" ? "Mở trang nguồn theo số trang; không hiển thị vùng bbox giả." : `${current.fixture_mode ? "Trang nguồn do fixture đề xuất" : "Trang nguồn do provider đề xuất"}; ảnh chưa được xác minh chữ tự động.`}</p>
        </section>

        <section className="review-panel">
          <div className="panel-heading"><div><p className="eyebrow">Kết quả trích xuất</p><h2>Thông tin hóa đơn</h2></div><span className="read-only-tag">{current.workflow_status === "APPROVED" || current.workflow_status === "REJECTED" ? "Chỉ xem" : "Có thể sửa"}</span></div>
          {data && draft ? <>
            <section className="review-field-section">
              <div className="review-section-heading"><div><h3>Thông tin chung</h3><p>Đối chiếu các giá trị nhận dạng với chứng từ gốc.</p></div></div>
              <div className="field-grid two">
              <Field label="Số hóa đơn" path="invoice_number" value={draft.invoice_number} onChange={v => update("invoice_number", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Ký hiệu hóa đơn" path="invoice_series" value={draft.invoice_series} onChange={v => update("invoice_series", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Ngày hóa đơn" path="invoice_date" value={draft.invoice_date} onChange={v => update("invoice_date", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} type="date" />
              <Field label="Mã số thuế NCC" path="supplier_tax_id" value={draft.supplier_tax_id} onChange={v => update("supplier_tax_id", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Nhà cung cấp" path="supplier_name" value={draft.supplier_name} onChange={v => update("supplier_name", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Số PO trên hóa đơn" path="po_number" value={draft.po_number} onChange={v => update("po_number", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Tiền tệ" path="currency" value={draft.currency} onChange={v => update("currency", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              </div>
              <label className="check-row"><input type="checkbox" checked={draft.invoice_series_absent_confirmed} disabled={!current.allowed_actions.edit} onChange={e => update("invoice_series_absent_confirmed", e.target.checked)} /> Tôi đã kiểm tra chứng từ và xác nhận hóa đơn không có series</label>
            </section>
            <section className="review-field-section amount-section">
              <div className="review-section-heading"><div><h3>Các khoản tiền</h3><p>Kiểm tra số tiền, thuế và tổng thanh toán.</p></div></div>
              <div className="field-grid two">
              <Field label="Tạm tính" path="subtotal" value={draft.subtotal} onChange={v => update("subtotal", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Thuế ghi trên hóa đơn" path="tax_total" value={draft.tax_total} onChange={v => update("tax_total", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Tổng cộng" path="grand_total" value={draft.grand_total} onChange={v => update("grand_total", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Chiết khấu" path="discount_total" value={draft.discount_total} onChange={v => update("discount_total", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              <Field label="Phí vận chuyển" path="shipping_total" value={draft.shipping_total} onChange={v => update("shipping_total", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
              </div>
            </section>
            <div className="line-section"><div className="line-title"><h3>Dòng hàng</h3><span>{draft.lines.length} dòng</span></div>
              {draft.lines.map((line, index) => <div className="line-card" key={line.line_id ?? index}>
                <div className="field-grid two">
                  <Field label="SKU" path={`lines[${index}].sku`} value={line.sku} onChange={v => updateLine(index, "sku", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
                  <Field label="Mô tả" path={`lines[${index}].description`} value={line.description} onChange={v => updateLine(index, "description", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
                  <Field label="Đơn vị" path={`lines[${index}].unit`} value={line.unit} onChange={v => updateLine(index, "unit", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
                  <Field label="Số lượng" path={`lines[${index}].quantity`} value={line.quantity} onChange={v => updateLine(index, "quantity", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
                  <Field label="Đơn giá" path={`lines[${index}].unit_price`} value={line.unit_price} onChange={v => updateLine(index, "unit_price", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
                  <Field label="Thành tiền dòng" path={`lines[${index}].net_amount`} value={line.net_amount} onChange={v => updateLine(index, "net_amount", v)} onSource={setSelectedField} disabled={!current.allowed_actions.edit} />
                </div>
              </div>)}
            </div>
            {current.allowed_actions.edit && <div className="revision-actions"><label>Lý do chỉnh sửa<input value={reason} onChange={e => setReason(e.target.value)} minLength={3} /></label><label>Trang đã đối chiếu cho giá trị sửa<input type="number" min={1} max={current.pages} step={1} value={manualSourcePageInput} onChange={e => setManualSourcePageInput(e.target.value)} placeholder="Nhập số trang" /></label><button className="secondary" disabled={isBusy} onClick={saveVersion}>Lưu phiên bản mới</button></div>}

            <div className="reconcile-box">
              <div className="section-title"><div><p className="eyebrow">Đối chiếu theo quy tắc</p><h3>Đối soát với đơn đặt hàng</h3></div>{current.reconciliation && <span className={`recon-pill ${current.reconciliation.status.toLowerCase()}`}>{reconciliationStatusText[current.reconciliation.status] ?? current.reconciliation.status}</span>}</div>
              <label>Chọn đơn đặt hàng<select aria-label="Chọn PO đối soát" value={poId} disabled={!current.allowed_actions.reconcile} onChange={e => { setPoId(e.target.value); setMapping({}); }}><option value="">Chọn PO…</option>{(orders.data ?? []).filter(po => po.state === "OPEN").map(po => <option value={po.id} key={po.id}>{po.po_number} · {po.supplier_name} · {po.state}</option>)}</select></label>
              {chosenPo && draft.lines.map((line, index) => <label className="mapping-row" key={line.line_id ?? index}><span>{line.sku || line.description || `Dòng ${index + 1}`}</span><select aria-label={`Ánh xạ dòng ${index + 1}`} disabled={!current.allowed_actions.reconcile} value={mapping[line.line_id ?? ""] ?? ""} onChange={e => setMapping(old => ({ ...old, [line.line_id ?? ""]: e.target.value }))}><option value="">Tự đối chiếu SKU</option>{chosenPo.lines.map(poLine => <option value={poLine.id} key={poLine.id}>{poLine.sku || "Không SKU"} · {poLine.description} · {poLine.quantity} × {poLine.unit_price}</option>)}</select></label>)}
              {chosenPo && <div className="po-comparison"><div><span>PO</span><b>{chosenPo.po_number}</b></div><div><span>Nhà cung cấp</span><b>{chosenPo.supplier_name}</b></div><div><span>Dòng hàng</span><b>{chosenPo.lines.length}</b></div><div><span>Đơn vị tiền</span><b>{chosenPo.currency}</b></div></div>}
              {current.allowed_actions.reconcile && <button className="primary" onClick={reconcile} disabled={isBusy || !poId}>Chạy đối soát</button>}
              {current.reconciliation && <div className="issue-list" aria-live="polite">{current.reconciliation.issues.length ? current.reconciliation.issues.map((issue, i) => <div className={`issue ${issue.severity.toLowerCase()}`} key={`${issue.code}-${i}`}><span aria-label={issue.severity === "BLOCK" ? "Lỗi chặn" : "Cảnh báo"}>{issue.severity === "BLOCK" ? "!" : "i"}</span><div><b>{issueText[issue.code] ?? issue.code}</b>{issue.field_path && <button className="link-button" onClick={() => { const evidence = current.evidence.find(item => item.field_path === issue.field_path); if (evidence?.page_no != null) setSourcePage(Math.max(1, Math.min(current.pages, evidence.page_no))); setSelectedField(issue.field_path!); }}>{current.evidence.find(item => item.field_path === issue.field_path)?.page_no == null ? "Xem dữ liệu nguồn" : "Mở trang nguồn"}: {issue.field_path}</button>}</div></div>) : <div className="pass-note">✓ Không tìm thấy chênh lệch theo chính sách {current.reconciliation.policy_version}</div>}</div>}
              {session.role === "reviewer" && current.reconciliation && warnings.length > 0 && <div className="warning-acks"><b>Xác nhận cảnh báo để duyệt</b>{warnings.map(issue => <label className="check-row" key={issue.code}><input type="checkbox" checked={ackWarnings.includes(issue.code)} onChange={e => setAckWarnings(old => e.target.checked ? [...old, issue.code] : old.filter(x => x !== issue.code))} />{issueText[issue.code] ?? issue.code}</label>)}</div>}
              {session.role === "reviewer" && current.reconciliation && current.allowed_actions.reject && <div className="decision-box"><label>Ghi chú review<textarea rows={2} value={decisionNote} onChange={e => setDecisionNote(e.target.value)} placeholder="Ghi chú từ chối hoặc review nội bộ…" /></label><div className="decision-buttons"><button className="danger-outline" disabled={isBusy || decisionNote.trim().length < 3} onClick={() => decide("REJECT")}>Từ chối</button>{current.allowed_actions.approve && <button className="primary" disabled={isBusy || (warnings.length > 0 && warnings.some(item => !ackWarnings.includes(item.code)))} onClick={() => decide("APPROVE")}>Phê duyệt nội bộ</button>}</div></div>}
              {session.role === "operator" && <p className="muted small-note">Tài khoản người nhập liệu không có quyền duyệt. Cần đăng nhập bằng tài khoản người duyệt để ra quyết định.</p>}
            </div>
          </> : <div className="empty-state"><div className="spinner"/><h3>Đang chờ kết quả trích xuất</h3><p>Kết quả sẽ xuất hiện tại đây sau khi xử lý xong.</p></div>}

          <InvoiceCopilot session={session} invoiceId={invoiceId} />
          <section className="history-box"><div className="section-title"><div><p className="eyebrow">Lịch sử thay đổi</p><h3>Các phiên bản đã lưu</h3></div></div>{(versions.data ?? []).slice().reverse().map(item => <div className="history-row" key={item.id}><b>v{item.version_no}</b><span>{item.origin === "fixture" ? "Trích xuất mẫu" : "Chỉnh sửa thủ công"}</span><small>{item.reason || ""}</small><code>{item.id.slice(0, 8)}</code></div>)}</section>
        </section>
      </div>
      <footer className="footer-note">Mỗi lần chỉnh sửa và đối soát được lưu thành một phiên bản riêng. Phê duyệt trong ứng dụng là quyết định duyệt nội bộ.</footer>
    </main>
  </div>;
}

function Field({ label, path, value, onChange, onSource, disabled = false, type = "text" }: {
  label: string; path: string; value: string | null; onChange: (value: string | null) => void;
  onSource: (path: string) => void; disabled?: boolean; type?: string;
}) {
  return <label className="field-control"><span>{label}<button className="source-link" type="button" title="Mở trang nguồn" onClick={() => onSource(path)}>↗</button></span><input type={type} value={value ?? ""} disabled={disabled} onChange={e => onChange(e.target.value || null)} /></label>;
}

export default App;

