import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FixtureBadge } from "./FixtureBadge";

describe("FixtureBadge", () => {
  it("labels the synthetic fixture mode plainly", () => {
    render(<FixtureBadge />);
    expect(screen.getByRole("status")).toHaveTextContent("Fixture demo — dữ liệu trích xuất có sẵn");
  });
});

