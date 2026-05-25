import { describe, expect, it } from "vitest";

import { COUNTRIES } from "../src/data/countries";
import { filterCountries, listRegions } from "../src/domain/countryFilters";

describe("country filters", () => {
  it("lists regions alphabetically", () => {
    expect(listRegions(COUNTRIES)).toEqual(["Africa", "Americas", "Asia", "Europe", "Oceania"]);
  });

  it("returns all countries when no region is selected", () => {
    expect(filterCountries(COUNTRIES, { region: "" })).toHaveLength(COUNTRIES.length);
  });

  it("filters countries by region", () => {
    const europe = filterCountries(COUNTRIES, { region: "Europe" });

    expect(europe.map((country) => country.code)).toEqual(["CZE", "DEU", "FRA"]);
  });
});

