# Vendor Name Cleaner

**A Python tool that finds and standardizes duplicate vendor names in disorganized
financial spreadsheets, using rule-based logic for the easy calls and Claude, an LLM, for the judgment calls.**

> Tested on the City of Boston FY25 Checkbook: **117,898 rows / 7,643 unique
> vendor names → 62 duplicate vendor groups surfaced** that otherwise split a
> single vendor's spending across several spellings.

---

## The Problem

The same vendor shows up as `Dennis K. Burke, Inc.`,`Dennis K Burke Inc`, and `DENNIS K. BURKE`. To a human they're one vendor; to a spreadsheet they're three. This can result in incorrect or unaccounted for totals, reports, and budgets.
Manually cleaning and organizing spreadsheets can be slow and costly. For small or under-resourced teams, those hours come straight out of their mission. 

## Why This Matters
Duplicate vendor names can distort any organization's view of its own funds. When one vendor is recorded several ways, budgets, reports, and audits rest on numbers that are wrong, and the usual fix is paying someone to reconcile thousands of rows by hand.

On Boston's data, one vendor's spending was split across spellings totaling about $4.7M, disconnected from its record until it was merged. For a small or under-resourced team, a tool that fixes this problem without significant use of resources, gives back both accurate numbers and the staff time to spend on the work that actually matters.

## The Approach

I split the work by what each tool is actually good at:

| Step | Tool | Why |
|------|------|-----|
| Trim whitespace, fix comma spacing | **Python (regex)** | Deterministic — no AI needed |
| Group likely-duplicate names | **Python (`difflib`, blocking keys)** | Cheap; shrinks 7,643 names to ~60 small groups |
| Decide "same vendor?" + pick best name | **Claude** | Genuine fuzzy judgment |
| Apply only confident merges; flag the rest | **Python + two-signal gate** | Model confidence + string distance must both agree — the model never silently overwrites financial records |

Letting plain logic handle the certain cases and a human handle the uncertain ones is what makes a tool like this safe to run on real financial data.

## What The Python Actually Does

This is a from-scratch data pipeline in standard-library Python (only the
Anthropic SDK is an external dependency):

- **`csv`** — streams a 118k-row file without loading a heavyweight framework
- **`re` (regex)** — normalizes text and builds "blocking keys" that strip
  company suffixes (`Inc`, `LLC`, `dba`…) so variants collide into one group
- **`difflib.SequenceMatcher`** — catches near-duplicate spellings the keys miss
- **batched API calls** — sends candidate groups to Claude ~15 at a time and
  parses structured JSON back, instead of one slow call per name
- **`argparse`** — a real CLI with `--no-llm` (runs with zero API key),
  `--eval` (builds an accuracy sample), and `--recall` (measures grouping recall
  against a labeled pair set) flags
- **two-signal auto-apply gate** — Claude must say same entity + high/medium
  confidence *and* a string-distance check must independently agree; either
  signal can veto an auto-apply, which defends against model overconfidence
- **reproducibility & guardrails** — Claude is called with `temperature=0` so
  runs are repeatable; its chosen name is validated against the group (no
  invented names); the API call retries on transient errors; and overlapping
  candidate groups are merged so no vendor is judged twice
- **tested** — `test_clean_vendors.py` covers the deterministic logic including
  the similarity gate (run with `python3 test_clean_vendors.py`, no API key needed)

## Quickstart

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...

# See it run with no API key (rule-based grouping only):
python3 clean_vendors.py checkbook_explorer_fy25_updated.csv --no-llm

# Full version, Claude judging each group:
python3 clean_vendors.py checkbook_explorer_fy25_updated.csv

# Build a sample to hand-check accuracy:
python3 clean_vendors.py checkbook_explorer_fy25_updated.csv --eval 50

# Measure grouping recall against a labeled pair set:
python3 clean_vendors.py checkbook_explorer_fy25_updated.csv --recall known_duplicates.csv
```

## Outputs

- **`..._cleaned.csv`** — original data + a new `Vendor Name Canonical` column
- **`..._change_log.csv`** — every proposed merge with Claude's confidence,
  one-line reasoning, the total spend tied to that name (so you can see the
  dollar value fragmented across duplicates), and whether it was applied or
  flagged for review

## Example Run

```text
$ python3 clean_vendors.py checkbook_explorer_fy25_updated.csv --recall known_duplicates.csv
Loaded 117,898 rows
7,643 unique vendor names after basic cleanup
62 candidate duplicate groups found (only these are sent to Claude)

Recall check (known_duplicates.csv):
  Known duplicate pairs : 41
  Surfaced by grouping  : 41 (100%)

Asking Claude (claude-haiku-4-5) to judge 62 groups...

Done.
  merges applied:        41
  groups needing review: 21
  -> checkbook_explorer_fy25_updated_cleaned.csv
  -> checkbook_explorer_fy25_updated_change_log.csv
```

A few real merges it made on the Boston data:

| Original name | Standardized to |
|---|---|
| `TODISCO SERVICES INC.` | `Todisco Services, Inc.` |
| `white, kyle` | `White, Kyle` |
| `OFF DUTY MANAGMENT` | `Off Duty Management` |
| `Language Connections` | `Language Connections Inc.` |
| `S G Harold Plumbing & Heating` | `S.G. Harold Plumbing & Heating` |

## Results & Evaluation

Run on the City of Boston FY25 Checkbook (117,898 rows, 7,643 unique vendor
names), with `temperature=0` so the run is reproducible:

- Duplicate groups surfaced: **62**
- Merges applied automatically: **41**
- Flagged for human review: **21**
- Hand-checked **all 41** applied merges: **41/41 correctly identified as the
  same vendor.** In ~6 cases the canonical name it chose was a messier existing
  variant (e.g. ALL CAPS or missing punctuation) rather than the cleanest option.
  That was an intentional tradeoff: the canonical name always has to be one that
  already appears in the data, so the tool can't invent a name.
- **Grouping recall on labeled set: 41/41 (100%).** The `known_duplicates.csv`
  file records all 41 confirmed pairs from the Boston run; re-running with
  `--recall` confirms the grouping step surfaces every one of them consistently
  across runs. This is a consistency check, not a measure of undiscovered
  duplicates — pairs the grouping step never produces can't appear in this set.
- This measures *precision* on applied merges and *recall* on a labeled set.
  Duplicates that the grouping step never surfaces are not counted, which is the
  main limitation noted below.

## Honest Limitations

- Only catches duplicates the grouping step puts together; wildly different
  spellings of one vendor can still slip through.
- The labeled recall set (`known_duplicates.csv`) was built from this tool's own
  output, so it confirms consistency, not coverage. A truly independent labeled
  set (assembled without running the tool first) would give a harder recall number.
- "Same vendor" is sometimes ambiguous (subsidiaries, `dba` names),
  which is exactly why low-confidence calls are flagged, not auto-applied.
- Uses public, non-sensitive city data on purpose; real customer/donor records
  contain personal info that shouldn't be sent to an API without care.

## What I Did to Address Past Limitations

The original version trusted Claude's self-reported confidence level as the sole
gate for auto-applying a merge. Two improvements ship in this version:

1. **Two-signal auto-apply gate** — string-distance similarity (blocking-key
   similarity ≥ 0.70 *or* raw name similarity ≥ 0.60) must agree with the
   model's confidence before a merge is applied. If Claude is overconfident about
   genuinely dissimilar names, the objective gate blocks the merge and flags it
   for human review instead.
2. **`--recall` flag** — accepts a CSV of known duplicate pairs and reports what
   fraction the grouping step surfaced, making the tool's recall measurable and
   improvable over time.

---

*Built by Camille Lockwood. Data: [City of Boston Open Data](https://data.boston.gov).*
