import type { Country } from "../../domain/country";
import { formatPopulation } from "../../domain/country";

type CountryListProps = {
  countries: Country[];
};

export function CountryList({ countries }: CountryListProps) {
  if (countries.length === 0) {
    return <p className="empty-state">No countries match the selected filters.</p>;
  }

  return (
    <ul className="country-list" aria-label="Countries">
      {countries.map((country) => (
        <li className="country-row" key={country.code}>
          <span className="flag" aria-hidden="true">
            {country.flagEmoji}
          </span>
          <div className="country-main">
            <span className="country-name">{country.name}</span>
            <span className="country-meta">
              {country.capital} · {country.region}
            </span>
          </div>
          <span className="population">{formatPopulation(country.population)}</span>
        </li>
      ))}
    </ul>
  );
}

