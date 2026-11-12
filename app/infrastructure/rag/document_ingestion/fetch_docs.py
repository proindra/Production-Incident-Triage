"""Download selected official PostgreSQL documentation pages for local indexing."""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

_PAGES = {
    "warm-standby": "warm-standby.html",
    "monitoring-stats": "monitoring-stats.html",
    "runtime-config-wal": "runtime-config-wal.html",
}
_BASE_URL = "https://www.postgresql.org/docs/{version}/{page}"
_MAX_PAGE_BYTES = 8_000_000


class _DocumentationParser(HTMLParser):
    _BLOCK_TAGS = {
        "p", "li", "pre", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_content = False
        self._content_depth = 0
        self._skip_depth = 0
        self._parts: list[str] = []
        self._block: list[str] = []
        self._block_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if not self._inside_content and (
            attributes.get("id") == "docContent"
            or "docContent" in attributes.get("class", "").split()
        ):
            self._inside_content = True
            self._content_depth = 1
            return
        if not self._inside_content:
            return
        if tag in {"script", "style", "nav"}:
            self._skip_depth += 1
        if tag not in {"br", "img", "meta", "link", "hr", "input"}:
            self._content_depth += 1
        if self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._flush_block()
            self._block_tag = tag

    def handle_endtag(self, tag: str) -> None:
        if not self._inside_content:
            return
        if self._skip_depth:
            if tag in {"script", "style", "nav"}:
                self._skip_depth -= 1
            return
        if tag == self._block_tag:
            self._flush_block()
            self._block_tag = None
        if tag not in {"br", "img", "meta", "link", "hr", "input"}:
            self._content_depth -= 1
        if self._content_depth <= 0:
            self._flush_block()
            self._inside_content = False

    def handle_data(self, data: str) -> None:
        if self._inside_content and self._skip_depth == 0:
            text = re.sub(r"\s+", " ", data).strip()
            if text:
                self._block.append(text)

    def _flush_block(self) -> None:
        text = " ".join(self._block).strip()
        if text:
            if self._block_tag and self._block_tag.startswith("h"):
                self._parts.append(f"{'#' * min(int(self._block_tag[1]), 6)} {text}")
            else:
                self._parts.append(text)
        self._block.clear()

    def markdown(self) -> str:
        self._flush_block()
        return "\n\n".join(self._parts).strip()


def fetch_postgres_docs(
    version: str,
    destination: str | Path,
    client: httpx.Client | None = None,
) -> list[Path]:
    if not re.fullmatch(r"\d+", version):
        raise ValueError("PostgreSQL version must be a major-version number.")
    root = Path(destination).resolve() / version
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    owned_client = client is None
    http = client or httpx.Client(timeout=20.0, follow_redirects=True)
    try:
        for slug, page in _PAGES.items():
            url = _BASE_URL.format(version=version, page=page)
            if urlparse(url).hostname != "www.postgresql.org":
                raise ValueError("Documentation source host is not permitted.")
            response = http.get(url)
            response.raise_for_status()
            if len(response.content) > _MAX_PAGE_BYTES:
                raise ValueError(f"PostgreSQL documentation page is too large: {page}")
            parser = _DocumentationParser()
            parser.feed(response.text)
            content = parser.markdown()
            if not content:
                raise ValueError(f"No document text found in PostgreSQL page: {page}")
            target = root / f"{slug}.md"
            target.write_text(
                f"# PostgreSQL {version}: {slug.replace('-', ' ').title()}\n\n"
                f"Source: {url}\n\n{content}\n",
                encoding="utf-8",
            )
            paths.append(target)
    finally:
        if owned_client:
            http.close()
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="17")
    parser.add_argument("--destination", default="data/postgresql")
    arguments = parser.parse_args()
    paths = fetch_postgres_docs(arguments.version, arguments.destination)
    print("\n".join(str(path) for path in paths))


if __name__ == "__main__":
    main()
