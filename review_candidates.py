import json
import os
import sys

import click

OUTPUT_FILE_DEFAULT = "final_replacements.json"


def load_existing(output_file: str) -> dict[str, dict]:
    if not os.path.exists(output_file):
        return {}
    try:
        with open(output_file, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(r["item_id"]): r for r in data}
    except Exception:
        return {}


def save_all(replacements: dict[str, dict], output_file: str) -> None:
    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(list(replacements.values()), fh, ensure_ascii=False, indent=2)


def print_separator(char: str = "─", width: int = 72) -> None:
    click.echo(char * width)


def wrap_text(text: str, width: int = 64, indent: str = "        ") -> None:
    words = text.split()
    line: list[str] = []
    for w in words:
        if sum(len(x) + 1 for x in line) + len(w) > width:
            click.echo(f"{indent}{' '.join(line)}")
            line = [w]
        else:
            line.append(w)
    if line:
        click.echo(f"{indent}{' '.join(line)}")


def print_result(i: int, c: dict, show_score: bool = False) -> None:
    score_str = f"  [score: {c.get('relevance_score', '?')}/10]" if show_score else ""
    click.echo(f"    [{i}] {c.get('title', '(no title)')}{score_str}")
    click.echo(f"        {c.get('url', '')}")
    if show_score and c.get("reason"):
        click.echo(f"        Reason : {c['reason']}")
    if c.get("snippet"):
        wrap_text(c["snippet"])
    click.echo()


def review_entry(entry: dict) -> dict | None:
    click.clear()

    item_id = entry["item_id"]
    original_link = entry["original_link"]
    original_title = entry.get("original_title", "")
    domain_alive = entry.get("domain_alive", False)
    search_query = entry.get("search_query", "")
    search_error = entry.get("search_error")
    groq_error = entry.get("groq_error")
    ai_candidates: list[dict] = entry.get("ai_candidates", [])
    raw_results: list[dict] = entry.get("raw_results", [])
    no_match_flag: bool = entry.get("no_match_flag", False)

    print_separator()
    click.echo(f"  Title  : {original_title or '(no title)'}")
    click.echo(f"  URL    : {original_link}")
    click.echo(f"  Domain : {'ALIVE' if domain_alive else 'DEAD'}")
    if search_query:
        click.echo(f"  Query  : {search_query}")
    if search_error:
        click.echo(f"  ERROR  : {search_error}")
    if groq_error:
        click.echo(f"  GROQ   : {groq_error}")
    if no_match_flag:
        click.echo("  AI flag: NO GOOD MATCH FOUND")
    click.echo()

    candidates = []

    if ai_candidates:
        label = "  AI-ranked candidates (low confidence — review carefully):" if no_match_flag else "  AI-ranked candidates:"
        click.echo(label)
        for i, c in enumerate(ai_candidates, start=1):
            print_result(i, c, show_score=True)
        candidates = ai_candidates

    if raw_results:
        click.echo("  Search results (no AI ranking):")
        for i, c in enumerate(raw_results, start=len(candidates) + 1):
            print_result(i, c, show_score=False)
        candidates = candidates + raw_results

    if not candidates:
        click.echo("  (no results found)\n")

    prompt_parts = [f"1-{len(candidates)}"] if candidates else []
    prompt_parts += ["c=custom", "s=skip", "q=quit"]
    prompt_str = f"  Choice [{', '.join(prompt_parts)}]: "

    while True:
        raw = click.prompt(prompt_str, default="s", prompt_suffix="").strip().lower()

        if raw == "q":
            return None

        if raw == "s":
            return {
                "item_id": item_id,
                "original_link": original_link,
                "original_title": original_title,
                "replacement_link": None,
                "replacement_title": None,
                "method": "skipped",
            }

        if raw == "c":
            custom_url = click.prompt("  Enter custom URL").strip()
            custom_title = click.prompt("  Enter title for custom URL (leave blank to use original title)", default="").strip()
            return {
                "item_id": item_id,
                "original_link": original_link,
                "original_title": original_title,
                "replacement_link": custom_url,
                "replacement_title": custom_title or original_title,
                "method": "custom",
            }

        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(candidates):
                chosen = candidates[idx]
                return {
                    "item_id": item_id,
                    "original_link": original_link,
                    "original_title": original_title,
                    "replacement_link": chosen.get("url", ""),
                    "replacement_title": chosen.get("title", ""),
                    "method": "candidate",
                }

        click.echo("  Invalid choice, please try again.")


@click.command()
@click.option("--input", "-i", "input_file", default="candidates.json", show_default=True, help="Input candidates JSON.")
@click.option("--output", "-o", default=OUTPUT_FILE_DEFAULT, show_default=True, help="Output final replacements JSON.")
def main(input_file: str, output: str) -> None:
    """Interactively review AI candidates and save final replacements."""
    if not os.path.exists(input_file):
        click.echo(f"ERROR: {input_file} not found. Run find_candidates.py first.", err=True)
        sys.exit(1)

    with open(input_file, encoding="utf-8") as fh:
        entries: list[dict] = json.load(fh)

    total = len(entries)
    replacements = load_existing(output)

    already_done = len(replacements)
    if already_done:
        click.echo(f"Resuming: {already_done}/{total} already reviewed.")

    pending = [e for e in entries if str(e["item_id"]) not in replacements]

    if not pending:
        click.echo("All entries already reviewed. Nothing to do.")
    else:
        click.echo(f"{len(pending)} entries to review. (s=skip, c=custom url, q=quit)\n")

    for i, entry in enumerate(pending, start=1):
        click.echo(f"\n[{i}/{len(pending)}]")
        result = review_entry(entry)

        if result is None:
            click.echo("\nQuit. Progress saved.")
            break

        replacements[str(result["item_id"])] = result
        save_all(replacements, output)

    print_separator("=")
    all_records = list(replacements.values())
    click.echo(f"Summary ({len(all_records)} total reviewed):")
    click.echo(f"  Replaced (candidate) : {sum(1 for r in all_records if r['method'] == 'candidate')}")
    click.echo(f"  Custom URL           : {sum(1 for r in all_records if r['method'] == 'custom')}")
    click.echo(f"  Skipped              : {sum(1 for r in all_records if r['method'] == 'skipped')}")
    click.echo(f"\nSaved to {output}")


if __name__ == "__main__":
    main()
