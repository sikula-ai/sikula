import { COUNTRIES } from "../src/data/countries";
import { REGIONS } from "../src/shared/country";

const codes = new Set<string>();

for (const country of COUNTRIES) {
  if (!/^[A-Z]{3}$/.test(country.code)) {
    throw new Error(`Invalid country code: ${country.code}`);
  }
  if (codes.has(country.code)) {
    throw new Error(`Duplicate country code: ${country.code}`);
  }
  if (!REGIONS.includes(country.region)) {
    throw new Error(`Invalid region for ${country.code}: ${country.region}`);
  }
  if (!country.name || !country.capital) {
    throw new Error(`Missing display fields for ${country.code}`);
  }
  codes.add(country.code);
}

console.log(`Fixture check passed for ${COUNTRIES.length} countries`);
