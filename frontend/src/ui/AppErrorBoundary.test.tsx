import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { AppErrorBoundary } from "./AppErrorBoundary";

afterEach(() => vi.restoreAllMocks());

describe("AppErrorBoundary", () => {
  it("shows a recovery screen without exposing render data when a view crashes", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    function BrokenView(): ReactNode {
      throw new Error("sensitive component details");
    }

    render(
      <AppErrorBoundary>
        <BrokenView />
      </AppErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Giao diện gặp lỗi");
    expect(screen.getByRole("button", { name: "Tải lại ứng dụng" })).toBeInTheDocument();
    expect(screen.queryByText("sensitive component details")).not.toBeInTheDocument();
  });
});
