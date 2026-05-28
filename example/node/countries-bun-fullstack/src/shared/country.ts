export const REGIONS = ["Africa", "Americas", "Asia", "Europe", "Oceania"] as const;

export type Region = (typeof REGIONS)[number];

export type Country = {
  code: string;
  name: string;
  capital: string;
  region: Region;
  population: number;
  area: number;
};

export type CountryFilters = {
  region?: Region | "";
};

export type CountryListItem = Country & {
  formattedPopulation: string;
  formattedArea: string;
};

export type CountryStats = {
  count: number;
  totalPopulation: number;
  averagePopulation: number;
};
