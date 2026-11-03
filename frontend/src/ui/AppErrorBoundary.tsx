import React, { type ErrorInfo, type ReactNode } from "react";

type AppErrorBoundaryProps = { children: ReactNode };
type AppErrorBoundaryState = { failed: boolean };

export class AppErrorBoundary extends React.Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    // Log only the error class; invoice data and component props must stay private.
    console.error("IDP_UI_RENDER_ERROR", error.name);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="recovery-shell" role="alert">
        <section className="login-card">
          <div className="brand-mark">ID</div>
          <p className="eyebrow">Không thể tiếp tục</p>
          <h1>Giao diện gặp lỗi</h1>
          <p>Không thể hiển thị hồ sơ này. Hãy tải lại ứng dụng. Nếu vừa tải chứng từ, hãy kiểm tra danh sách trước khi gửi lại.</p>
          <button className="primary" onClick={() => window.location.reload()}>Tải lại ứng dụng</button>
        </section>
      </main>
    );
  }
}
