# Intelligent Document Processing Assistant — Implementation Plan for Codex

Version: 1.0 | Ngày: 22/10/2026 | Ngôn ngữ sản phẩm: tiếng Việt, hỗ trợ chứng từ Việt/Anh.

Mục tiêu: xây một sản phẩm portfolio cho AI Engineer hướng LLM/Agent ở mức junior, có luồng nghiệp vụ hoàn chỉnh, dữ liệu đánh giá và bản triển khai thực tế. Đây là đặc tả để implement, chưa phải phần mềm hoặc kết quả benchmark đã chạy.

## 0. Prompt giao việc đầu tiên cho Codex

Copy đoạn dưới đây, đính kèm toàn bộ file này, và mở Codex tại một repository mới dành cho dự án.

```text
Bạn là kỹ sư triển khai dự án Intelligent Document Processing Assistant.
Đọc toàn bộ IDP_ASSISTANT_IMPLEMENTATION_PLAN.md trước khi sửa code.

Hãy implement theo các mốc M0–M6 và definition of done trong tài liệu.
Trong lượt đầu, hoàn thành M0 và M1 thành một lát cắt chạy được từ UI đến
database, gồm đăng nhập, upload, job bất đồng bộ, fixture extraction và review.
Không chỉ tạo skeleton, TODO hoặc giao diện dùng dữ liệu hard-code.

Trước khi sửa, kiểm tra repository, README và AGENTS.md hiện có. Giữ nguyên
công việc không liên quan. Nếu repo trống, tạo monorepo theo cấu trúc đề xuất.
Ghi quyết định kỹ thuật vào docs/decisions.md và tiến độ vào docs/progress.md.
Ưu tiên P0, giữ domain/service độc lập với SDK và framework agent.

Chạy migration và các kiểm thử tương ứng với phạm vi đã sửa. Không yêu cầu
API key thật để chạy CI hoặc fixture demo. Fixture mode phải được ghi rõ trên UI.
Không giả lập thành công một provider thật và không bịa benchmark.

Nếu thiếu credential cloud, tiếp tục hoàn thành mọi phần độc lập, thêm adapter,
contract test và hướng dẫn cấu hình; đánh dấu live verification là NOT_RUN.
Không tự dùng dịch vụ phát sinh phí nếu chưa có cấu hình và cho phép phù hợp.

Khi xong lượt: báo phần đã chạy được, các lệnh đã kiểm tra và kết quả thực tế,
các rủi ro còn lại, bước tiếp theo; cập nhật docs/progress.md.
Đọc các hợp đồng schema, concurrency, tiền tệ và phê duyệt trong tài liệu này
trước khi tự chọn chi tiết triển khai. Không bỏ qua các invariant để chạy demo.
```

Mỗi lượt tiếp theo: “Đọc plan và docs/progress.md; hoàn thành mốc tiếp theo chưa đạt, chạy kiểm thử liên quan và cập nhật tiến độ.” Chia thành các mốc để kết quả mỗi lượt có thể review và chạy lại. Không coi một lượt tạo nhiều file là bằng chứng hoàn thành sản phẩm.

## 1. Bài toán, người dùng và kết quả

**Người dùng:** nhân viên mua hàng, kế toán nội bộ, chủ doanh nghiệp nhỏ. Người dùng đã có đơn đặt hàng (purchase order — PO) trong dữ liệu có cấu trúc, nhận hóa đơn PDF/ảnh và muốn giảm việc nhập liệu, kiểm tra chênh lệch.

**Luồng chính:** upload hóa đơn → trích xuất → sửa dữ liệu nếu cần → chọn PO và ánh xạ dòng hàng → kiểm tra bằng code → người có quyền phê duyệt hoặc từ chối → xuất dữ liệu đã duyệt.

**Ví dụ:** PO đặt 10 bàn phím, đơn giá 200.000 VND. Hóa đơn ghi 12 chiếc. Hệ thống hiển thị chênh lệch 2 chiếc, liên kết trang chứa số liệu, và chặn phê duyệt đến khi dữ liệu được sửa hợp lệ hoặc người dùng từ chối hóa đơn. LLM chỉ hỗ trợ đọc và giải thích; kết quả tính toán, quy tắc và quyền quyết định do backend thực thi.

**Thước đo giá trị:** độ đúng của trường trích xuất, độ đúng đối soát, số thao tác sửa và thời gian kiểm tra một chứng từ. Đánh giá phần AI trên output ban đầu, trước khi người dùng sửa.

## 2. Quyết định phạm vi

### P0 — phải hoàn thành để có MVP dùng thử

- Một hóa đơn đối chiếu với một PO, một lần thanh toán toàn bộ; một PO có tối đa một hóa đơn APPROVED.
- Hóa đơn đầu vào: PDF có text, PDF scan, PNG, JPEG; một file chứa một hóa đơn; tối đa 10 trang, 15 MiB. Parser/provider có thể yêu cầu giới hạn thấp hơn, phải báo rõ trước khi gọi.
- VND; đơn giá trước thuế; số lượng dương; không chiết khấu, không phí vận chuyển; tổng thuế ghi ở cấp hóa đơn. Có thuế suất hỗn hợp, credit note, tiền âm, điều chỉnh, đa tiền tệ: trả `UNSUPPORTED_CASE`, đưa vào review, không âm thầm áp dụng công thức đơn giản.
- Chứng từ phải thể hiện đủ subtotal, tax total và grand total, hoặc người review bổ sung giá trị với nguồn gốc chỉnh sửa rõ ràng. Giá trị thiếu là null, không mặc định thành 0.
- PO nhập JSON/CSV theo schema cố định và tạo form thủ công. Chưa đọc PO từ PDF trong P0.
- Gán nhãn trang nguồn; bbox tùy khả năng. UI phải cho mở đúng trang, không vẽ vùng chính xác giả.
- Sửa dữ liệu tạo phiên bản bất biến; đối soát, duyệt và export gắn với phiên bản cụ thể.
- Hai vai trò `operator`, `reviewer`; xác thực thật; dữ liệu tách theo organization.
- Fixture provider offline + một provider thật Bedrock hỗ trợ input cần thiết. Thiếu credential không làm hỏng fixture demo, nhưng chưa đủ để tuyên bố live AI hoàn tất.
- Job có lưu trạng thái bền vững, retry hữu hạn và chống thực thi lặp.
- Review luôn có người duyệt; không tự duyệt dựa trên LLM hoặc điểm confidence.
- UI, API, test, bộ dữ liệu gán nhãn, báo cáo eval, Docker và tài liệu.

### P1 — mở rộng sau khi P0 chạy ổn định

- LangGraph checkpoint và interrupt cho review/resume, triển khai ở M4 sau khi domain đã được kiểm thử.
- MCP server read-only và client demo độc lập.
- Adapter Microsoft Foundry cho cùng extraction contract.
- RAG hỏi đáp quy định nội bộ, có trích dẫn phiên bản tài liệu.
- Chat copilot có function calling cho các tool đọc dữ liệu.

### P2 — thí nghiệm, không chặn ứng tuyển

- So sánh single-agent và multi-agent cho giải thích/điều tra chênh lệch.
- Three-way matching: PO + phiếu nhận hàng + hóa đơn; partial invoice, nhiều PO; đơn vị quy đổi.
- XML hóa đơn điện tử, xác minh chữ ký, tích hợp ERP hoặc thanh toán.
- Fine-tuning, Kubernetes, xử lý livestream, tự host model GPU.

Phê duyệt trong ứng dụng là duyệt kết quả đối soát nội bộ. Không đặt tên hành động là xác minh hóa đơn hợp pháp hoặc thanh toán thành công.

## 3. Stack và nguyên tắc kỹ thuật

| Thành phần | Lựa chọn mặc định | Lý do |
|---|---|---|
| Backend | Python 3.12, FastAPI, Pydantic | Gần stack hiện tại, validation rõ |
| DB | PostgreSQL, SQLAlchemy 2, Alembic | Transaction, khóa, version và audit |
| Job | Celery + Redis, DB outbox và recovery scanner | Chạy background; DB là nguồn trạng thái |
| File | Local private volume trong dev; S3 private khi deploy | Adapter, không phụ thuộc cloud để chạy test |
| PDF | pypdf trích text; pypdfium2 render | Tách text và ảnh theo trang |
| AI | Provider protocol; boto3/Bedrock adapter | Một contract, kiểm thử độc lập |
| Workflow P1 | LangGraph + PostgreSQL checkpointer | Pause/resume bền vững |
| Frontend | React + TypeScript + Vite, TanStack Query, PDF.js | Review chứng từ và job polling |
| Test | pytest, PostgreSQL integration, Vitest, Playwright | Kiểm tra logic, transaction và luồng người dùng |
| Dev/CI | Docker Compose, uv, npm lockfile, Ruff, mypy, GitHub Actions | Có thể tái lập môi trường |

Chọn phiên bản stable tương thích tại M0, pin vào lockfile và ghi vào ADR. Không lấy code API từ phiên bản SDK khác rồi giả định tương thích. Giữ một dependency manager cho Python và một cho frontend.

Domain không import boto3, LangGraph, Celery hoặc MCP. Các adapter gọi chung application services; REST, MCP và chat không được có ba bản quy tắc nghiệp vụ khác nhau.

Không cần một vector database riêng cho P0. P1 RAG có thể dùng pgvector trong PostgreSQL.

## 4. Kiến trúc

```mermaid
flowchart TD
  UI["React review UI"] --> API["FastAPI + auth"]
  API --> DB["PostgreSQL + outbox"]
  API --> FILE["Private document storage"]
  DB --> DIS["Outbox dispatcher"]
  DIS --> Q["Redis queue"]
  Q --> W["Celery worker"]
  W --> FILE
  W --> AI["Extraction provider"]
  W --> RULE["Validation + reconciliation"]
  RULE --> DB
```

P0 lưu `jobs.current_step` và domain state trong DB. M4 thay phần điều phối bằng LangGraph; không duy trì hai workflow engine cùng hoạt động cho một run. Trạng thái nghiệp vụ trong application tables là authoritative, graph checkpoint phục vụ khôi phục thực thi.

Compose gồm: frontend, api, worker, dispatcher/recovery, postgres, redis. Có thể dùng cùng backend image với entrypoint khác nhau. Local file volume phải mount được vào api và worker; không mount công khai qua web server.

API không chờ extraction trong request upload. UI polling 2 giây khi tab mở, backoff khi lỗi hoặc tab ẩn; job hoàn thành hoặc review dừng polling. SSE là tùy chọn sau P0.

## 5. Cấu trúc repository

```text
backend/app/api/                  REST routes, auth dependencies, DTO
backend/app/domain/               invoice, PO, money, match rules, invariants
backend/app/services/             upload, extraction, review, approval, export
backend/app/db/                   models, repositories, session
backend/app/providers/            fixture, bedrock, foundry, capability checks
backend/app/documents/            validation, text, rendering, provenance
backend/app/jobs/                 tasks, outbox dispatcher, lease recovery
backend/app/workflows/            orchestration, LangGraph ở M4
backend/app/mcp/                  P1 tools + auth
backend/app/rag/                  P1 policy ingestion + retrieval
backend/app/observability/        structured logs, usage, metrics
backend/alembic/                  migrations
backend/tests/unit/
backend/tests/integration/
backend/tests/provider_contract/
frontend/src/features/            documents, invoices, review, PO, dashboard
frontend/e2e/
data/demo/                       demo documents + PO + manifest
data/eval/                       labels, split manifests, source/license notes
evals/                           runner, metrics, reports
docs/                            architecture, decisions, progress, runbook
infra/                           compose + deployment guide
scripts/                         seed, fixtures, evaluation, smoke checks
```

Ở M0 tạo `AGENTS.md` ngắn: đọc plan, invariants, lệnh kiểm thử, không bịa số đo, không ghi secret. Nội dung không được mâu thuẫn với chỉ dẫn người dùng. Không tạo skill/plugin mới cho dự án.

## 6. Data model và invariant

Tất cả ID nghiệp vụ dùng UUID; timestamp lưu UTC; UI hiển thị Asia/Ho_Chi_Minh. Tiền/số lượng dùng Decimal và `NUMERIC(20,6)`; JSON truyền chuỗi decimal. Không dùng float hoặc JavaScript Number để tính tiền; frontend chỉ format hoặc dùng decimal library khi cần preview.

| Bảng | Trường chính, constraint cần có |
|---|---|
| organizations | id, name |
| users / memberships | user id, password hash, active; membership org_id, role, unique(user_id, org_id) |
| suppliers | org_id, id, tax_id_normalized, name; unique(org_id, tax_id_normalized) |
| purchase_orders | org_id, id, po_number, supplier_id, currency, state OPEN/CLOSED; unique(org_id, po_number) |
| purchase_order_lines | org_id, po_id, line_id, sku, description, unit, quantity, unit_price; positive values |
| documents | org_id, id, storage_key, sha256, mime, bytes, pages, original_name, uploader; unique(org_id, sha256) |
| document_pages | org_id, document_id, page_no, text_ref, preview_ref, page_hash; unique(document_id, page_no) |
| invoices | org_id, id, document_id unique per org, current_version_id, workflow_status, version_counter |
| invoice_versions | org_id, id, invoice_id, version_no, canonical_json, schema_version, parent_id, snapshot_hash, origin, creator, reason; unique(invoice_id, version_no) |
| extraction_runs | org_id, id, document_id, provider, model_id, prompt_version, schema_version, raw_output_ref, usage, status, timestamps, failure_code |
| extraction_evidence | org_id, version_id, field_path, page_no, raw_text, bbox nullable, origin, verification_state |
| reconciliation_runs | org_id, id, invoice_version_id, po_id, mapping_json, policy_version, result_json, status, snapshot_hash; immutable |
| review_decisions | org_id, id, invoice_id, version_id, reconciliation_id, action, actor_id, note, acknowledged_warnings, expected_snapshot_hash; unique(invoice_id, version_id) |
| approval_records | org_id, invoice_id unique, version_id, po_id, supplier_id, business_key, decision_id; unique(org_id, business_key), unique(org_id, po_id) |
| audit_events | org_id, entity_type/id, actor, action, before_hash, after_hash, metadata, created_at; append only |
| jobs | org_id, id, kind, entity_id, status, step, attempts, lease_until, fencing_token, error_code, requested_by |
| outbox_events | org_id, id, kind, job_id, payload_ref, status, attempts, next_attempt_at; payload chứa ID, không chứa file |
| idempotency_requests | org_id, actor_id, route_key, key, request_hash, resource_id, response_snapshot; unique theo scope |
| provider_calls | org_id, run_id, attempt_no, latency_ms, tokens, model, cost_estimate nullable, price_version |

MCP service tokens và policy_chunks thêm ở P1. LangGraph checkpoint tables do package checkpointer quản lý, không tự sửa schema nội bộ.

Invariants bắt buộc:

1. Mọi truy vấn, file access và tool đều kiểm tra organization từ danh tính đã xác thực. `org_id` không lấy từ model hoặc body do client tự quyết định.
2. Composite foreign key/constraint phải ngăn liên kết invoice của org A với supplier, PO, version hoặc run của org B. Không chỉ dựa vào UUID khó đoán.
3. Version và reconciliation đã tạo là immutable. Sửa dữ liệu tạo version mới, làm mất hiệu lực kết quả đối soát và quyết định cũ trong UI.
4. Extraction ban đầu không được ghi đè bằng bản đã được người sửa; phục vụ evaluation chính xác.
5. APPROVED chỉ được thiết lập qua approval service trong transaction có kiểm tra quyền, version hiện hành và reconciliation hiện hành.
6. `approval_records` là hàng rào cuối cùng chống hai hóa đơn cùng sử dụng một PO hoặc cùng business key.
7. LLM, MCP và chat không có quyền gọi approval service. Trong P0 chỉ reviewer qua REST có thể quyết định.
8. PO và số định danh nhà cung cấp là immutable sau khi tạo trong P0. Chưa có API sửa PO; sửa sai bằng PO mới có mã mới. CLOSED không nhận hóa đơn mới.
9. Audit log không chứa password, token, raw PDF hoặc toàn bộ prompt; raw extraction chỉ ở private storage được kiểm soát.

## 7. Schema hóa đơn chuẩn

Tách `ExtractedInvoice` (giá trị có thể null, chưa tin cậy) và `ValidatedInvoice` (đủ điều kiện chạy quy tắc). Pydantic phải chặn unknown field. Không dùng schema yêu cầu đầy đủ để ép model đoán trường thiếu.

Ví dụ canonical payload sau khi chuẩn hóa:

```json
{
  "schema_version": "1.0",
  "document_id": "11111111-1111-4111-8111-111111111111",
  "invoice_number": "000123",
  "invoice_series": "DEMO26",
  "invoice_date": "2026-10-01",
  "supplier_tax_id": "DEMO-SUP-001",
  "supplier_name": "Nhà cung cấp Demo",
  "po_number": "PO-DEMO-001",
  "currency": "VND",
  "subtotal": "2000000",
  "tax_total": "200000",
  "grand_total": "2200000",
  "discount_total": "0",
  "shipping_total": "0",
  "lines": [
    {
      "line_id": "L1",
      "sku": "KB-001",
      "description": "Bàn phím",
      "unit": "cai",
      "quantity": "10",
      "unit_price": "200000",
      "net_amount": "2000000"
    }
  ]
}
```

Số thuế và định danh trong ví dụ là dữ liệu demo. Không dùng chúng để tuyên bố tuân thủ biểu mẫu hay thuế suất thực tế.

Trường `document_id`, `line_id`, org và thông tin actor do backend gán, không tin ID do model sinh. Model trả các row theo thứ tự; backend cấp line ID ổn định trong một version. Khi row bị thêm/xóa, tạo version mới và mapping mới.

Raw extraction cần giữ `raw_value`, `normalized_value` và evidence cho mỗi field. Không nhét evidence của nhiều field vào một câu giải thích chung.

```json
{
  "field_path": "lines[0].quantity",
  "raw_value": "10",
  "normalized_value": "10",
  "evidence": {
    "page_no": 1,
    "raw_text": "KB-001  Bàn phím  10  200.000  2.000.000",
    "bbox": null,
    "origin": "model",
    "verification_state": "PAGE_REFERENCED"
  }
}
```

Evidence phải có page_no hợp lệ. Với native text, kiểm tra quote có trong text trang sau chuẩn hóa whitespace; đánh dấu `TEXT_VERIFIED` khi khớp. Với image, page reference chỉ chứng minh trang tồn tại, không chứng minh model đọc đúng. UI dùng nhãn “trang nguồn do AI đề xuất”. Với manual correction, lưu actor, timestamp, note và page được chọn; không sửa origin thành model hoặc TEXT_VERIFIED.

Không hiển thị confidence của LLM dưới dạng xác suất đã hiệu chuẩn. Nếu dùng điểm từ OCR, lưu nguồn và mô tả đúng bản chất. Trường tài chính/định danh thiếu provenance phải có warning cần người dùng xác nhận trước khi approve.

### Chuẩn hóa và tiền tệ

- Locale mặc định `vi-VN`, lưu cùng run. ISO `YYYY-MM-DD` trong canonical. Chuỗi ngày mơ hồ không được đoán locale khác; ghi vấn đề để review.
- Chuỗi `1.234.567,89` theo locale vi-VN phải thành Decimal `1234567.89`. Giữ raw string. Bộ test riêng cho dấu nhóm, dấu thập phân, khoảng trắng và leading zero.
- Không parse chung bằng replace mọi dấu chấm/phẩy. Nếu không phân giải chắc chắn theo locale đã chọn: null + `AMBIGUOUS_NUMBER`.
- Invoice number, series, SKU, tax ID là chuỗi; không làm mất leading zero. Chuẩn hóa Unicode/whitespace, uppercase chỉ theo quy tắc đã ghi, không tự xóa ký tự có ý nghĩa.
- VND: tổng tiền quy về quantum `1`, ROUND_HALF_UP. Số lượng và đơn giá có tối đa 6 chữ số thập phân. Lưu giá trị trước làm tròn; không so float.
- MVP: `expected_line_net = round_vnd(quantity * unit_price)`; `expected_subtotal = sum(line.net_amount)`; `expected_grand_total = subtotal + tax_total`.
- Không suy ra thuế suất pháp định. So sánh số thuế đã đọc và tổng tiền; không mặc định thuế 10%. Phát hiện cấu trúc thuế/giảm giá ngoài phạm vi thì block.
- `discount_total`/`shipping_total` chỉ là 0 khi chứng từ được xác nhận nằm trong phạm vi không có khoản này; nếu không rõ thì null + yêu cầu xác nhận. Không tự hiểu missing là zero.

## 8. Quy tắc đối soát — code quyết định

Policy mặc định có version `reconciliation-v1`:

```json
{
  "currency": "VND",
  "amount_absolute_tolerance": "1",
  "unit_price_absolute_tolerance": "0.000001",
  "quantity_absolute_tolerance": "0.000001",
  "allow_partial_invoice": false,
  "require_human_approval": true
}
```

Tolerance là quyết định sản phẩm của dự án demo, cấu hình được, không phải chuẩn kế toán chung. Thay policy tạo version mới; không đổi âm thầm kết quả run cũ.

### 8.1 Chọn PO và ánh xạ dòng

- Nếu PO number có trên hóa đơn, tìm exact trong org. Nếu không có: người dùng chọn PO của đúng supplier; không cho LLM tự chốt.
- Chỉ auto-map SKU khi SKU tồn tại và unique ở cả hai phía. Nếu SKU thiếu, trùng, khác unit hoặc có nhiều ứng viên: yêu cầu người review ánh xạ thủ công.
- Fuzzy description chỉ là gợi ý. Lưu mapping `invoice_line_id -> po_line_id`, actor và origin. Mapping thủ công không sửa số liệu hóa đơn.
- Full matching: số dòng có thể khác thứ tự nhưng mỗi PO line phải được map đúng một invoice line. Không gom/tách dòng, đổi đơn vị hoặc gộp partial trong P0.
- Validation phát hiện thiếu/thừa dòng, map nhiều-một, SKU/unit chưa xác nhận, qty hoặc giá thiếu.

### 8.2 Rule catalog

| Code | Severity | Điều kiện |
|---|---|---|
| REQUIRED_FIELD_MISSING | BLOCK | Thiếu field quan trọng để kiểm tra |
| AMBIGUOUS_NUMBER / DATE | BLOCK | Chưa chuẩn hóa chắc chắn |
| UNSUPPORTED_CASE | BLOCK | Currency khác VND, partial, discount/fee/credit hoặc cấu trúc ngoài scope |
| SUPPLIER_MISMATCH | BLOCK | Tax ID/supplier không khớp PO |
| PO_NOT_FOUND / PO_CLOSED | BLOCK | Không có PO dùng được |
| LINE_MAPPING_REQUIRED | BLOCK | Mapping chưa đầy đủ hoặc chưa xác nhận |
| QUANTITY_MISMATCH | BLOCK | Sai qty ngoài tolerance |
| UNIT_PRICE_MISMATCH | BLOCK | Sai unit price ngoài tolerance |
| UNIT_MISMATCH | BLOCK | Đơn vị khác, chưa có mapping hợp lệ |
| LINE_AMOUNT_MISMATCH | BLOCK | Qty × price không khớp line net |
| SUBTOTAL_MISMATCH | BLOCK | Tổng line không khớp subtotal |
| GRAND_TOTAL_MISMATCH | BLOCK | Subtotal + tax không khớp grand total |
| DUPLICATE_APPROVED_INVOICE | BLOCK | Business key đã có hóa đơn approved |
| DUPLICATE_PENDING_INVOICE | WARN | Cùng business key với hồ sơ chưa approved |
| DESCRIPTION_DIFFERENCE | WARN | SKU đã map nhưng mô tả khác |
| SOURCE_NOT_TEXT_VERIFIED | WARN | Chỉ có trang ảnh hoặc nguồn chưa xác nhận |

`reconciliation_status`: `PASS` khi không issue; `WARNING` khi chỉ WARN; `BLOCKED` khi có ít nhất một BLOCK. Job/infra failure không được biến thành PASS hoặc BLOCKED nghiệp vụ: job là FAILED và kết quả reconciliation chưa có.

Approve được khi PASS hoặc WARNING đã acknowledge hết warning bằng thao tác reviewer. BLOCKED không có nút override trong MVP. Reviewer có thể sửa dữ liệu đọc sai hoặc reject vì chứng từ thực sự sai. Một người có quyền reviewer có thể tự review tài liệu mình upload trong demo; không yêu cầu phân tách hai người.

### 8.3 Duplicate và race condition

- Byte duplicate: unique `(org_id, sha256)`; upload lại trả document/invoice hiện có, không tính phí extraction lại tự động.
- Business key: hash canonical JSON gồm `supplier_id`, `invoice_series` (empty nếu xác nhận không có series), `invoice_number`, `invoice_year`. Giữ các giá trị gốc để giải thích.
- Thiếu invoice number/year/supplier: chưa tạo được key, phải review. Series chưa biết khác với series được xác nhận không có.
- Prior rejected invoice chỉ là audit/history; prior pending tạo warning; prior approved chặn.
- Hai request approve cạnh tranh phải được giải quyết bằng lock và unique constraint trong DB, không chỉ bằng nút disabled ở frontend.
- Sau approve, đóng PO và ghi approval record cùng một transaction. Lỗi unique trả 409 có mã rõ, không partial commit.

## 9. Workflow, review và version

### State nghiệp vụ

| Current | Hành động / điều kiện | Next |
|---|---|---|
| UPLOADED | Enqueue thành công | PROCESSING |
| PROCESSING | Extraction có dữ liệu hoặc lỗi đọc cần người sửa | REVIEW_REQUIRED |
| PROCESSING | Hạ tầng/model lỗi hết retry | PROCESSING_FAILED |
| PROCESSING_FAILED | Retry do user có quyền | PROCESSING |
| REVIEW_REQUIRED | Sửa/chọn PO/map line | REVIEW_REQUIRED, version hoặc run mới |
| REVIEW_REQUIRED | Run hiện hành PASS/WARNING | READY_FOR_APPROVAL |
| READY_FOR_APPROVAL | Có chỉnh sửa mới | REVIEW_REQUIRED |
| READY_FOR_APPROVAL | Reviewer approve hợp lệ | APPROVED |
| REVIEW_REQUIRED / READY_FOR_APPROVAL | Reviewer reject + reason | REJECTED |

APPROVED và REJECTED là terminal ở MVP. Không sửa/extract lại/approve lại dưới danh nghĩa run mới; hành động lặp với cùng idempotency key chỉ trả kết quả trước đó. Rejected có thể tạo một hồ sơ mới với file được chỉnh thực sự; không reopen ngầm. Exact same-byte upload vẫn trỏ về hồ sơ đã có.

Extraction chứa field thiếu/schema không sửa được là REVIEW_REQUIRED cùng issues nếu còn phục hồi thủ công được. File hỏng/password-protected bị từ chối tại upload/preflight; API provider lỗi là PROCESSING_FAILED. Phân biệt failure code ngay từ đầu.

### Optimistic concurrency

- Mọi mutation review gửi `expected_version_id` và/hoặc `expected_version_no`.
- Sửa field tạo version mới trong transaction khóa invoice row; validate expected version; stale trả 409.
- Chọn PO/map line tạo immutable reconciliation run, gắn invoice_version + PO + policy + mapping. `snapshot_hash` bao gồm tất cả các thành phần này.
- Quyết định gửi cả expected invoice version, reconciliation id và snapshot hash. Backend kiểm tra một lần nữa ngay lúc commit approve.
- Không dùng dữ liệu sửa trên browser làm bằng chứng run đã PASS. Backend tự chạy/kiểm lại rule từ snapshot lưu trong DB.

### Job delivery, leases và idempotency

1. Tạo job và outbox trong cùng transaction với domain mutation.
2. Dispatcher gửi ID lên Celery. Gửi rồi chết trước đánh dấu outbox có thể gửi lại: chấp nhận at-least-once.
3. Worker claim job bằng DB lock/CAS; cấp fencing token tăng dần, lease 60s, heartbeat 15s. Worker cũ không được commit khi fencing token đã đổi.
4. Task duplicate thấy job DONE hoặc lease còn hiệu lực thì no-op hoặc retry theo policy; không chạy inference trùng song song.
5. Recovery scanner tìm job lease hết, backoff và requeue qua outbox; giới hạn số retry.
6. Checkpoint output của từng bước bằng unique key `(job_id, step, input_hash)`. Worker restart tái sử dụng output đã commit; output inference chưa commit có thể phải gọi lại, phải ghi nhận khả năng này.
7. Side effect tạo decision, approval hoặc export dùng unique business key/idempotency và transaction. Không tuyên bố exactly-once cho queue hoặc external API.
8. Review decision được lưu cùng outbox resume. Crash sau decision trước graph resume sẽ được recovery tiếp tục; graph resume lặp không tạo thêm approval record.

### LangGraph ở M4

Các node: load snapshot → prepare pages → extract → normalize → validate → reconcile (nếu đủ mapping) → await review → finalize decision. Việc sửa có thể tạo run mới; chỉ resume đúng active run, không dùng thread cũ để approve snapshot mới.

- `thread_id` là ID nội bộ do backend tạo, ánh xạ org/run sau authorization.
- Checkpointer PostgreSQL, payload chỉ giữ ID/reference và JSON nhỏ; không nhét PDF/ảnh base64 vào state.
- Pause bằng interrupt, không giữ HTTP request hoặc worker chờ người dùng. Interrupted job chuyển WAITING_REVIEW và trả worker slot.
- Resume node có thể chạy lại từ đầu: không đặt side effect chưa idempotent trước interrupt.
- API ghi decision transaction trước; worker resume bằng ID decision, đọc bản đã lưu; model không cung cấp quyết định.
- App trạng thái APPROVED chỉ do approval service thiết lập. Checkpoint với state “approved” không thay thế invariant DB.

## 10. REST API contract

Prefix `/api/v1`; dùng Pydantic response schema. Pagination `limit` 1–100 và cursor. Filter không tin SQL do client gửi. JSON decimal là string; file response có content-disposition phù hợp.

| Method / path | Quyền | Contract chính |
|---|---|---|
| POST /auth/login | Public có rate limit | email, password, org selection hợp lệ → access token |
| GET /auth/me | Auth | identity, org, role |
| POST /suppliers | operator/reviewer | Tạo nhà cung cấp, validate unique tax ID |
| GET /suppliers | Auth | Danh sách trong org |
| POST /purchase-orders | operator/reviewer | Tạo PO JSON immutable |
| POST /purchase-orders/import | operator/reviewer | CSV batch, validate hết trước commit; lỗi theo row |
| GET /purchase-orders[/{id}] | Auth | Chỉ trong org |
| POST /documents | operator/reviewer | Multipart file + number/date locale → document, invoice, job; 202 khi tạo |
| GET /documents/{id}/content | Auth | Stream file, private; không trả storage path |
| GET /documents/{id}/pages/{page_no} | Auth | Preview page đã kiểm tra quyền |
| GET /jobs/{id} | Auth | status, step, attempts, error, timestamps |
| POST /jobs/{id}/retry | operator/reviewer | Chỉ failed job hợp lệ, không terminal invoice |
| GET /invoices | Auth | Filter status/supplier, pagination |
| GET /invoices/{id} | Auth | Current version, evidence, current reconciliation, allowed actions |
| GET /invoices/{id}/versions | Auth | Lịch sử immutable |
| POST /invoices/{id}/versions | operator/reviewer | Full draft + expected version + reason; validate, tạo revision |
| POST /invoices/{id}/reconciliations | operator/reviewer | expected version, po_id, line_mapping → run đã tính bằng code |
| POST /invoices/{id}/decisions | reviewer | APPROVE/REJECT, expected snapshot, acknowledge warnings, reason |
| GET /invoices/{id}/audit | Auth | Events trong org |
| GET /exports/invoices/{id}.json | Auth | Chỉ bản APPROVED, gắn version/run |
| GET /exports/invoices/{id}.csv | Auth | Chỉ bản APPROVED, chống spreadsheet formula injection |
| GET /health/live, /health/ready | Infra | Liveness và readiness tách; không lộ secret |

`POST /invoices/{id}/versions` nhận full canonical draft để tránh patch tùy ý vào metadata; whitelist field được sửa, bắt buộc reason và manual provenance cho field thay đổi. Ngăn sửa org, actor, trạng thái, approval hoặc provider output.

Các POST tạo tài nguyên/command hỗ trợ `Idempotency-Key`. Cùng key + cùng canonical request hash trả resource/response cũ; cùng key khác payload trả 409. Upload hash gồm bytes và tham số; file SHA duplicate vẫn được giải quyết độc lập theo constraint. Phạm vi key: org + actor + route + key. Lưu ít nhất 7 ngày cho demo, approval uniqueness tồn tại lâu dài.

Error envelope:

```json
{
  "error": {
    "code": "STALE_REVIEW_VERSION",
    "message": "Dữ liệu đã thay đổi. Hãy tải lại trước khi duyệt.",
    "request_id": "req-demo-001",
    "details": {"current_version_no": 3}
  }
}
```

Status: 401 chưa đăng nhập; 403 sai role; 404 tài nguyên không thuộc org hoặc không tồn tại; 409 conflict/state/version; 413 file lớn; 415 sai loại; 422 schema/upload không hợp lệ; 429 rate limit; 503 dependency/provider chưa cấu hình. Không trả traceback hoặc nội dung SDK credential lỗi.

Ví dụ approve:

```json
{
  "action": "APPROVE",
  "expected_version_id": "22222222-2222-4222-8222-222222222222",
  "reconciliation_id": "33333333-3333-4333-8333-333333333333",
  "expected_snapshot_hash": "sha256-demo-snapshot",
  "acknowledged_warning_codes": ["SOURCE_NOT_TEXT_VERIFIED"],
  "note": "Đã kiểm tra số lượng và số tiền trên trang 1."
}
```

CSV PO: `po_number,supplier_tax_id,currency,line_no,sku,description,unit,quantity,unit_price`. UTF-8; comma delimiter; decimal không group separator; quote text có comma; lặp PO header phải đồng nhất; import all-or-nothing. Header schema và mẫu tải được từ UI. Numeric input vượt precision trả lỗi, không truncate.

## 11. Extraction provider và xử lý tài liệu

### Contract nội bộ

```python
class ExtractionProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    async def extract_invoice(
        self, request: ExtractionRequest
    ) -> ExtractionResult: ...
```

`ExtractionRequest`: page refs/text/images, schema version, locale, prompt version, limits và run id. Không truyền DB session vào provider. `ExtractionResult`: parsed candidate nullable, validation issues, raw output reference, usage, actual model và provider metadata. Provider result chưa được phép cập nhật domain trực tiếp.

Capabilities: text/image input, structured output strategy, max pages/bytes/tokens và configured model. Preflight fail rõ ràng khi input không được hỗ trợ; không silent truncate trang, hàng hoặc context.

### PDF/image pipeline

1. Stream upload vào private temp với byte limit; kiểm tra magic bytes và parser, không chỉ extension.
2. Reject encrypted/password PDF, file hỏng, nhiều hơn 10 trang, ảnh quá 25 megapixel hoặc tổng render vượt resource budget. MIME allowlist không bao gồm SVG/HTML.
3. Text extraction theo trang; render preview có kiểm soát DPI, pixel và dung lượng.
4. Native PDF: text kèm page markers là baseline. Nếu text quá ít/không usable hoặc bảng không đọc được: chọn vision path theo capabilities, ghi routing reason. Không coi text dài là bảo đảm layout đúng.
5. Scan/image: dùng vision-capable model của provider. Không bắt buộc cài OCR riêng trong MVP.
6. Multi-page: gửi trang đã đánh số trong một request nếu nằm trong giới hạn model. Vượt giới hạn sau render thì báo lỗi rõ; page-wise merge là P1 và phải kiểm thử mất/trùng line trước khi bật.
7. Normalize, validate schema và domain. Có tối đa một schema repair trong tổng invocation budget; không tự sửa giá trị thiếu bằng suy đoán.
8. Save raw/canonical/evidence, usage và version. Không log file/prompt đầy đủ.

Các giới hạn sản phẩm ở trên không phải giới hạn mặc định của cloud. Adapter phải kiểm tra cả hai, lấy mức chặt hơn.

### Bedrock P0

- Dùng SDK trực tiếp trong adapter. Chọn model có vision + structured output/tool use phù hợp theo tài liệu, quyền và region thực tế.
- Native structured output khi model/API hỗ trợ; nếu cần schema-as-tool thì đó là công cụ trả kết quả extraction, không phải action nghiệp vụ. Luôn chạy Pydantic/domain validation.
- Config model ID/inference profile và region bằng env; không hard-code tên model “mới nhất”.
- Lưu strategy thực tế: `native_schema`, `tool_schema`, hoặc `json_validated`. Không quảng cáo chế độ strict nếu chỉ parse JSON bằng prompt.
- Xử lý refusal, truncation/token limit, schema invalid, throttling, auth và input không hỗ trợ thành typed errors khác nhau.
- Boto3 sync phải chạy trong threadpool phù hợp hoặc trong sync worker, không block event loop FastAPI.
- Tổng physical model invocations tối đa 3/run, bao gồm network retry và repair. Backoff exponential+jitter, tôn trọng Retry-After trong deadline; 401/403/input/schema không hợp lệ không retry mù.
- Deadline ví dụ: connect 10s, provider attempt 180s, processing job 10 phút; configurable. Schema compilation của provider có thể làm lần đầu chậm; warm-up smoke test và ghi cold/warm riêng.
- Chỉ bật paid live calls khi `ALLOW_LIVE_MODEL_CALLS=true` và cấu hình đủ. Không có key thì thông báo setup, không fallback thành fixture thành công.

**Lựa chọn ngôn ngữ:** không chọn Textract làm OCR mặc định cho chứng từ Việt vì danh sách ngôn ngữ chính thức không có tiếng Việt [S3]. P1 có thể thử Azure Document Intelligence Read/Layout theo bảng hỗ trợ ngôn ngữ [S4], rồi đo trên chính dữ liệu; không suy từ OCR language support sang chất lượng prebuilt invoice extraction.

### Fixture provider

- Dùng manifest ánh xạ SHA256 của file demo tới response cố định; không trả kết quả trống hoặc một hóa đơn chung cho mọi file.
- File chưa có trong manifest trả `FIXTURE_NOT_FOUND`, UI giải thích chọn sample hoặc cấu hình provider thật.
- Badge “Fixture demo — dữ liệu trích xuất có sẵn” xuất hiện trên UI, API metadata, export demo và README.
- Fixture kiểm tra plumbing và rule, không dùng để báo cáo độ chính xác AI.

### Foundry P1

- Chọn một deployment và endpoint thực tế; dùng SDK/API đúng với deployment đó, đối chiếu [S5]. Không lẫn project endpoint với model inference endpoint.
- Cùng schema nội bộ, cùng labels, cùng policy/routing khi so sánh; khác biệt capabilities ghi rõ.
- Ưu tiên managed identity/Entra ID khi deploy, API key local qua secret config khi được hỗ trợ.
- Auth test bằng stub; live verification chỉ chạy khi có credential và được bật.
- Báo cáo đúng “provider comparison”, không suy ra AWS/Azure nói chung tốt hơn từ một model trên tập nhỏ.

## 12. Giao diện và trải nghiệm demo

UI tiếng Việt; dùng business terms, không bắt người dùng chọn temperature, tool schema, vector DB hoặc graph node.

1. **Đăng nhập:** demo accounts tạo từ seed, mật khẩu local do env chỉ định; không commit mật khẩu cố định dùng cho deploy.
2. **Danh sách chứng từ:** file, supplier, số hóa đơn, tổng tiền, trạng thái, ngày, filter và lỗi có hành động khắc phục.
3. **Upload:** drag/drop, giới hạn file rõ, chọn sample; progress theo bước. Trạng thái extracting không giả tăng phần trăm chính xác.
4. **Review hai cột:** trái PDF/image và chuyển trang; phải header/line fields, giá trị thiếu, cảnh báo; click field mở trang nguồn. Không có bbox thì chỉ mở trang.
5. **PO/mapping:** chọn đúng supplier, PO; bảng so sánh qty/price/amount và ánh xạ thủ công khi cần.
6. **Duyệt:** hiển thị snapshot đang review; lý do chặn hoặc warning cần acknowledge; reviewer có approve/reject. Không có nút approve cho operator.
7. **Lịch sử:** timeline thay đổi, version, người sửa, thời điểm, quyết định; không cho sửa log.
8. **Export:** chỉ approved, gắn version và reconciliation; button disabled kèm lý do ở trạng thái khác.

Accessibility: label field, keyboard navigation, focus khi error, không chỉ dùng màu để báo mismatch. API vẫn bảo vệ quyền khi UI button bị ẩn. Không đưa full processing exception lên người dùng.

## 13. Authentication, tenant isolation và file safety

- Backend xác thực password hash bằng thư viện uy tín, token JWT expiry ngắn, verify signature/issuer/audience/expiry. Mỗi token gắn một org membership đã xác minh.
- Frontend giữ access token trong memory; MVP hết hạn/refresh trang có thể đăng nhập lại. Không lưu bearer token dài hạn trong localStorage. CORS explicit origin, TLS khi public.
- API kiểm tra role và org cho mọi object kể cả job, page, audit, export và parent-child ID. Seed org A/B để test isolation.
- Storage key do server tạo; filename chỉ metadata, sanitize khi download. Không fetch arbitrary URL để OCR trong MVP. Không public bucket hoặc static mount `/uploads`.
- File render/parse trong worker có memory/time budget; không chạy embedded code. Xử lý upload không làm mất request DB transaction dài.
- Chỉ render raw text bằng escaping; JSON extraction không được chèn HTML. CSV escape ô có thể bắt đầu công thức (`=`, `+`, `-`, `@`, tab/CR hoặc leading whitespace rồi ký tự nguy hiểm), phân biệt text và numeric field đã validate.
- Nội dung tài liệu là untrusted data. Prompt yêu cầu chỉ extraction; instruction trong hóa đơn không cấp quyền tool, mạng hoặc thay đổi approval.
- Demo công khai dùng sample synthetic; upload tài liệu cá nhân là private theo tài khoản. Không public labels/raw data thật.
- P0 không có delete endpoint. Việc hết hạn lưu và xóa theo yêu cầu phải được mô tả trong runbook trước khi dùng dữ liệu thật.

## 14. MCP, RAG và function calling — backlog có thứ tự

### M5a: MCP read-only

Dùng official Python MCP SDK đúng phiên bản đã pin. Chọn Streamable HTTP cho demo remote; stdio tùy chọn local. Token được cấp bằng CLI, lưu hash, scopes và org; credential không nằm trong prompt.

Tools: `list_invoices`, `get_invoice_summary`, `get_reconciliation`, `get_purchase_order`. Trả JSON giới hạn dòng/trường; không trả raw PDF mặc định. Không có tool approve/reject/payment, không nhận org_id từ model.

Cùng authorization/application services với REST. Kiểm thử một client độc lập list tools và call tool; unauthorized cross-org phải thất bại. README phân biệt MCP tool với REST API nghiệp vụ.

### M5b: RAG quy định nội bộ

Chỉ ingest 5–10 tài liệu demo có chủ đích: quy định tiếp nhận, quy trình xử lý chênh lệch, hướng dẫn review. Tài liệu ghi rõ synthetic và không đại diện chính sách kế toán thực tế.

Chunks lưu org_id, document/version, section/page, content hash; pgvector + keyword search khi cần. Câu trả lời trích đúng nguồn và phiên bản. Không có nguồn thì nói chưa đủ thông tin. Policy retrieved là hướng dẫn giải thích; không tự thay đổi tolerance hoặc rule code đang áp dụng.

### M5c: Chat copilot

Ví dụ: “Vì sao hóa đơn này bị chặn?” Model đọc kết quả rule, PO và evidence rồi giải thích. Tool read-only; tối đa 4 vòng và 6 tool calls/turn, deadline rõ, kiểm tra schema và quyền trước mỗi lần.

Phải có luồng tool-call → backend validate/execute → tool-result đúng call ID → response. Không giả function calling bằng cách prompt model viết tên hàm. Trace chỉ lưu metadata cần thiết, không yêu cầu lưu chain-of-thought.

### M6 tùy chọn: multi-agent experiment

Chỉ cho agent đọc chứng từ, agent đối chiếu bằng tool đã có, agent giải thích/kiểm tra dẫn nguồn. Quy tắc đối soát và approval giữ trong code.

Baseline single-agent và multi-agent dùng cùng model family/config khi khả thi, cùng corpus và budget được báo rõ. Đo chất lượng, model call, token, latency và cost. Nếu không có cải thiện đáng kể, giữ single-agent trong bản sản phẩm; vẫn báo cáo thí nghiệm trung thực.

## 15. Dataset và evaluation

### 15.1 Demo fixtures bắt buộc từ M1

Tạo 12 tài liệu synthetic có watermark “DEMO”, PO đi kèm, canonical labels và expected findings:

1. Native PDF hợp lệ; 2. Scan hợp lệ; 3. Invoice hai trang; 4. Thiếu invoice number;
5. Quantity mismatch; 6. Unit price mismatch; 7. Grand total sai;
8. Supplier sai; 9. Scan lại invoice có business key trùng;
10. Dòng thiếu SKU cần mapping; 11. Unsupported currency/discount;
12. Document có instruction prompt injection.

Thêm file invalid cho parser, quá giới hạn, password PDF trong test fixtures. Fixture API test phải dùng files thật có bytes, không chỉ JSON label.

### 15.2 Corpus đánh giá AI

Mục tiêu khởi đầu 90 hóa đơn: 30 dev, 20 validation, 40 held-out test. Tách theo layout/vendor template: 3 family dev, 2 validation, 4 test, mỗi family khoảng 10 tài liệu; các scan/native biến thể của một tài liệu phải ở cùng split. Dữ liệu nhỏ chỉ là benchmark dự án, không suy rộng cho mọi doanh nghiệp.

Bao gồm native/scan, một/nhiều trang, Việt/Anh, dấu phân cách, số thiếu, nhiều dòng và trường hợp ngoài scope. PO reference và labels do người tạo/annotator xác nhận. Bổ sung chứng từ thực đã ẩn danh và được phép dùng nếu có; báo kết quả synthetic và real riêng. Không dùng LLM output làm ground truth chưa kiểm tra.

Manifest mỗi sample: sample_id, file_sha256, source, license/consent, synthetic flag, layout_family, split, language, modality, label_path, po_reference, expected_findings. Script verify không có hash hoặc family overlap giữa split; số liệu training prompt/example không lấy từ test.

### 15.3 Metrics

| Lớp | Metric và cách đo |
|---|---|
| Schema | Schema-valid response / tất cả input được gửi, báo refusal/truncation/failure riêng |
| Header extraction | Exact match chuẩn hóa theo field; missing và hallucinated field tính sai |
| Số tiền/ngày/ID | Exact match sau normalization; không dùng tolerance rộng để che lỗi đọc |
| Line items | One-to-one matching row theo SKU khi có, otherwise label alignment cố định; precision/recall/F1 và độ đúng cell |
| Reconciliation | Precision/recall theo từng issue code; tỷ lệ PASS sai trên invoice có chênh lệch |
| Provenance | Đúng trang, verified quote khi có text, tỷ lệ evidence không tồn tại |
| Human effort | Thời gian và số field cần sửa; trước/sau trên cùng nhiệm vụ có ghi điều kiện |
| Runtime | Median/p95 latency, throughput với concurrency công bố, failure/retry rate |
| Cost | Tokens/calls, cost estimate khi có bảng giá cấu hình; unknown không được ghi 0 |

Rule engine test trên canonical labels trước để tách lỗi rule khỏi lỗi extraction. Sau đó end-to-end từ file raw để đo lỗi tích lũy. Human correction không được nâng accuracy extraction. Tỷ lệ PASS sai chỉ đánh giá trước manual correction.

Evaluation runner lưu git SHA, dependency lock hash, dataset manifest hash, split, model/deployment/region, prompt/schema/policy versions, routing, temperature nếu hỗ trợ, timestamp và raw prediction reference. Báo n mẫu và denominator của mọi metric. Failed sample không biến mất khỏi báo cáo.

### 15.4 Baselines

A. Text-only extraction trên native PDF. B. Vision extraction cho scan. C. Routing native-text/vision. So sánh trên cùng test set phải báo nhóm nào không được hỗ trợ; không so một nhóm dễ với một nhóm khó rồi kết luận model tốt hơn.

Chạy validation để chỉnh prompt/routing. Khi freeze, test chỉ chạy để báo cáo; thay đổi sau khi nhìn test phải đánh dấu đã sử dụng test và tạo test mới nếu cần kết luận độc lập.

### 15.5 Targets ban đầu, không phải kết quả đã đạt

- Header critical fields ≥95% normalized exact match trên clean synthetic subset.
- Line cells ≥90% trên subset có schema được hỗ trợ.
- Rule tests 100% expected outcomes; không có false approval trong regression suite.
- Approve/retry concurrency tests không tạo duplicate record.
- Live AI latency/cost đo trước rồi đặt SLO phù hợp; chưa cam kết thời gian model khi chưa có benchmark.

Không đạt target vẫn giao báo cáo trung thực và error taxonomy; không sửa denominator, bỏ ca khó hoặc dùng fixture để nâng số.

## 16. Kiểm thử có ý nghĩa

### Unit tests

- Decimal locale/date, leading zero, missing-vs-zero, rounding và precision.
- Từng reconciliation rule, tolerance boundary, mapping thiếu/trùng/khác thứ tự.
- Duplicate key và scope năm/series/org.
- Allowed transitions, approve predicate, warning acknowledge.
- Provider response invalid/refusal/truncated, timeout, retry budget.

### PostgreSQL integration tests

- Migration từ DB rỗng, constraints/FKs, rollback toàn bộ import sai.
- Hai upload cùng bytes: một document/invoice, không hai job active.
- Hai request cùng Idempotency-Key, cùng/khác payload.
- Hai approve cùng invoice, cùng PO hoặc cùng business key; chỉ một record hợp lệ, còn lại idempotent hoặc 409 đúng nghĩa.
- Edit-vs-approve race: version mới thắng thì approve cũ bị chặn; approve đã commit thì edit bị chặn.
- Crash sau outbox publish, lease expiry và stale worker: không duplicate final side effect.
- Decision lưu rồi worker chết: restart resume đúng snapshot, không ghi hai approval.
- Org A không đọc/ghi file, job, invoice, version, PO, audit, export của B.

Không thay PostgreSQL bằng SQLite cho các test lock/unique/transaction.

### UI end-to-end

1. Login → sample upload → job → review → mapping → reviewer approve → export đúng version.
2. Quantity mismatch chặn approve, sửa dữ liệu tạo revision, chạy lại → approve được khi hợp lệ.
3. Reload/restart ở trạng thái chờ review vẫn tiếp tục được.
4. Stale tab nhận 409 và hướng dẫn reload; không mất bản gốc.
5. Operator không approve được, kể cả gọi API trực tiếp.
6. Fixture badge và unknown-file message chính xác.

### Live provider tests

Opt-in riêng, không chạy trong CI mặc định. Tối thiểu một native PDF và một scan qua provider thật; lưu model/region/usage. Có mock contract không có nghĩa live test PASS.

## 17. Observability và ngân sách

Structured logs: request_id, org_id dạng opaque ID, job/run id, step, duration, provider/model, tokens, retry count và error code. Không log raw tài liệu, bearer token hoặc credential.

Metrics: queue depth, active/failed jobs, processing latency theo step, provider failures, token usage, extraction review rate và rule findings. Có trang kỹ thuật/CLI nội bộ, không đưa hạ tầng lên luồng kế toán.

Budget config: max_pages=10, max_file_mib=15, worker concurrency=2, provider invocations/run=3, bounded output tokens theo schema và model. Giới hạn daily live runs cấu hình được; ghi nhận từ DB, không dùng một counter memory dễ reset.

Cost estimate lấy bảng giá versioned do người vận hành cấu hình. Nếu thiếu usage/pricing của modality thì cost=null + reason. Theo dõi cloud budget bằng alert; không gọi nó là hard stop billing tuyệt đối.

## 18. Triển khai và lệnh chuẩn

Local: PostgreSQL/Redis/private file volume; API và worker dùng cùng config. Public pilot: một VM/container host có Docker Compose + TLS reverse proxy, private S3, DB có backup. Ghi rõ giới hạn pilot single-host; managed DB/queue có thể nâng sau.

Không expose Redis/PostgreSQL trực tiếp ra Internet. Credential runtime role/secret config, không build vào image. Migration là bước deploy riêng, không chạy đồng thời mỗi replica. Backup/restore rehearsal cho DB + object refs trước pilot có dữ liệu thật.

Các command dưới đây là interface mà Codex phải tạo, chưa phải command đã tồn tại:

```bash
cp .env.example .env
make bootstrap
make up
make migrate
make seed-demo
make test-unit
make test-integration
make test-e2e
make lint
make typecheck
make eval-fixture
make eval-live SPLIT=test PROVIDER=bedrock
make smoke
```

README có Windows/WSL hoặc PowerShell tương đương nếu Make không có. `make up` phải có healthcheck và retry connect hữu hạn, không dựa vào sleep cố định. `make down` không xóa dữ liệu; reset demo là lệnh riêng có tên rõ.

Env mẫu (tên là đề xuất; implementation được chỉnh đồng bộ docs/tests):

```dotenv
APP_ENV=development
DATABASE_URL=postgresql+psycopg://idp:change-me@postgres:5432/idp
REDIS_URL=redis://redis:6379/0
STORAGE_BACKEND=local
DOCUMENT_STORAGE_PATH=/var/lib/idp/documents
EXTRACTION_PROVIDER=fixture
ALLOW_LIVE_MODEL_CALLS=false
AWS_REGION=
BEDROCK_MODEL_ID=
FOUNDRY_MODEL_ENDPOINT=
FOUNDRY_DEPLOYMENT=
MAX_DOCUMENT_PAGES=10
MAX_FILE_MIB=15
JWT_SECRET=
DEMO_PASSWORD=
```

Bootstrap yêu cầu tạo secret/mật khẩu local hoặc tự tạo bằng secure random trong `.env` không commit. Không để môi trường production khởi động với secret trống/default. Thông số endpoint/identity của Foundry bổ sung theo deployment thật ở M5.

## 19. Milestones và acceptance criteria

Lịch 6–8 tuần là ước lượng cho P0 + một phần P1 ở 15–20 giờ/tuần; phụ thuộc dữ liệu, quyền cloud và khả năng debug. Không dùng thời gian làm tiêu chí hoàn thành. M0–M3 đủ vertical slice; M4–M6 tăng giá trị portfolio.

### M0 — setup và domain nền

Deliver: monorepo, lockfiles, Compose, migration nền, auth/roles, seed hai org, supplier/PO schema, Decimal/rule domain, provider/storage interfaces, docs/decisions và progress.

Acceptance: boot từ README, migration DB rỗng, auth và isolation test pass, unit money test pass. Không chỉ tạo type class chưa kết nối API.

### M1 — vertical slice offline

Deliver: upload thật, bytes storage, fixture manifest, outbox/worker, invoice/detail/review UI, version sửa, mapping PO, rule result, approve/reject và export. Bắt đầu 3 sample (valid/mismatch/scan), mở rộng 12 trước M3.

Acceptance: một flow end-to-end từ UI đến DB; raw input đọc được sau restart; role và stale version được kiểm tra; UI badge đúng; mismatch không approve được. Đây là mốc giao việc đầu tiên sau M0.

### M2 — provider thật và provenance

Deliver: native/scanned routing, Bedrock adapter, schema validation, evidence theo trang, usage, typed failure, retry/deadline, provider contract tests.

Acceptance: fixture regression pass; nếu có credential, native + scan live smoke pass và có kết quả thật. Nếu thiếu, adapter/contract hoàn thành nhưng ghi LIVE_PENDING; tiếp tục M3/M4 độc lập. Không đánh dấu P0 live AI done khi chưa chạy.

### M3 — độ tin cậy, dữ liệu và evaluation

Deliver: đầy đủ 12 fixtures, corpus split manifest, runner/report, concurrency tests, lease recovery, idempotent decisions, audit và file validation.

Acceptance: toàn bộ suite liên quan pass; process restart không mất review; approve race không trùng; eval report phân biệt fixture/live và synthetic/real. Có error analysis tối thiểu các nhóm lỗi thực sự quan sát được, không bịa đủ số nhóm.

### M4 — LangGraph persistence và bản triển khai

Deliver: thay orchestration bằng graph, interrupt/resume, PostgreSQL checkpoint, deployment/runbook, demo video script.

Acceptance: restart giữa extraction/review/finalize không tạo duplicate; worker không chờ người review; demo public dùng sample hoặc private pilot có auth/TLS; có lệnh và kết quả smoke. Nếu chưa có hosting thì chỉ ghi deployment-ready, không gọi deployed.

### M5 — chọn extension theo JD

Thứ tự đề xuất: MCP read-only → Foundry adapter → policy RAG → chat tool calling. Mỗi extension có contract/integration test và một demo cụ thể. Không cần tất cả để bắt đầu ứng tuyển.

Acceptance MCP: client độc lập list/call, negative auth/isolation test. Foundry: cùng eval contract, live state rõ. RAG: citations đúng source/version và no-answer case. Chat: tool call trace thật, quyền và budget được enforce.

### M6 — portfolio và thí nghiệm tùy chọn

Deliver: README tiếng Anh ngắn, kiến trúc, video 3–5 phút, report metric thật, tradeoffs, known limitations, hướng dẫn tái lập. Multi-agent chỉ khi các mốc trước đã đạt.

Acceptance: người khác chạy được fixture demo theo README; nội dung CV trỏ tới bằng chứng; không đưa target thành achieved metric.

## 20. Prompt tiếp theo theo mốc

### Prompt M2

```text
Đọc plan, docs/progress.md và code hiện có. Hoàn thành M2: native/scanned
pipeline, Bedrock adapter, provenance, validation, typed errors và usage.
Kiểm tra SDK/model/region hiện tại trước khi chọn API. Không dùng Textract
làm OCR tiếng Việt mặc định. Giữ fixture regression chạy không cần cloud.
Thêm test cho refusal/truncation/schema invalid/429/deadline và ảnh nhiều trang.
Nếu không có credential, ghi LIVE_PENDING; không giả model response.
```

### Prompt M3

```text
Hoàn thành M3 theo plan. Ưu tiên concurrency/idempotency/versioning/tenant
isolation và bộ dữ liệu gán nhãn. Chạy test transaction bằng PostgreSQL thật.
Tạo eval runner có split validation và held-out test theo layout family.
Không đưa fixture accuracy hoặc dữ liệu sau manual correction vào accuracy AI.
Xuất báo cáo chứa n mẫu, failures, config và error analysis từ lần chạy thật.
```

### Prompt M4

```text
Hoàn thành M4. Chuyển orchestration hiện có sang LangGraph PostgreSQL
checkpoint, giữ domain services và approval invariants. Human review phải
release worker, resume bằng decision đã lưu, thread/org/snapshot được kiểm tra.
Kiểm thử crash/restart và duplicate resume. Hoàn thiện deployment/runbook,
health checks và smoke commands. Báo rõ deployment-ready hay đã deploy thật.
```

### Prompt M5

```text
Đọc plan và triển khai M5a trước: MCP read-only bằng SDK official đã pin.
REST/MCP dùng cùng service và auth policy. Viết client demo độc lập list/call
và test sai token/cross-org. Không thêm tool approve/reject/payment.
Sau khi mốc này đạt, cập nhật progress và mới chọn extension tiếp theo theo JD.
```

## 21. Definition of Done để đưa vào CV

- [ ] Clone mới chạy fixture demo theo README, có migration và seed.
- [ ] File thật → extract → sửa → đối soát → reviewer approve/reject → export.
- [ ] Một provider live đã chạy native PDF và scan; metadata/usage được ghi.
- [ ] Giá trị thiếu, sai tổng, trùng chứng từ, mapping mơ hồ có hành vi rõ.
- [ ] Không approve snapshot stale, BLOCKED hoặc sai quyền.
- [ ] Crash/retry/concurrent review không gây duplicate approval.
- [ ] Test hai organization và file access pass.
- [ ] Bộ eval giữ riêng và report có dữ liệu thực, thất bại và giới hạn.
- [ ] Median/p95 latency, token/cost estimate có cấu hình và sample count.
- [ ] README, kiến trúc, video, tradeoffs, live/deploy status trung thực.
- [ ] Secret và dữ liệu cá nhân không nằm trong repo/demo public.

Nếu viết CV trước khi live/deploy hoàn thành, dùng từ “implemented/tested locally” phù hợp thực tế. Chỉ ghi production khi có bằng chứng vận hành tương ứng, không dựa vào Docker hoặc một demo thành công.

## 22. Tài liệu tham khảo đã kiểm tra

Các link có thể thay đổi; trước khi implement adapter, kiểm tra lại SDK/model/deployment thực tế. Quy tắc tiền tệ, phạm vi và targets trong plan là quyết định thiết kế của dự án, không lấy làm quy định kế toán.

- [S1] AWS Bedrock structured outputs: https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html — schema enforcement phụ thuộc API và model; vẫn cần semantic/domain validation.
- [S2] AWS Bedrock Converse API: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html — request/response và input content.
- [S3] Textract quotas/language list: https://docs.aws.amazon.com/textract/latest/dg/limits-document.html — không có tiếng Việt trong danh sách text detection.
- [S4] Azure Document Intelligence Read/Layout language support: https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/language-support/ocr?view=doc-intel-4.0.0 — kiểm tra ngôn ngữ theo model/feature, không suy diễn sang prebuilt invoice.
- [S5] Foundry SDK/endpoints: https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview — chọn đúng SDK và endpoint theo tài nguyên/deployment.
- [S6] LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts — checkpoint, thread và node replay cần idempotent side effects.
- [S7] Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk — pin phiên bản, dùng đúng server/client/transport API.

Ngày kiểm tra tài liệu: 22/10/2026. Không có số liệu benchmark đã đạt hoặc dịch vụ cloud đã được tạo bởi bản plan này.
