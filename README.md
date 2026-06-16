# 🧹 Vendor Name Cleaner

**A Python tool that finds and standardizes duplicate vendor names in messy
financial spreadsheets — using rule-based logic for the easy calls and Claude
(an LLM) for the judgment calls.**

> Tested on the City of Boston FY25 Checkbook: **117,898 rows / 7,643 unique
> vendor names → 60+ duplicate vendor groups surfaced** that otherwise split a
> single vendor's spending across several spellings.

---

## The problem

Real spreadsheets are messy. The same vendor shows up as `Dennis K. Burke, Inc.`,
`Dennis K Burke Inc`, and `DENNIS K. BURKE`. To a human they're obviously one
vendor; to a spreadsheet they're three — so totals, reports, and budgets are
quietly wrong. Cleaning this by hand across thousands of rows is a half-day job
no one wants.

## The approach

I split the work by what each tool is actually good at:

| Step | Tool | Why |
|------|------|-----|
| Trim whitespace, fix comma spacing | **Python (regex)** | Deterministic — no AI needed |
| Group likely-duplicate names | **Python (`difflib`, blocking keys)** | Cheap; shrinks 7,643 names to ~60 small groups |
| Decide "same vendor?" + pick best name | **Claude** | Genuine fuzzy judgment |
| Apply only confident merges; flag the rest | **Python + a confidence gate** | A human approves uncertain calls — the model never silently overwrites financial records |

That last row is the point: **knowing when *not* to fully automate** is the
difference between a careless script and a tool you'd trust near real data.

## What the Python actually does

This is a from-scratch data pipeline in standard-library Python (only the
Anthropic SDK is an external dependency):

- **`csv`** — streams a 118k-row file without loading a heavyweight framework
- **`re` (regex)** — normalizes text and builds "blocking keys" that strip
  company suffixes (`Inc`, `LLC`, `dba`…) so variants collide into one group
- **`difflib.SequenceMatcher`** — catches near-duplicate spellings the keys miss
- **batched API calls** — sends candidate groups to Claude ~15 at a time and
  parses structured JSON back, instead of one slow call per name
- **`argparse`** — a real CLI with `--no-llm` (runs with zero API key) and
  `--eval` (builds an accuracy sample) flags

## Quickstart

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...

# See it run with no API key (rule-based grouping only):
python clean_vendors.py checkbook_explorer_fy25_updated.csv --no-llm

# Full version, Claude judging each group:
python clean_vendors.py checkbook_explorer_fy25_updated.csv

# Build a sample to hand-check accuracy:
python clean_vendors.py checkbook_explorer_fy25_updated.csv --eval 50
```

## Outputs

- **`..._cleaned.csv`** — original data + a new `Vendor Name Canonical` column
- **`..._change_log.csv`** — every proposed merge with Claude's confidence,
  one-line reasoning, and whether it was applied or flagged for review

## Example run

```text
$ python clean_vendors.py checkbook_explorer_fy25_updated.csv
Loaded 117,898 rows
7,643 unique vendor names after basic cleanup
62 candidate duplicate groups found (only these are sent to Claude)
Asking Claude (claude-haiku-4-5) to judge 62 groups...

Done.
  merges applied:        52
  groups needing review: 15
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

## Results & evaluation

Run on the City of Boston FY25 Checkbook (117,898 rows, 7,643 unique vendor names):

- Duplicate groups surfaced: **62**
- Merges applied automatically: **52**
- Flagged for human review: **15**
- Hand-checked a random 50-merge sample: **50/50 correctly identified as the
  same vendor.** In 1 case the canonical name it chose was less complete than
  ideal (right vendor, imperfect label).
- Note: this sample measures *precision* on flagged candidates, not *recall* —
  duplicates the grouping step never surfaces aren't tested here.

## Honest limitations

- Only catches duplicates the grouping step puts together; wildly different
  spellings of one vendor can still slip through.
- "Same vendor" is sometimes genuinely ambiguous (subsidiaries, `dba` names) —
  which is exactly why low-confidence calls are flagged, not auto-applied.
- Uses public, non-sensitive city data on purpose; real customer/donor records
  contain personal info that shouldn't be sent to an API without care.

## What I'd build next

- Add address / vendor-ID as a second matching signal so dedup isn't name-only.
- A one-click review screen so a non-technical staffer can approve flagged
  merges — the piece that makes it genuinely hand-off-able.

---

*Built by Camille Lockwood. Data: [City of Boston Open Data](https://data.boston.gov).*
