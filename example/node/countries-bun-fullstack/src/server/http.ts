import { findCountryByCode, isRegion, listCountries, listRegions, populationStats } from "../domain/countryService";
import type { CountryFilters } from "../shared/country";

const DIST_ROOT = new URL("../../dist/public/", import.meta.url);
const PUBLIC_ROOT = new URL("../../public/", import.meta.url);

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
    },
  });
}

function error(message: string, status: number): Response {
  return json({ message }, status);
}

function contentType(pathname: string): string {
  if (pathname.endsWith(".html")) {
    return "text/html; charset=utf-8";
  }
  if (pathname.endsWith(".css")) {
    return "text/css; charset=utf-8";
  }
  if (pathname.endsWith(".js")) {
    return "text/javascript; charset=utf-8";
  }
  return "application/octet-stream";
}

function safeRelativePath(pathname: string): string | undefined {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return undefined;
  }

  const relative = decoded === "/" ? "index.html" : decoded.replace(/^\/+/, "");
  if (!relative || relative.includes("\\") || relative.split("/").includes("..")) {
    return undefined;
  }
  return relative;
}

async function serveFile(root: URL, pathname: string): Promise<Response | undefined> {
  const relative = safeRelativePath(pathname);
  if (!relative) {
    return undefined;
  }

  const file = Bun.file(new URL(relative, root));
  if (!(await file.exists())) {
    return undefined;
  }

  return new Response(file, {
    headers: {
      "content-type": contentType(relative),
    },
  });
}

function regionFilterFromUrl(url: URL): CountryFilters | Response {
  const region = url.searchParams.get("region")?.trim() ?? "";
  if (!region) {
    return { region: "" };
  }
  if (!isRegion(region)) {
    return error(`Invalid region '${region}'. Allowed values: ${listRegions().join(", ")}.`, 400);
  }
  return { region };
}

export async function handleApiRequest(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const pathname = url.pathname;

  if (request.method !== "GET") {
    return error("Method not allowed", 405);
  }

  if (pathname === "/api/countries") {
    const filters = regionFilterFromUrl(url);
    if (filters instanceof Response) {
      return filters;
    }
    return json(listCountries(filters));
  }

  if (pathname.startsWith("/api/countries/")) {
    let code: string;
    try {
      code = decodeURIComponent(pathname.slice("/api/countries/".length));
    } catch {
      return error("Invalid country code.", 400);
    }
    const country = findCountryByCode(code);
    return country ? json(country) : error(`Country '${code}' not found.`, 404);
  }

  if (pathname === "/api/regions") {
    return json(listRegions());
  }

  if (pathname === "/api/stats") {
    return json(populationStats());
  }

  return error("API route not found", 404);
}

export async function handleRequest(request: Request): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname.startsWith("/api/")) {
    return handleApiRequest(request);
  }

  if (request.method !== "GET") {
    return error("Method not allowed", 405);
  }

  const asset = await serveFile(DIST_ROOT, url.pathname);
  if (asset) {
    return asset;
  }

  if (url.pathname === "/" || !url.pathname.startsWith("/assets/")) {
    const fallback = await serveFile(PUBLIC_ROOT, url.pathname === "/" ? "/" : "/index.html");
    if (fallback) {
      return fallback;
    }
  }

  return error("Not found", 404);
}
