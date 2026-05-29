import { describe, expect, test } from "bun:test";

async function readStyles(): Promise<string> {
  return Bun.file(new URL("../src/client/styles.css", import.meta.url)).text();
}

describe("client styles", () => {
  test("keeps country cards as a responsive, stable grid", async () => {
    const styles = await readStyles();

    expect(styles).toContain(".countries-list {");
    expect(styles).toContain("grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));");
    expect(styles).toContain(".countries-list > li {");
    expect(styles).toContain("min-width: 0;");
    expect(styles).toContain(".country-row {");
    expect(styles).toContain("align-content: space-between;");
    expect(styles).toContain("width: 100%;");
    expect(styles).toContain("min-height: 144px;");
  });

  test("keeps card facts in separate label and value columns", async () => {
    const styles = await readStyles();

    expect(styles).toContain(".country-row dl {");
    expect(styles).toContain(".country-row dl div {");
    expect(styles).toContain("grid-template-columns: max-content minmax(0, 1fr);");
    expect(styles).toContain(".country-row dd {");
    expect(styles).toContain("text-align: right;");
    expect(styles).toContain("overflow-wrap: anywhere;");
  });

  test("supports accessible interactive country card wrappers", async () => {
    const styles = await readStyles();

    expect(styles).toContain(".country-row:is(button, a) {");
    expect(styles).toContain("appearance: none;");
    expect(styles).toContain("cursor: pointer;");
    expect(styles).toContain(".country-row:is(button, a):hover {");
    expect(styles).toContain(".country-row:is(button, a):focus-visible {");
  });
});
