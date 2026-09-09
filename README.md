# alternate_links_finder

Finds live replacements for dead links in a curated link collection.

Link collections rot. A directory of a few hundred government or institutional pages
loses a steady fraction every year to site reorganizations, CMS migrations and dropped
domains. The document usually still exists somewhere. It moved, and nothing points at
the new location any more.

Checking those by hand is slow, and most of the time is spent on searches that go
nowhere. This narrows each dead link down to a short ranked list a person can judge in
a few seconds.

## How it works

### 1. `extract_dead_links.py`

Reads a link collection and pulls out the entries that are dead, meaning `working` is
false or the status code is anything other than 200. For each one it sends a HEAD
request to the domain root and records whether the domain itself still answers. Domain
results are cached, so fifty dead pages on one host cost one request.

That flag drives everything downstream. A dead page on a live domain has usually just
moved within the same site. A dead page on a dead domain needs a different approach
entirely.

### 2. `find_candidates.py`

Builds a search per link, then ranks what comes back.

When the domain is alive it searches `site:{domain} {title}`, which keeps results inside
the organization that published the original. If that returns nothing it drops the site
restriction and retries.

When the domain is dead there is no site left to search, and the stored title is often
the only thing that survived. So it asks the Wayback Machine for the closest snapshot,
reads the `<title>` out of the archived HTML, and searches on that plus keywords pulled
from the dead URL's own path.

The top ten results go to an LLM, which returns its best three with a relevance score
from 1 to 10, a one line reason for each, and a `no_match` flag when none of them look
convincing. Scoring beats filtering here. A weak candidate with a stated reason is more
useful to a reviewer than one that was silently dropped.

Output is written after every entry and the script resumes where it stopped, retrying
only the entries that recorded a search or model error. Both APIs rate limit, and any
run over a few hundred links will hit that.

### 3. `review_candidates.py`

A terminal review loop. It shows the original link, the ranked candidates with their
scores and reasons, then the unranked search results below them. Pick a number, paste a
custom URL, skip, or quit. Progress saves after every decision.

## Why a person stays in the loop

A model can tell you a page is topically close to a dead one. It cannot tell you they
are the same document, because the original is gone and there is nothing left to compare
against. A ministry's 2022 annual programme page and its current annual programme page
will score highly against each other while containing completely different files.

So this shortlists, it does not decide. On real collections most dead links produce a
plausible candidate and a smaller number produce the right one. Read the score as a
suggested reading order rather than a verdict.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Two keys go in `.env`: [serper.dev](https://serper.dev/api-key) for search and
[Groq](https://console.groq.com/keys) for ranking.

## Usage

```bash
python extract_dead_links.py sample/links.json -o dead_links.json
python find_candidates.py -i dead_links.json -o candidates.json
python review_candidates.py -i candidates.json -o final_replacements.json
```

`sample/links.json` holds ten fictional entries in the expected input shape, so the
pipeline runs on a fresh clone. Point stage one at your own collection to do real work.

Useful flags: `--timeout` on stage one for slow hosts, `--concurrency` on stage two,
which defaults to 3 and is what you turn down when search starts returning errors.

## Input format

Stage one expects a JSON array. Only four fields matter, and the rest are carried
through or ignored.

| Field | Type | Used for |
|---|---|---|
| `item_id` | int | Identity across all three stages, and how resume works |
| `link` | string | The URL to check |
| `title` | string | Search text, and what you read during review |
| `status_code` | int | Liveness. Anything other than 200 counts as dead |
| `working` | bool | Optional. False marks an entry dead regardless of status code |

## Generated files

| File | Written by | Contents |
|---|---|---|
| `dead_links.json` | stage 1 | Dead entries plus a `domain_alive` flag |
| `candidates.json` | stage 2 | Ranked candidates, raw results, and any errors per entry |
| `final_replacements.json` | stage 3 | Your reviewed decisions, one per entry |

All three are gitignored. Stage two and stage three both read their own output back on
startup, so stopping a run and restarting it later is the expected way to use them.

## Limitations

- Search and ranking both cost API calls, and roughly one search plus one model call per
  dead link. A large collection needs paid tiers on both.
- Wayback recovery only reads the archived page title. A snapshot with a generic title
  like "Home" gives the search almost nothing to work with.
- Nothing here verifies that a chosen replacement actually contains the same document.
  That judgment is the reviewer's, and it is the reason stage three exists.
