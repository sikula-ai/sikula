import { describe, expect, test } from "bun:test";

import { handleRequest } from "../src/server/http";
import type { CountryListItem } from "../src/shared/country";

async function json<T>(path: string): Promise<{ status: number; body: T }> {
  const response = await handleRequest(new Request(`http://localhost${path}`));
  return { status: response.status, body: (await response.json()) as T };
}

describe("server routes", () => {
  test("lists countries", async () => {
    const { status, body } = await json<CountryListItem[]>("/api/countries");
    expect(status).toBe(200);
    expect(body).toHaveLength(14);
    expect(body[0]).toHaveProperty("formattedPopulation");
  });

  test("filters countries by region", async () => {
    const { body } = await json<CountryListItem[]>("/api/countries?region=Europe");
    expect(body.map((country) => country.code)).toEqual(["CZE", "DEU", "FRA"]);
  });

  test("rejects invalid region filters", async () => {
    const { status, body } = await json<{ message: string }>("/api/countries?region=Atlantis");
    expect(status).toBe(400);
    expect(body.message).toContain("Invalid region");
  });

  test("returns one country by code", async () => {
    const { status, body } = await json<CountryListItem>("/api/countries/deu");
    expect(status).toBe(200);
    expect(body.name).toBe("Germany");
  });

  test("returns aggregate stats", async () => {
    const { status, body } = await json<{ count: number; totalPopulation: number }>("/api/stats");
    expect(status).toBe(200);
    expect(body.count).toBe(14);
    expect(body.totalPopulation).toBeGreaterThan(0);
  });

  test("returns 404 for missing country", async () => {
    const { status, body } = await json<{ message: string }>("/api/countries/XXX");
    expect(status).toBe(404);
    expect(body.message).toContain("not found");
  });

  test("returns 400 for malformed encoded country code", async () => {
    const { status, body } = await json<{ message: string }>("/api/countries/%E0%A4%A");
    expect(status).toBe(400);
    expect(body.message).toBe("Invalid country code.");
  });

  test("serves the browser shell", async () => {
    const response = await handleRequest(new Request("http://localhost/"));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/html");
    expect(await response.text()).toContain("Bun full-stack example");
  });
});
