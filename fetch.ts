#!/usr/bin/env bun
// Downloads a Wayback Machine snapshot's page and the assets it references.
// Usage: bun fetch.ts [snapshotUrl] [outDir]

const DEFAULT_URL = "https://web.archive.org/web/20200603030326/https://mope.io/";

const snapshot = process.argv[2] ?? DEFAULT_URL;
const outDir = process.argv[3] ?? "out";

const match = snapshot.match(/^https?:\/\/web\.archive\.org\/web\/(\d+)[a-z_]*\/(https?:\/\/.+)$/);
if (!match) {
  console.error("Expected a URL like https://web.archive.org/web/<timestamp>/<original url>");
  process.exit(1);
}
const [, timestamp, originalUrl] = match;

// The `id_` flag returns the original bytes without Wayback's injected toolbar/URL rewriting.
const rawUrl = (url: string) => `https://web.archive.org/web/${timestamp}id_/${url}`;

async function fetchRetry(url: string, tries = 4): Promise<Response> {
  for (let i = 0; ; i++) {
    try {
      const res = await fetch(url);
      if (res.ok || res.status === 404 || i >= tries - 1) return res;
    } catch (err) {
      if (i >= tries - 1) throw err;
    }
    await Bun.sleep(2 ** (i + 1) * 1000);
  }
}

function localPath(url: URL, isPage = false): string {
  let path = decodeURIComponent(url.pathname);
  if (path.endsWith("/")) path += "index.html";
  else if (isPage && !/\.[a-z0-9]+$/i.test(path)) path += "/index.html";
  path = path.replace(/\.\.+\//g, "").replace(/^\/+/, "");
  const host = url.host.replace(/[:]/g, "_");
  return `${outDir}/${host}/${path}`;
}

const seen = new Set<string>();

async function save(url: string, isPage = false): Promise<string | undefined> {
  if (seen.has(url)) return;
  seen.add(url);
  const res = await fetchRetry(rawUrl(url));
  if (!res.ok) {
    console.warn(`  ${res.status} ${url}`);
    return;
  }
  const file = localPath(new URL(url), isPage);
  await Bun.write(file, res);
  console.log(`  saved ${file}`);
  return file;
}

function extractAssets(html: string, base: string): string[] {
  const urls = new Set<string>();
  const attr = /<(?:script|img|source|video|audio)\b[^>]*?\b(?:src|data-src)\s*=\s*["']([^"']+)["']/gi;
  const link = /<link\b[^>]*?\bhref\s*=\s*["']([^"']+)["']/gi;
  const cssUrl = /url\(\s*["']?([^"')]+)["']?\s*\)/gi;
  for (const re of [attr, link, cssUrl]) {
    for (const m of html.matchAll(re)) {
      const ref = m[1].trim();
      if (!ref || ref.startsWith("data:") || ref.startsWith("#") || ref.startsWith("javascript:")) continue;
      try {
        const abs = new URL(ref, base);
        if (abs.protocol === "http:" || abs.protocol === "https:") urls.add(abs.href);
      } catch {}
    }
  }
  return [...urls];
}

console.log(`Fetching ${originalUrl} @ ${timestamp}`);
const pageFile = await save(originalUrl, true);
if (!pageFile) process.exit(1);

const html = await Bun.file(pageFile).text();
const assets = extractAssets(html, originalUrl);
console.log(`Found ${assets.length} assets`);

// Fetch with limited concurrency to stay polite to archive.org.
const CONCURRENCY = 4;
const queue = [...assets];
await Promise.all(
  Array.from({ length: CONCURRENCY }, async () => {
    while (queue.length) {
      const url = queue.shift()!;
      try {
        const file = await save(url);
        // Pull in assets referenced from stylesheets (fonts, images).
        if (file?.endsWith(".css")) {
          const css = await Bun.file(file).text();
          for (const dep of extractAssets(css, url)) queue.push(dep);
        }
      } catch (err) {
        console.warn(`  failed ${url}: ${err}`);
      }
    }
  }),
);

console.log("Done.");
