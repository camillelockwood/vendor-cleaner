# Vendor Name Cleaner

**A Python tool that finds and standardizes duplicate vendor names in unorganized and difficult to grasp
financial spreadsheets, using rule-based logic for the easy calls and Claude, an LLM, for the judgment calls.**

> Tested on the City of Boston FY25 Checkbook: **117,898 rows / 7,643 unique
> vendor names → 62 duplicate vendor groups surfaced** that otherwise split a
> single vendor's spending across several spellings.

---

## The Problem

The same vendor shows up as `Dennis K. Burke, Inc.`,`Dennis K Burke Inc`, and `DENNIS K. BURKE`. To a human they're obviously one vendor; to a spreadsheet they're three, resulting in incorrect or unaccounted for totals, reports, and budgets.
Manually cleaning and organizing spreadsheets can be slow and costly. For small or under-resourced teams, those hours come straight out of their mission. 

## The Approach

I split the work by what each tool is actually good at:

| Step | Tool | Why |
|------|------|-----|
| Trim whitespace, fix comma spacing | **Python (regex)** | Deterministic — no AI needed |
| Group likely-duplicate names | **Python (`difflib`, blocking keys)** | Cheap; shrinks 7,643 names to ~60 small groups |
| Decide "same vendor?" + pick best name | **Claude** | Genuine fuzzy judgment |
| Apply only confident merges; flag the rest | **Python + a confidence gate** | A human approves uncertain calls — the model never silently overwrites financial records |

Letting plain logic handle the certain cases and a human handle the uncertain ones, is what makes a tool like this safe to run on real financial data.

## What The Python Actually Does

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
- **reproducibility & guardrails** — Claude is called with `temperature=0` so
  runs are repeatable; its chosen name is validated against the group (no
  invented names); the API call retries on transient errors; and overlapping
  candidate groups are merged so no vendor is judged twice
- **tested** — `test_clean_vendors.py` covers the deterministic logic (run it
  with `python test_clean_vendors.py`, no API key needed)

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
  one-line reasoning, the total spend tied to that name (so you can see the
  dollar value fragmented across duplicates), and whether it was applied or
  flagged for review

## Example Run

```text
$ python clean_vendors.py checkbook_explorer_fy25_updated.csv
Loaded 117,898 rows
7,643 unique vendor names after basic cleanup
62 candidate duplicate groups found (only these are sent to Claude)
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
  That was an intentional tradeoff chosen on purpose: the canonical name always has to be one that already appears in the data, so the tool can't invent a name. 
- This measures *precision* on flagged candidates, not *recall*. Duplicates that the
  grouping step never surfaces are not tested here.

**What the duplicates were hiding:** because the change log totals spend per name,
it surfaced real money fragmented across spellings — one variant of "YMCA of
Greater Boston" alone carried ~$4.7M, a "Boston Chinatown Neighborhood Center"
variant ~$1.8M, and "Aramsco" ~$1.24M — all disconnected from the main vendor
record until merged.

## Honest limitations

- Only catches duplicates the grouping step puts together; wildly different
  spellings of one vendor can still slip through.
- "Same vendor" is sometimes genuinely ambiguous (subsidiaries, `dba` names),
  which is exactly why low-confidence calls are flagged, not auto-applied.
- Uses public, non-sensitive city data on purpose; real customer/donor records
  contain personal info that shouldn't be sent to an API without care.

## What I'd Do Differently 

- The auto-apply gate trusts Claude's self-reported high/medium/low. Large Language Models can be overconfident, so I'd back that with a more objective signal, like string-distance thresholds, or only auto-applying when the rule-based groupings and the model agree, opposed to taking the models word for how sure it is. 
- Right now, human review means reading a flagged CSV. If I were building it for a real non-technical staffer, I'd have designed the review step as an actual approve/reject interface from the start, instead of leaving it as a spreadsheet column.  

---

*Built by Camille Lockwood. Data: [City of Boston Open Data](https://data.boston.gov).*
