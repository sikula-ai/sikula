import type { Country, Region } from "./country";

export type CountryFilters = {
  region?: Region | "";
};

export function listRegions(countries: Country[]): Region[] {
  return [...new Set(countries.map((country) => country.region))].sort();
}

export function filterCountries(countries: Country[], filters: CountryFilters): Country[] {
  const selectedRegion = filters.region?.trim();
  if (!selectedRegion) {
    return countries;
  }

  return countries.filter((country) => country.region === selectedRegion);
}

