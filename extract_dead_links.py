import json
import sys
from urllib.parse import urlparse

import click
import httpx


def is_domain_alive(url: str, timeout: float = 10.0) -> bool:
    parsed = urlparse(url)
    domain_root = f"{parsed.scheme}://{parsed.netloc}"
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.head(domain_root)
            return resp.status_code < 500
    except Exception:
        return False


def is_dead(entry: dict) -> bool:
    if entry.get("working") is False:
        return True
    status = entry.get("status_code")
    if status is not None and status != 200:
        return True
    return False


@click.command()
@click.argument("json_file", type=click.Path(exists=True, readable=True))
@click.option("--output", "-o", default="dead_links.json", show_default=True, help="Output file path.")
@click.option("--timeout", default=10.0, show_default=True, help="Timeout in seconds for domain checks.")
def main(json_file: str, output: str, timeout: float) -> None:
    """Extract dead links from JSON_FILE and check domain liveness."""
    click.echo(f"Reading {json_file} ...")
    with open(json_file, encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, list):
        click.echo("ERROR: Expected a JSON array at the top level.", err=True)
        sys.exit(1)

    total = len(data)
    dead_entries = [e for e in data if is_dead(e)]

    click.echo(f"Total links   : {total}")
    click.echo(f"Dead links    : {len(dead_entries)}")

    results = []
    domain_cache: dict[str, bool] = {}

    with click.progressbar(
        dead_entries,
        label="Checking domains",
        item_show_func=lambda e: e.get("link", "")[:60] if e else "",
    ) as bar:
        for entry in bar:
            link = entry.get("link", "")
            parsed = urlparse(link)
            domain_key = f"{parsed.scheme}://{parsed.netloc}"

            if domain_key not in domain_cache:
                domain_cache[domain_key] = is_domain_alive(link, timeout=timeout)

            results.append({
                "item_id": entry.get("item_id"),
                "link": link,
                "title": entry.get("title", ""),
                "status_code": entry.get("status_code"),
                "domain_alive": domain_cache[domain_key],
            })

    alive_domains = sum(1 for r in results if r["domain_alive"])
    click.echo(f"\nDead links with alive domains : {alive_domains}")
    click.echo(f"Dead links with dead domains  : {len(results) - alive_domains}")

    with open(output, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    click.echo(f"\nSaved {len(results)} dead link(s) to {output}")


if __name__ == "__main__":
    main()
