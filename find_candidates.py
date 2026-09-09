import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse

import click
import httpx
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

SERPER_BASE = "https://google.serper.dev/search"
WAYBACK_API = "https://archive.org/wayback/available"
SEARCH_DELAY = 2.0
RAW_TOP_N = 10
GROQ_MODEL = "llama-3.3-70b-versatile"


def extract_keywords(url: str) -> list[str]:
    raw = re.split(r"[/\-_]", urlparse(url).path)
    keywords = []
    for segment in raw:
        segment = segment.strip()
        if not segment or len(segment) <= 2 or re.fullmatch(r"\d{1,4}", segment):
            continue
        segment = re.sub(r"\.(html?|php|aspx?|jsp|cfm)$", "", segment, flags=re.I)
        if segment:
            keywords.append(segment)
    return keywords


def get_domain(url: str) -> str:
    return urlparse(url).netloc


async def serper_search(query: str, api_key: str, client: httpx.AsyncClient, num: int = RAW_TOP_N) -> tuple[list[dict], str | None]:
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num}
    try:
        resp = await client.post(SERPER_BASE, headers=headers, json=payload, timeout=30.0)
        if resp.status_code == 401:
            raise SystemExit("ERROR: Serper API key is invalid or expired. Check SERPER_API_KEY in your .env")
        resp.raise_for_status()
        organic = resp.json().get("organic", [])
        return [{"title": r.get("title", ""), "url": r.get("link", ""), "snippet": r.get("snippet", "")} for r in organic[:num]], None
    except SystemExit:
        raise
    except Exception as exc:
        log.error("Serper error for query %r: %s", query, exc)
        return [], f"{type(exc).__name__}: {exc}"


async def wayback_title(url: str, client: httpx.AsyncClient) -> str | None:
    try:
        resp = await client.get(WAYBACK_API, params={"url": url}, timeout=15.0)
        resp.raise_for_status()
        closest = resp.json().get("archived_snapshots", {}).get("closest", {})
        if closest.get("available"):
            page = await client.get(closest["url"], timeout=20.0, follow_redirects=True)
            m = re.search(r"<title[^>]*>([^<]+)</title>", page.text, re.I)
            if m:
                return m.group(1).strip()
    except Exception as exc:
        log.debug("Wayback error for %s: %s", url, exc)
    return None


async def rank_with_groq(original_url: str, original_title: str, raw_results: list[dict], groq_client: AsyncGroq) -> tuple[list[dict], bool]:
    if not raw_results:
        return [], True, None

    numbered = "\n".join(
        f"{i + 1}. Title: {r['title']}\n   URL: {r['url']}\n   Snippet: {r['snippet']}"
        for i, r in enumerate(raw_results)
    )

    prompt = f"""You are helping find replacement URLs for a broken/dead web page.

Original broken URL : {original_url}
Original page title : {original_title}

Search results to evaluate:
{numbered}

Task: Decide which of the above results are the best replacement for the original broken page.

Return ONLY a JSON object with exactly this structure (no markdown, no extra text):
{{
  "ranked": [
    {{
      "result_index": <1-based index>,
      "title": "<result title>",
      "url": "<result url>",
      "snippet": "<result snippet>",
      "relevance_score": <integer 1-10>,
      "reason": "<one-line explanation of why this is or isn't a good replacement>"
    }}
  ],
  "no_match": <true if none of the results are confident replacements, false otherwise>
}}

Rules:
- Always include the top 3 results in "ranked" (by relevance_score), even if scores are low.
- Use "no_match": true to signal low confidence — do NOT leave "ranked" empty because of it.
- Only leave "ranked" empty if there are literally zero search results to evaluate.
- relevance_score 8-10 = strong match, 5-7 = partial match, 1-4 = poor match.
"""

    try:
        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        data = json.loads(response.choices[0].message.content)
        ranked = data.get("ranked", [])[:3]
        no_match = bool(data.get("no_match", len(ranked) == 0))
        ai_candidates = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
                "relevance_score": int(r.get("relevance_score", 0)),
                "reason": r.get("reason", ""),
            }
            for r in ranked
        ]
        return ai_candidates, no_match, None
    except Exception as exc:
        log.error("Groq ranking error for %s: %s", original_url, exc)
        return [], True, f"{type(exc).__name__}: {exc}"


async def process_entry(entry: dict, serp_key: str, groq_client: AsyncGroq, http_client: httpx.AsyncClient) -> dict:
    item_id = entry["item_id"]
    link = entry["link"]
    title = entry.get("title", "")
    domain_alive = entry.get("domain_alive", False)
    domain = get_domain(link)
    kw_str = " ".join(extract_keywords(link))

    raw_results: list[dict] = []
    search_query = ""
    search_error: str | None = None

    if domain_alive:
        primary_query = f"site:{domain} {title}".strip()
        fallback_query = f"{title} {domain}".strip()

        search_query = primary_query
        raw_results, search_error = await serper_search(primary_query, serp_key, http_client)
        await asyncio.sleep(SEARCH_DELAY)
        if not raw_results:
            log.info("  [alive] no site: results, trying fallback")
            search_query = fallback_query
            raw_results, search_error = await serper_search(fallback_query, serp_key, http_client)
            await asyncio.sleep(SEARCH_DELAY)

    else:
        wb_title = await wayback_title(link, http_client)
        effective_title = wb_title or title
        broader_query = f"{effective_title} {kw_str}".strip()
        title_only_query = title.strip()

        search_query = broader_query
        raw_results, search_error = await serper_search(broader_query, serp_key, http_client)
        await asyncio.sleep(SEARCH_DELAY)
        if not raw_results and title_only_query:
            log.info("  [dead] no results, falling back to title-only search")
            search_query = title_only_query
            raw_results, search_error = await serper_search(title_only_query, serp_key, http_client)
            await asyncio.sleep(SEARCH_DELAY)

    if raw_results:
        search_error = None

    ai_candidates: list[dict] = []
    no_match_flag = True
    groq_error: str | None = None

    if raw_results:
        click.echo(f"  -> {len(raw_results)} raw result(s), ranking with Groq ...")
        ai_candidates, no_match_flag, groq_error = await rank_with_groq(link, title, raw_results, groq_client)
    else:
        log.warning("  No raw results for %s — skipping Groq step", link)

    return {
        "item_id": item_id,
        "original_link": link,
        "original_title": title,
        "domain_alive": domain_alive,
        "search_query": search_query,
        "search_error": search_error,
        "groq_error": groq_error,
        "raw_results": raw_results[:5],
        "ai_candidates": ai_candidates,
        "no_match_flag": no_match_flag,
    }


@click.command()
@click.option("--input", "-i", "input_file", default="dead_links.json", show_default=True, help="Input dead links JSON.")
@click.option("--output", "-o", default="candidates.json", show_default=True, help="Output candidates JSON.")
@click.option("--concurrency", default=3, show_default=True, help="Max concurrent search requests.")
def main(input_file: str, output: str, concurrency: int) -> None:
    """Find AI-ranked candidate replacement URLs for dead links via Serper + Groq."""
    serp_key = os.environ.get("SERPER_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    if not serp_key:
        click.echo("ERROR: SERPER_API_KEY environment variable is not set.", err=True)
        raise SystemExit(1)
    if not groq_key:
        click.echo("ERROR: GROQ_API_KEY environment variable is not set.", err=True)
        raise SystemExit(1)

    groq_client = AsyncGroq(api_key=groq_key)

    click.echo(f"Reading {input_file} ...")
    with open(input_file, encoding="utf-8") as fh:
        entries: list[dict] = json.load(fh)

    total = len(entries)

    existing: dict[str, dict] = {}
    if os.path.exists(output):
        try:
            with open(output, encoding="utf-8") as fh:
                existing = {str(r["item_id"]): r for r in json.load(fh)}
        except Exception:
            pass

    def needs_retry(entry: dict) -> bool:
        r = existing.get(str(entry["item_id"]))
        if r is None:
            return True
        return bool(r.get("groq_error") or r.get("search_error"))

    pending = [e for e in entries if needs_retry(e)]

    if existing:
        skip_count = total - len(pending)
        click.echo(f"Resuming: {skip_count}/{total} already done, {len(pending)} to process (errors or missing).\n")
    else:
        click.echo(f"Found {total} dead link(s) to process.\n")

    new_results: list[dict] = []

    async def run_all() -> None:
        sem = asyncio.Semaphore(concurrency)

        async def bounded(entry: dict, idx: int) -> dict:
            async with sem:
                click.echo(f"[{idx}/{len(pending)}] {entry['link'][:80]}")
                result = await process_entry(entry, serp_key, groq_client, client)
                click.echo(f"  -> {len(result['ai_candidates'])} AI candidate(s) | no_match={result['no_match_flag']}")
                return result

        async with httpx.AsyncClient(follow_redirects=True) as client:
            tasks = [bounded(entry, i + 1) for i, entry in enumerate(pending)]
            for coro in asyncio.as_completed(tasks):
                new_results.append(await coro)

    if pending:
        asyncio.run(run_all())

    existing.update({str(r["item_id"]): r for r in new_results})

    id_order = {e["item_id"]: i for i, e in enumerate(entries)}
    final = sorted(existing.values(), key=lambda r: id_order.get(r["item_id"], 9999))

    with open(output, "w", encoding="utf-8") as fh:
        json.dump(final, fh, ensure_ascii=False, indent=2)

    matched = sum(1 for r in final if not r["no_match_flag"])
    click.echo(f"\nDone. {matched}/{total} entries have at least one AI candidate.")
    click.echo(f"Saved to {output}")


if __name__ == "__main__":
    main()
