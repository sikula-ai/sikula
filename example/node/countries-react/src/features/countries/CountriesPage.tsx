import { useMemo, useState } from "react";

import { COUNTRIES } from "../../data/countries";
import type { Country, Region } from "../../domain/country";
import { filterCountries, listRegions } from "../../domain/countryFilters";
import { CountryList } from "./CountryList";
import { RegionFilter } from "./RegionFilter";

type CountriesPageProps = {
  countries?: Country[];
};

export function CountriesPage({ countries = COUNTRIES }: CountriesPageProps) {
  const [selectedRegion, setSelectedRegion] = useState<Region | "">("");

  const regions = useMemo(() => listRegions(countries), [countries]);
  const visibleCountries = useMemo(
    () => filterCountries(countries, { region: selectedRegion }),
    [countries, selectedRegion]
  );

  return (
    <main className="app-shell">
      <section className="hero" aria-labelledby="countries-title">
        <div>
          <p className="eyebrow">Countries</p>
          <h1 id="countries-title">Explore country data</h1>
        </div>
        <p className="hero-copy">
          Browse a small local dataset with capital cities, regions, population, and area.
        </p>
      </section>

      <section className="toolbar" aria-label="Country filters">
        <RegionFilter regions={regions} value={selectedRegion} onChange={setSelectedRegion} />
        <p className="result-count" aria-live="polite">
          {visibleCountries.length} {visibleCountries.length === 1 ? "country" : "countries"}
        </p>
      </section>

      <CountryList countries={visibleCountries} />
    </main>
  );
}

