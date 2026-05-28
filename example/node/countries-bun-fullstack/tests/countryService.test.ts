import { describe, expect, test } from "bun:test";

import {
  filterCountries,
  findCountryByCode,
  formatArea,
  formatPopulation,
  listCountries,
  listRegions,
  populationStats,
} from "../src/domain/countryService";
import { COUNTRIES } from "../src/data/countries";

describe("country service", () => {
  test("lists regions in canonical order", () => {
    expect(listRegions()).toEqual(["Africa", "Americas", "Asia", "Europe", "Oceania"]);
  });

  test("filters countries by region", () => {
    const europe = filterCountries(COUNTRIES, { region: "Europe" });
    expect(europe.map((country) => country.code)).toEqual(["CZE", "DEU", "FRA"]);
  });

  test("returns all countries when region is blank", () => {
    expect(filterCountries(COUNTRIES, { region: "" })).toHaveLength(COUNTRIES.length);
  });

  test("finds country by code case-insensitively", () => {
    expect(findCountryByCode("deu")?.name).toBe("Germany");
  });

  test("formats list items for API and UI display", () => {
    const germany = listCountries({ region: "Europe" }).find((country) => country.code === "DEU");
    expect(germany?.formattedPopulation).toBe("83,240,525");
    expect(germany?.formattedArea).toBe("357,114 km2");
  });

  test("formats numbers with US separators", () => {
    expect(formatPopulation(1380004385)).toBe("1,380,004,385");
    expect(formatArea(3287590)).toBe("3,287,590 km2");
  });

  test("computes population stats", () => {
    expect(populationStats([{ ...COUNTRIES[0], population: 10 }, { ...COUNTRIES[1], population: 20 }])).toEqual({
      count: 2,
      totalPopulation: 30,
      averagePopulation: 15,
    });
  });
});
