import { createCountriesViewModel } from "./viewModel";
import type { CountryListItem, Region } from "../shared/country";

const regionSelect = document.querySelector<HTMLSelectElement>("#region-filter");
const list = document.querySelector<HTMLUListElement>("#countries-list");
const summary = document.querySelector<HTMLParagraphElement>("#countries-summary");
const empty = document.querySelector<HTMLParagraphElement>("#countries-empty");

async function requestJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`Request failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

function renderRegions(regions: Region[]) {
  if (!regionSelect) {
    return;
  }
  for (const region of regions) {
    const option = document.createElement("option");
    option.value = region;
    option.textContent = region;
    regionSelect.append(option);
  }
}

function countryRow(country: CountryListItem): HTMLLIElement {
  const item = document.createElement("li");
  item.className = "country-row";

  const header = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = country.name;
  const subtitle = document.createElement("span");
  subtitle.textContent = `${country.capital} - ${country.region}`;
  header.append(name, subtitle);

  const facts = document.createElement("dl");
  facts.append(factRow("Population", country.formattedPopulation), factRow("Area", country.formattedArea));

  item.append(header, facts);
  return item;
}

function factRow(label: string, value: string): HTMLDivElement {
  const row = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  row.append(term, description);
  return row;
}

function renderCountries(countries: CountryListItem[], selectedRegion: Region | "") {
  if (!list || !summary || !empty) {
    return;
  }

  const viewModel = createCountriesViewModel(countries, selectedRegion);
  summary.textContent = viewModel.summary;
  list.replaceChildren(...viewModel.countries.map(countryRow));
  empty.hidden = viewModel.countries.length > 0;
  empty.textContent = viewModel.emptyMessage;
}

async function loadCountries() {
  const selectedRegion = (regionSelect?.value ?? "") as Region | "";
  const params = new URLSearchParams();
  if (selectedRegion) {
    params.set("region", selectedRegion);
  }

  const path = params.size > 0 ? `/api/countries?${params}` : "/api/countries";
  const countries = await requestJson<CountryListItem[]>(path);
  renderCountries(countries, selectedRegion);
}

async function start() {
  const regions = await requestJson<Region[]>("/api/regions");
  renderRegions(regions);
  regionSelect?.addEventListener("change", () => {
    void loadCountries();
  });
  await loadCountries();
}

void start().catch((err: unknown) => {
  if (summary) {
    summary.textContent = err instanceof Error ? err.message : "Failed to load countries.";
  }
});
