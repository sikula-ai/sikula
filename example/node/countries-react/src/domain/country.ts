export type Region = "Africa" | "Americas" | "Asia" | "Europe" | "Oceania";

export type Country = {
  code: string;
  name: string;
  capital: string;
  region: Region;
  population: number;
  area: number;
  flagEmoji: string;
};

export function formatPopulation(population: number): string {
  return new Intl.NumberFormat("en-US").format(population);
}

