import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { CountriesPage } from "../src/features/countries/CountriesPage";

describe("CountriesPage", () => {
  it("renders the country list", () => {
    render(<CountriesPage />);

    expect(screen.getByRole("heading", { name: "Explore country data" })).toBeInTheDocument();
    expect(screen.getByText("Germany")).toBeInTheDocument();
    expect(screen.getByText("Japan")).toBeInTheDocument();
  });

  it("filters countries by region", async () => {
    const user = userEvent.setup();
    render(<CountriesPage />);

    await user.selectOptions(screen.getByLabelText("Region"), "Europe");

    expect(screen.getByText("Germany")).toBeInTheDocument();
    expect(screen.getByText("France")).toBeInTheDocument();
    expect(screen.queryByText("Japan")).not.toBeInTheDocument();
    expect(screen.getByText("3 countries")).toBeInTheDocument();
  });
});

