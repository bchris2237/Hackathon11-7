import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";

afterEach(() => vi.unstubAllGlobals());
const renderApp = () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  return render(<App />);
};

describe("business entry screen", () => {
  it("starts invalid with Continue disabled, then loads the sample and enables it", async () => {
    const user = userEvent.setup();
    renderApp();
    const next = screen.getByRole("button", { name: "Continue to confirmation" });
    expect(next).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("DevCo");
    expect(screen.getByLabelText("Business 2 ID")).toHaveValue("designco");
    expect(screen.getByLabelText("Business 3 needs (service)")).toHaveValue("coding");
    expect(screen.getByLabelText("Business 1 capacity (hours)")).toHaveValue("10");
    expect(screen.getByLabelText("Business 1 design estimate (hours)")).toHaveValue("8");
    expect(screen.getByTestId("validity")).toHaveTextContent("Input is valid");
    expect(next).toBeEnabled();
  });

  it("shows field errors for a sub-minute value and disables Continue again", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    const capacity = screen.getByLabelText("Business 2 capacity (hours)");
    await user.clear(capacity);
    await user.type(capacity, "0.001");
    expect(capacity).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/sub-minute precision/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to confirmation" })).toBeDisabled();
  });

  it("rejects a blank ID and reset returns to blank", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.clear(screen.getByLabelText("Business 1 ID"));
    expect(screen.getByText("ID is required.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset to blank" }));
    expect(screen.getByLabelText("Business 1 name")).toHaveValue("");
  });

  it("editing a field keeps the result slot cleared", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: "Load fictional sample" }));
    await user.type(screen.getByLabelText("Business 1 name"), "X");
    expect(screen.getByTestId("result-slot")).toHaveTextContent(/No result/);
  });
});
