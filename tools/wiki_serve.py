"""Serve the agency-os wiki over HTTP with [[wikilinks]] resolved.

    python -m tools.wiki_serve --port 8888 [--wiki ~/wiki/agency-os]

Markdown-only, zero dependencies: renders pages as lightly-styled HTML and
rewrites [[wikilinks]] to working anchors, so the vault is browsable without
Obsidian. For real editing, open the folder in Obsidian or VS Code.
"""

from __future__ import annotations

import html
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def resolve(link: str, pages: set[str]) -> str:
    """[[concepts/pipeline##Section]] or [[pipeline]] → concepts/pipeline.md."""
    target = link.split("#", 1)[0].strip().lower().replace(" ", "-")
    if target in pages:
        return f"/{target}"
    for p in pages:  # match by basename
        if p.rsplit("/", 1)[-1] == target:
            return f"/{p}"
    return ""


def render(md: str, pages: set[str]) -> str:
    body = html.escape(md)

    def _link(m: re.Match) -> str:
        target, label = m.group(1), m.group(2) or m.group(1)
        href = resolve(m.group(1), pages)
        if href:
            return f'<a href="{href}">{html.escape(label)}</a>'
        return f'<span class="broken">[[{html.escape(m.group(1))}]]</span>'

    body = LINK_RE.sub(_link, body)
    lines = []
    in_code = False
    for ln in body.split("\n"):
        if ln.strip().startswith("```"):
            in_code = not in_code
            lines.append("<pre>" if in_code else "</pre>")
            continue
        if in_code:
            lines.append(ln)
        elif ln.startswith("# "):
            lines.append(f"<h1>{ln[2:]}</h1>")
        elif ln.startswith("## "):
            lines.append(f"<h2>{ln[3:]}</h2>")
        elif ln.startswith("### "):
            lines.append(f"<h3>{ln[4:]}</h3>")
        elif ln.strip().startswith("|"):
            lines.append(f"<code>{ln}</code>")
        elif ln.startswith("- "):
            lines.append(f"<li>{ln[2:]}</li>")
        elif ln.strip():
            lines.append(f"<p>{ln}</p>")
        else:
            lines.append("")
    return "\n".join(lines)


PAGE = """<!doctype html><meta charset="utf-8"><title>{title} — agency-os wiki</title>
<style>
 body {{ font: 15px/1.55 -apple-system, sans-serif; max-width: 46em; margin: 2em auto; padding: 0 1em; color: #222; }}
 a {{ color: #06c; }} .broken {{ color: #999; }} h1 {{ border-bottom: 2px solid #ddd; padding-bottom: .2em; }}
 code, pre {{ background: #f4f4f4; padding: .1em .3em; border-radius: 3px; }}
 pre {{ padding: .8em; overflow-x: auto; }}
 nav {{ font-size: .85em; margin-bottom: 1.5em; color: #666; }}
</style>
<nav><a href="/">index</a> · <a href="/log">log</a> · <a href="/SCHEMA">schema</a></nav>
{body}"""


def main(argv: list[str]) -> None:
    wiki = Path("~/wiki/agency-os").expanduser()
    port = 8888
    args = iter(argv[1:])
    for a in args:
        if a == "--port":
            port = int(next(args))
        elif a == "--wiki":
            wiki = Path(next(args)).expanduser()

    pages: dict[str, Path] = {}
    for p in wiki.rglob("*.md"):
        pages[str(p.relative_to(wiki))[:-3]] = p
    pages.setdefault("index", wiki / "index.md")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            slug = self.path.lstrip("/").split("?", 1)[0] or "index"
            path = pages.get(slug)
            if not path or not path.exists():
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
                return
            body = render(path.read_text(), set(pages))
            out = PAGE.format(title=html.escape(slug), body=body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, format, *args):  # noqa: A002 — quiet
            pass

    print(f"Serving {wiki} at http://localhost:{port} ({len(pages)} pages)")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main(sys.argv)
