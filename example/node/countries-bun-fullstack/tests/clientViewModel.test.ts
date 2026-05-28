import { describe, expect, test } from "bun:test";

import { createCountriesViewModel } from "../src/client/viewModel";
import { listCountries } from "../src/domain/countryService";

describe("client view model", () => {
  test("summarizes all countries", () => {
    const viewModel = createCountriesViewModel(listCountries(), "");
    expect(viewModel.summary).toBe("14 countries in all regions");
    expect(viewModel.emptyMessage).toBe("No countries found in all regions.");
  });

  test("summarizes selected region", () => {
    const viewModel = createCountriesViewModel(listCountries({ region: "Europe" }), "Europe");
    expect(viewModel.summary).toBe("3 countries in Europe");
    expect(viewModel.countries.map((country) => country.code)).toEqual(["CZE", "DEU", "FRA"]);
  });

  test("uses singular noun for one result", () => {
    const viewModel = createCountriesViewModel([listCountries({ region: "Europe" })[0]], "Europe");
    expect(viewModel.summary).toBe("1 country in Europe");
  });
});
