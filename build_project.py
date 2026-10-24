#!/usr/bin/env python3
"""Build a runnable local mope.io project from a snapshot fetched with fetch.ts.

Usage: python3 build_project.py [snapshot_dir] [project_dir]
       (defaults: out  mope-project)

Steps:
  1. copy the snapshot into project/public, flattening the main host to the
     web root and other hosts to /_external/<host>/
  2. strip Wayback Machine artifacts and rewrite absolute/archived URLs to
     local root-relative paths
  3. decode JS string escapes and beautify scripts (originals kept in src_orig/)
  4. analyse scripts: WebSocket endpoints, HTTP endpoints, base64 blobs,
     obfuscation markers -> analysis/report.{json,md}
  5. inject a shim that redirects the game's WebSocket to a local server
  6. emit server.py (static server + frame-logging WebSocket stub) and README

Only the Python standard library is required; `jsbeautifier` is used if installed.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import re
import shutil
import struct
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

MAIN_HOST = "mope.io"
WAYBACK_RE = re.compile(
    r"(?:https?:)?//web\.archive\.org/web/\d+[a-z_]*/(?=(?:https?:)?//|[\w.-]+\.[a-z]{2,}/)",
    re.I,
)
TEXT_EXT = {".html", ".htm", ".js", ".css", ".json", ".txt", ".svg", ".xml", ".map"}
WS_URL_RE = re.compile(r"""wss?://[^\s"'`\\)<>]+""", re.I)
HTTP_URL_RE = re.compile(r"""https?://[^\s"'`\\)<>]+""", re.I)
B64_RE = re.compile(r"""["']([A-Za-z0-9+/]{40,}={0,2})["']""")
HEX_ESC_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")
OBFUSCATION_MARKERS = {
    "obfuscator.io string array": re.compile(r"_0x[0-9a-f]{4,6}\b"),
    "eval()": re.compile(r"\beval\s*\("),
    "Function constructor": re.compile(r"\bnew\s+Function\s*\("),
    "packer (p,a,c,k,e,d)": re.compile(r"\}\(\s*'[^']*',\s*\d+,\s*\d+,\s*'[^']*'\.split\('\|'\)"),
    "atob()": re.compile(r"\batob\s*\("),
    "String.fromCharCode": re.compile(r"String\.fromCharCode"),
}


# ---------------------------------------------------------------- layout ----
def project_rel(host: str, path: str) -> str:
    path = unquote(path).lstrip("/") or "index.html"
    if path.endswith("/"):
        path += "index.html"
    return path if host == MAIN_HOST else f"_external/{host}/{path}"


def map_snapshot(snapshot: Path) -> dict[Path, str]:
    """snapshot file -> relative path inside public/"""
    mapping = {}
    for f in sorted(snapshot.rglob("*")):
        if not f.is_file():
            continue
        parts = f.relative_to(snapshot).parts
        host, rest = parts[0], "/".join(parts[1:])
        mapping[f] = project_rel(host.removeprefix("www."), rest)
    return mapping


def known_hosts(mapping: dict[Path, str], snapshot: Path) -> set[str]:
    return {p.relative_to(snapshot).parts[0].removeprefix("www.") for p in mapping}


# ------------------------------------------------------------- rewriting ----
def strip_wayback(text: str) -> str:
    text = re.sub(r"<!--\s*BEGIN WAYBACK TOOLBAR INSERT\s*-->.*?<!--\s*END WAYBACK TOOLBAR INSERT\s*-->", "", text, flags=re.S)
    text = re.sub(r"<script[^>]*>[^<]*(?:__wm|wombat)[^<]*</script>\s*", "", text, flags=re.S)
    text = re.sub(r'<script[^>]+src="[^"]*(?:/_static/|archive\.org/includes/)[^"]*"[^>]*></script>\s*', "", text)
    text = re.sub(r'<link[^>]+href="[^"]*/_static/[^"]*"[^>]*>\s*', "", text)
    text = re.sub(r"<!--\s*FILE ARCHIVED ON.*?-->", "", text, flags=re.S)
    return WAYBACK_RE.sub("", text)


def rewrite_urls(text: str, hosts: set[str]) -> str:
    def repl(m: re.Match) -> str:
        host = m.group(2).removeprefix("www.")
        if host not in hosts:
            return m.group(0)
        u = urlparse("//" + m.group(2) + (m.group(3) or "/"))
        return "/" + quote(project_rel(host, u.path)) + (f"?{u.query}" if u.query else "")

    return re.sub(r"(https?:)?//((?:www\.)?[\w.-]+\.[a-z]{2,})(/[^\s\"'`)<>]*)?", repl, text)


def decode_js_escapes(src: str) -> str:
    """Turn \\xNN / \\uNNNN escapes of printable ASCII into the characters.

    Characters that would break a string literal (quotes, backslash, newline)
    stay escaped, so the script remains valid JS.
    """
    def one(m: re.Match) -> str:
        code = int(m.group(1) or m.group(2), 16)
        if 0x20 <= code < 0x7F and chr(code) not in "\"'`\\":
            return chr(code)
        return m.group(0)

    return HEX_ESC_RE.sub(one, src)


def beautify_js(src: str) -> str:
    try:
        import jsbeautifier  # type: ignore
        return jsbeautifier.beautify(src)
    except ImportError:
        pass
    if src.count("\n") > max(20, len(src) // 400):
        return src  # already multi-line
    # fallback: naive line breaks outside of strings
    out, depth, quote, i = [], 0, None, 0
    while i < len(src):
        c = src[i]
        out.append(c)
        if quote:
            if c == "\\":
                i += 1
                out.append(src[i] if i < len(src) else "")
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
            out.append("\n" + "  " * depth)
        elif c == "}":
            depth = max(depth - 1, 0)
            out[-1:] = ["\n" + "  " * depth + "}"]
            if i + 1 < len(src) and src[i + 1] not in ",;)":
                out.append("\n" + "  " * depth)
        elif c == ";":
            out.append("\n" + "  " * depth)
        i += 1
    return "".join(out)


# -------------------------------------------------------------- analysis ----
def analyse_script(rel: str, src: str, report: dict) -> None:
    for m in WS_URL_RE.finditer(src):
        report["websockets"][m.group(0).rstrip(";,")].append(rel)
    for m in re.finditer(r"new\s+WebSocket\s*\(([^)]*)\)", src):
        report["websocket_calls"].append({"file": rel, "args": m.group(1).strip()[:200]})
    for m in HTTP_URL_RE.finditer(src):
        report["http_urls"][m.group(0).rstrip(";,")].append(rel)
    for m in re.finditer(r"\.(?:open)\s*\(\s*[\"'](GET|POST)[\"']\s*,\s*([^,)]+)", src):
        report["xhr"].append({"file": rel, "method": m.group(1), "url": m.group(2).strip()[:200]})
    for m in re.finditer(r"\bfetch\s*\(\s*([^,)]+)", src):
        report["xhr"].append({"file": rel, "method": "fetch", "url": m.group(1).strip()[:200]})
    for m in B64_RE.finditer(src):
        try:
            raw = base64.b64decode(m.group(1), validate=True)
        except (binascii.Error, ValueError):
            continue
        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in raw) / max(len(raw), 1)
        report["base64"].append({
            "file": rel, "length": len(raw),
            "kind": "text" if printable > 0.9 else "binary",
            "preview": raw[:80].decode("latin-1") if printable > 0.9 else raw[:16].hex(),
        })
    for name, rx in OBFUSCATION_MARKERS.items():
        n = len(rx.findall(src))
        if n:
            report["obfuscation"].append({"file": rel, "marker": name, "count": n})
    for m in re.finditer(r"""["'](/?(?:[\w.-]+/)*[\w.-]+\.(?:png|jpg|jpeg|gif|svg|mp3|ogg|wav|webp|json|woff2?|ttf|mp4))["']""", src):
        report["asset_refs"][m.group(1)].append(rel)


def write_report(report: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    plain = {k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in report.items()}
    (out / "report.json").write_text(json.dumps(plain, indent=2))
    md = ["# mope.io snapshot analysis\n"]
    md.append("## WebSocket endpoints\n" + ("\n".join(f"- `{u}` ({', '.join(sorted(set(f)))})" for u, f in report["websockets"].items()) or "_none found in literal strings_"))
    md.append("\n## `new WebSocket(...)` call sites\n" + ("\n".join(f"- `{c['args']}` in {c['file']}" for c in report["websocket_calls"]) or "_none_"))
    md.append("\n## HTTP requests (XHR/fetch)\n" + ("\n".join(f"- {x['method']} `{x['url']}` in {x['file']}" for x in report["xhr"]) or "_none_"))
    md.append("\n## External URLs\n" + ("\n".join(f"- {u}" for u in sorted(report["http_urls"])) or "_none_"))
    md.append("\n## Obfuscation indicators\n" + ("\n".join(f"- {o['file']}: {o['marker']} x{o['count']}" for o in report["obfuscation"]) or "_none_"))
    md.append("\n## Base64 blobs\n" + ("\n".join(f"- {b['file']}: {b['length']} bytes {b['kind']} `{b['preview']!r}`" for b in report["base64"]) or "_none_"))
    md.append("\n## Referenced assets\n" + ("\n".join(f"- {a}" for a in sorted(report["asset_refs"])) or "_none_"))
    (out / "report.md").write_text("\n".join(md) + "\n")


# ----------------------------------------------------------- generated ------
SHIM = """/* Injected by build_project.py: sends the game's WebSocket traffic to the local server. */
(function () {
  var Native = window.WebSocket;
  var LOCAL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.hostname + ':' + (window.MOPE_WS_PORT || 8081);
  window.MOPE_WS_LOG = [];
  window.WebSocket = function (url, protocols) {
    var target = /^wss?:\\/\\//i.test(url) ? LOCAL + new URL(url).pathname + new URL(url).search : url;
    console.log('[mope shim] WebSocket', url, '->', target);
    window.MOPE_WS_LOG.push(url);
    return protocols ? new Native(target, protocols) : new Native(target);
  };
  window.WebSocket.prototype = Native.prototype;
  ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) { window.WebSocket[k] = Native[k]; });
})();
"""

SERVER_PY = '''#!/usr/bin/env python3
"""Static file server (port 8080) + frame-logging WebSocket stub (port 8081).

The original game server is not recoverable from the client snapshot, so the
stub only accepts connections and logs every frame it receives. Use the logs
to reverse engineer the protocol, then implement replies in `handle_frame`.
"""
import asyncio, base64, hashlib, http.server, struct, threading
from pathlib import Path

ROOT = Path(__file__).parent / "public"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def handle_frame(opcode: int, payload: bytes) -> bytes | None:
    """Return a binary payload to send back, or None."""
    return None


async def ws_client(reader, writer):
    head = (await reader.readuntil(b"\\r\\n\\r\\n")).decode("latin-1")
    key = next((l.split(":", 1)[1].strip() for l in head.split("\\r\\n") if l.lower().startswith("sec-websocket-key")), None)
    if not key:
        writer.close(); return
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(f"HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Accept: {accept}\\r\\n\\r\\n".encode())
    await writer.drain()
    print("[ws] connect", head.split("\\r\\n")[0])
    try:
        while True:
            b1, b2 = await reader.readexactly(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126: n = struct.unpack(">H", await reader.readexactly(2))[0]
            elif n == 127: n = struct.unpack(">Q", await reader.readexactly(8))[0]
            mask = await reader.readexactly(4) if b2 & 0x80 else b"\\0\\0\\0\\0"
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(n)))
            if opcode == 8: break
            print(f"[ws] opcode={opcode} len={n} data={data[:64].hex()}")
            reply = handle_frame(opcode, data)
            if reply is not None:
                writer.write(bytes([0x82, len(reply)]) + reply if len(reply) < 126 else bytes([0x82, 126]) + struct.pack(">H", len(reply)) + reply)
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        print("[ws] disconnect"); writer.close()


def serve_http():
    h = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(ROOT), **k)
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), h).serve_forever()


async def main():
    threading.Thread(target=serve_http, daemon=True).start()
    srv = await asyncio.start_server(ws_client, "0.0.0.0", 8081)
    print("http://localhost:8080  (ws stub on :8081)")
    async with srv:
        await srv.serve_forever()


asyncio.run(main())
'''

README = """# mope.io local project

Generated by `build_project.py` from a Wayback Machine snapshot.

- `public/` - the site, with Wayback artifacts removed and URLs rewritten to local paths
  (third-party hosts live in `public/_external/<host>/`)
- `src_orig/` - untouched copies of every text file before rewriting
- `analysis/report.md|json` - WebSocket endpoints, HTTP calls, base64 blobs, obfuscation markers
- `server.py` - `python3 server.py` serves `public/` on :8080 and a WebSocket logging stub on :8081
- `public/mope-shim.js` - redirects the game's `new WebSocket(...)` to the local stub

The original game servers are not part of the snapshot, so the game will load but
cannot play until the protocol seen in the stub's logs is implemented in `handle_frame`.
"""


# ------------------------------------------------------------------ main ----
def main() -> int:
    snapshot = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    project = Path(sys.argv[2] if len(sys.argv) > 2 else "mope-project")
    if not snapshot.is_dir():
        print(f"snapshot dir not found: {snapshot} (run `bun fetch.ts` first)")
        return 1

    mapping = map_snapshot(snapshot)
    if not mapping:
        print("snapshot is empty")
        return 1
    hosts = known_hosts(mapping, snapshot)
    public, orig = project / "public", project / "src_orig"
    if project.exists():
        shutil.rmtree(project)
    report = {
        "websockets": defaultdict(list), "websocket_calls": [], "http_urls": defaultdict(list),
        "xhr": [], "base64": [], "obfuscation": [], "asset_refs": defaultdict(list),
        "files": {},
    }

    for src, rel in mapping.items():
        dst = public / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        report["files"][rel] = src.relative_to(snapshot).as_posix()
        if src.suffix.lower() not in TEXT_EXT:
            shutil.copy2(src, dst)
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        (orig / rel).parent.mkdir(parents=True, exist_ok=True)
        (orig / rel).write_text(text)
        text = rewrite_urls(strip_wayback(text), hosts)
        if src.suffix.lower() == ".js":
            analyse_script(rel, text, report)
            text = beautify_js(decode_js_escapes(text))
        elif src.suffix.lower() in {".html", ".htm"}:
            for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, flags=re.S | re.I):
                analyse_script(rel + " (inline)", m.group(1), report)
        dst.write_text(text)

    index = public / "index.html"
    if index.exists():
        html = index.read_text()
        tag = '<script src="/mope-shim.js"></script>'
        html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + tag, html, count=1, flags=re.I) if re.search(r"<head", html, re.I) else tag + html
        index.write_text(html)
    (public / "mope-shim.js").write_text(SHIM)

    write_report(report, project / "analysis")
    (project / "server.py").write_text(SERVER_PY)
    (project / "README.md").write_text(README)
    print(f"built {project}/ from {len(mapping)} files; "
          f"{len(report['websockets'])} ws endpoints, {len(report['base64'])} base64 blobs")
    print(f"next: python3 {project}/server.py  ->  http://localhost:8080")
    return 0


if __name__ == "__main__":
    sys.exit(main())
