import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");
const htmlPath = join(dist, "index.html");

let html = await readFile(htmlPath, "utf8");

const stylesheetMatches = [...html.matchAll(/<link rel="stylesheet" crossorigin href="([^"]+)">/g)];
for (const match of stylesheetMatches) {
  const href = match[1];
  const css = await readFile(join(dist, href.replace(/^\//, "")), "utf8");
  html = html.replace(match[0], () => `<style>\n${css}\n</style>`);
}

const scriptMatches = [
  ...html.matchAll(/<script type="module" crossorigin src="([^"]+)"><\/script>/g)
];
for (const match of scriptMatches) {
  const src = match[1];
  const js = await readFile(join(dist, src.replace(/^\//, "")), "utf8");
  html = html.replace(match[0], () => `<script type="module">\n${js}\n</script>`);
}

await writeFile(htmlPath, html);
