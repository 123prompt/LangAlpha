#!/usr/bin/env python3
"""Web scraping MCP server — scrapling as a library, era-proof.

Replaces the third-party ``scrapling mcp`` entrypoint, which imports the
mcp 1.x-only ``mcp.server.fastmcp`` and dies in any 2.x environment. Importing
scrapling's fetchers directly decouples the server from the SDK era the
scrapling CLI was built against; ``_bootstrap`` handles our own era split.

Tools: scrape_page, scrape_pages.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, List, Optional

try:
    from _bootstrap import MCPServer  # script launch: mcp_servers/ is sys.path[0]
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._bootstrap import MCPServer

from mcp_servers._schemas import output_model

mcp = MCPServer("ScrapeMCP")

_MODES = ("fast", "browser", "stealth")
_EXTRACTIONS = ("markdown", "html", "text")
_MAX_TIMEOUT_S = 120.0
_DEFAULT_TIMEOUT_S = 30.0
_MAX_BULK_URLS = 10
# Browser sessions are ~400MB each; the third-party server serialized too.
_BROWSER_CONCURRENCY = 2
_FAST_CONCURRENCY = 8
_MAX_CONTENT_CHARS = 400_000
_MAX_DETAIL_CHARS = 300

# Process-wide, not per-call. A semaphore built inside the handler bounds only
# its own batch, so N concurrent tool calls could still open 2N browsers and
# blow the memory budget the limit exists to protect. Safe to build at import:
# asyncio binds the loop on first contended acquire, and the stdio server runs
# a single loop for the life of the process.
_BROWSER_SEM = asyncio.Semaphore(_BROWSER_CONCURRENCY)
_FAST_SEM = asyncio.Semaphore(_FAST_CONCURRENCY)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_ERROR_PROPS = {
    "error": {
        "type": "string",
        "description": "Machine-readable error code (error responses only).",
    },
    "detail": {
        "type": "string",
        "description": "Human-readable cause (error responses only).",
    },
}
_ERROR_ARM = {"required": ["error", "detail"]}

_PAGE_PROPS = {
    "url": {"type": "string"},
    "status": {"type": "integer", "description": "HTTP status of the final response."},
    "title": {"type": "string"},
    "content": {"type": "string", "description": "Extracted content, truncated to 400k chars."},
    "extraction": {"type": "string", "enum": list(_EXTRACTIONS)},
    "mode": {"type": "string", "enum": list(_MODES)},
    **_ERROR_PROPS,
}

_OUT_PAGE = output_model(
    "ScrapePageOut",
    {
        "type": "object",
        "additionalProperties": True,
        "properties": _PAGE_PROPS,
        "anyOf": [{"required": ["url", "status", "content"]}, _ERROR_ARM],
    },
)

_OUT_PAGES = output_model(
    "ScrapePagesOut",
    {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": _PAGE_PROPS,
                },
                "description": "One entry per input URL, in input order.",
            },
            "count": {"type": "integer"},
            **_ERROR_PROPS,
        },
        "anyOf": [{"required": ["results", "count"]}, _ERROR_ARM],
    },
)


def _clean_detail(detail: str) -> str:
    """Collapse to one capped line — upstream exception text can carry a whole
    response body or driver dump, and the agent only needs the cause."""
    collapsed = re.sub(r"\s+", " ", detail).strip()
    if len(collapsed) <= _MAX_DETAIL_CHARS:
        return collapsed
    return collapsed[:_MAX_DETAIL_CHARS] + "..."


def _error(code: str, detail: str, **echo: Any) -> dict:
    return {"error": code, "detail": _clean_detail(detail), **echo}


def _as_entry(url: str, result: Any) -> dict:
    """Per-URL row for one gather slot.

    ``return_exceptions=True`` hands back the exception object rather than
    failing the batch, so anything that still escaped _scrape_one owes the
    caller a row — not an MCP isError over the other nine URLs. Cancellation
    is not ours to convert into a result.
    """
    if isinstance(result, BaseException):
        if not isinstance(result, Exception):
            raise result
        return _error("scrape_failed", f"{type(result).__name__}: {result}", url=url)
    return result


def _extract_title(html: str) -> str:
    match = _TITLE_RE.search(html[:50_000])
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def _to_markdown(html: str) -> str:
    import html_to_markdown
    import trafilatura

    extracted = trafilatura.extract(
        html,
        favor_recall=True,
        output_format="markdown",
        include_links=True,
        include_images=True,
        include_formatting=True,
        include_tables=True,
        with_metadata=True,
    )
    if extracted and len(extracted) > 200:
        return extracted
    full = html_to_markdown.convert(
        html, html_to_markdown.ConversionOptions(extract_metadata=False)
    ).content
    return full if full.strip() else (extracted or "")


def _to_text(html: str) -> str:
    import trafilatura

    return trafilatura.extract(html, output_format="txt") or ""


async def _fetch_html(
    url: str, mode: str, timeout_s: float, solve_cloudflare: bool
) -> tuple[str, int]:
    if mode == "fast":
        from scrapling.fetchers import AsyncFetcher

        page = await AsyncFetcher.get(
            url, stealthy_headers=True, follow_redirects=True, timeout=timeout_s
        )
        return page.body.decode(page.encoding or "utf-8", errors="replace"), page.status

    if mode == "browser":
        from scrapling.engines._browsers._controllers import AsyncDynamicSession

        session = AsyncDynamicSession(
            headless=True, disable_resources=True, network_idle=True, timeout=timeout_s * 1000
        )
        fetch_kwargs: dict[str, Any] = {}
    else:  # stealth
        from scrapling.engines._browsers._stealth import AsyncStealthySession

        session = AsyncStealthySession(
            headless=True, network_idle=True, timeout=timeout_s * 1000
        )
        fetch_kwargs = {"solve_cloudflare": solve_cloudflare}

    try:
        await session.start()
        page = await session.fetch(url, **fetch_kwargs)
        return page.body.decode(page.encoding or "utf-8", errors="replace"), page.status
    finally:
        # Cancel-during-start leaves _is_alive False while the playwright
        # driver is up; close() would early-return and leak it (same forcing
        # the in-process crawler applies).
        if getattr(session, "playwright", None) is not None and not getattr(
            session, "_is_alive", True
        ):
            session._is_alive = True
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - teardown must never mask the fetch result
            pass


async def _scrape_one(
    url: str, mode: str, extraction: str, timeout_s: float, solve_cloudflare: bool
) -> dict:
    if not url.startswith(("http://", "https://")):
        return _error("invalid_url", f"URL must be http(s), got: {url[:200]}", url=url)
    # Gate held across the fetch only: the extraction below is thread work
    # holding no browser, so releasing early lets the next URL start sooner.
    async with (_FAST_SEM if mode == "fast" else _BROWSER_SEM):
        try:
            html, status = await _fetch_html(url, mode, timeout_s, solve_cloudflare)
        except Exception as e:  # noqa: BLE001 - per-URL failures become error dicts
            return _error("fetch_failed", f"{type(e).__name__}: {e}", url=url)

    # Extraction is as failure-prone as the fetch — trafilatura and
    # html_to_markdown exhaust the recursion limit on deeply nested or
    # malformed markup — and an unguarded raise here sinks the whole batch.
    try:
        if extraction == "html":
            content = html
        elif extraction == "text":
            content = await asyncio.to_thread(_to_text, html)
        else:
            content = await asyncio.to_thread(_to_markdown, html)
    except Exception as e:  # noqa: BLE001 - same per-URL envelope as a fetch failure
        return _error(
            "extract_failed", f"{type(e).__name__}: {e}", url=url, status=status
        )

    return {
        "url": url,
        "status": status,
        "title": _extract_title(html),
        "content": content[:_MAX_CONTENT_CHARS],
        "extraction": extraction,
        "mode": mode,
    }


def _validate_args(mode: str, extraction: str, timeout: float) -> Optional[dict]:
    if mode not in _MODES:
        return _error("invalid_mode", f"mode must be one of {_MODES}, got: {mode!r}")
    if extraction not in _EXTRACTIONS:
        return _error(
            "invalid_extraction",
            f"extraction must be one of {_EXTRACTIONS}, got: {extraction!r}",
        )
    if not 1.0 <= timeout <= _MAX_TIMEOUT_S:
        return _error(
            "invalid_timeout", f"timeout must be 1-{_MAX_TIMEOUT_S:.0f}s, got: {timeout}"
        )
    return None


@mcp.tool()
async def scrape_page(
    url: str,
    mode: str = "fast",
    extraction: str = "markdown",
    timeout: float = _DEFAULT_TIMEOUT_S,
    solve_cloudflare: bool = False,
) -> _OUT_PAGE:
    """Scrape one web page and extract its content.

    Start with mode='fast' (plain HTTP). Use 'browser' when the page needs
    JavaScript rendering, 'stealth' for bot-protected sites; add
    solve_cloudflare=true only when 'stealth' still returns a challenge page.

    Args:
        url: Full http(s) URL.
        mode: fast|browser|stealth.
        extraction: markdown (default) | html (raw) | text (plain).
        timeout: Per-fetch seconds, 1-120.
        solve_cloudflare: Solve Cloudflare challenges (stealth mode only).

    Returns:
        dict: {url, status, title, content, extraction, mode}. content is
        truncated to 400k chars.
        On error: {error, detail} — invalid_url|invalid_mode|
        invalid_extraction|invalid_timeout|fetch_failed.
    """
    bad = _validate_args(mode, extraction, timeout)
    if bad:
        return bad
    return await _scrape_one(url, mode, extraction, timeout, solve_cloudflare)


@mcp.tool()
async def scrape_pages(
    urls: List[str],
    mode: str = "fast",
    extraction: str = "markdown",
    timeout: float = _DEFAULT_TIMEOUT_S,
    solve_cloudflare: bool = False,
) -> _OUT_PAGES:
    """Scrape up to 10 web pages concurrently.

    Same modes as scrape_page; per-URL failures come back as {error, detail}
    entries in results instead of failing the batch.

    Args:
        urls: Full http(s) URLs, max 10 per call.
        mode: fast|browser|stealth.
        extraction: markdown (default) | html (raw) | text (plain).
        timeout: Per-fetch seconds, 1-120.
        solve_cloudflare: Solve Cloudflare challenges (stealth mode only).

    Returns:
        dict: {results, count}. results holds one entry per input URL in
        input order — {url, status, title, content, extraction, mode} or a
        per-URL {error, detail, url}.
        On error: {error, detail} — invalid_urls|invalid_mode|
        invalid_extraction|invalid_timeout.
    """
    bad = _validate_args(mode, extraction, timeout)
    if bad:
        return bad
    if not urls:
        return _error("invalid_urls", "urls must be a non-empty list")
    if len(urls) > _MAX_BULK_URLS:
        return _error(
            "invalid_urls", f"max {_MAX_BULK_URLS} URLs per call, got {len(urls)}"
        )

    # _scrape_one holds the concurrency gate itself, so the batch just fans out.
    results = await asyncio.gather(
        *(_scrape_one(u, mode, extraction, timeout, solve_cloudflare) for u in urls),
        return_exceptions=True,
    )
    entries = [_as_entry(u, r) for u, r in zip(urls, results)]
    return {"results": entries, "count": len(entries)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
