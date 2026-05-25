import type { Region } from "../../domain/country";

type RegionFilterProps = {
  regions: Region[];
  value: Region | "";
  onChange: (region: Region | "") => void;
};

export function RegionFilter({ regions, value, onChange }: RegionFilterProps) {
  return (
    <label className="select-field">
      <span>Region</span>
      <select value={value} onChange={(event) => onChange(event.target.value as Region | "")}>
        <option value="">All regions</option>
        {regions.map((region) => (
          <option key={region} value={region}>
            {region}
          </option>
        ))}
      </select>
    </label>
  );
}

