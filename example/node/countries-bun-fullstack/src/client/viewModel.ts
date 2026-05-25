import type { CountryListItem, Region } from "../shared/country";

export type CountriesViewModel = {
  title: string;
  summary: string;
  emptyMessage: string;
  countries: CountryListItem[];
};

export function createCountriesViewModel(
  countries: CountryListItem[],
  selectedRegion: Region | "",
): CountriesViewModel {
  const scope = selectedRegion || "all regions";
  const count = countries.length;
  return {
    title: "Countries",
    summary: `${count} ${count === 1 ? "country" : "countries"} in ${scope}`,
    emptyMessage: `No countries found in ${scope}.`,
    countries,
  };
}
