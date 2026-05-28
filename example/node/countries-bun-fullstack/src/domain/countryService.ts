import { COUNTRIES } from "../data/countries";
import {
  REGIONS,
  type Country,
  type CountryFilters,
  type CountryListItem,
  type CountryStats,
  type Region,
} from "../shared/country";

const numberFormatter = new Intl.NumberFormat("en-US");

export function isRegion(value: string): value is Region {
  return (REGIONS as readonly string[]).includes(value);
}

export function formatPopulation(population: number): string {
  return numberFormatter.format(population);
}

export function formatArea(area: number): string {
  return `${numberFormatter.format(area)} km2`;
}

export function listRegions(countries: Country[] = COUNTRIES): Region[] {
  return REGIONS.filter((region) => countries.some((country) => country.region === region));
}

export function filterCountries(countries: Country[], filters: CountryFilters = {}): Country[] {
  const selectedRegion = filters.region?.trim();
  if (!selectedRegion) {
    return countries;
  }

  return countries.filter((country) => country.region === selectedRegion);
}

export function toCountryListItem(country: Country): CountryListItem {
  return {
    ...country,
    formattedPopulation: formatPopulation(country.population),
    formattedArea: formatArea(country.area),
  };
}

export function listCountries(filters: CountryFilters = {}): CountryListItem[] {
  return filterCountries(COUNTRIES, filters).map(toCountryListItem);
}

export function findCountryByCode(code: string): CountryListItem | undefined {
  const normalized = code.trim().toUpperCase();
  const country = COUNTRIES.find((item) => item.code === normalized);
  return country ? toCountryListItem(country) : undefined;
}

export function populationStats(countries: Country[] = COUNTRIES): CountryStats {
  const totalPopulation = countries.reduce((sum, country) => sum + country.population, 0);
  return {
    count: countries.length,
    totalPopulation,
    averagePopulation: countries.length === 0 ? 0 : Math.floor(totalPopulation / countries.length),
  };
}
