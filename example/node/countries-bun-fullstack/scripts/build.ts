import { copyFile, mkdir, rm } from "node:fs/promises";

const distRoot = new URL("../dist/public/", import.meta.url);
const assetsRoot = new URL("assets/", distRoot);
const clientEntry = new URL("../src/client/main.ts", import.meta.url).pathname;
const indexHtml = new URL("../public/index.html", import.meta.url);
const stylesCss = new URL("../src/client/styles.css", import.meta.url);

await rm(distRoot, { recursive: true, force: true });
await mkdir(assetsRoot, { recursive: true });

const result = await Bun.build({
  entrypoints: [clientEntry],
  outdir: assetsRoot.pathname,
  target: "browser",
  naming: "[name].[ext]",
});

if (!result.success) {
  for (const log of result.logs) {
    console.error(log);
  }
  process.exit(1);
}

await copyFile(indexHtml, new URL("index.html", distRoot));
await copyFile(stylesCss, new URL("styles.css", distRoot));

console.log("Built browser assets in dist/public");
