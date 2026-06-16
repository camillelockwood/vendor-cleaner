#!/usr/bin/env python3
"""
clean_vendors.py  —  Messy-spreadsheet cleaner (vendor-name edition)

What it does
------------
Takes a messy spreadsheet of payments and finds vendor names that are really
the SAME vendor entered inconsistently (e.g. "Dennis K. Burke, Inc." vs
"Dennis K Burke Inc"), then standardizes them to one canonical name.

The design idea (this is the part that shows judgment):
  - PLAIN PYTHON does the cheap, deterministic work: trim whitespace, fix
    spacing, and GROUP likely-duplicate names together. No AI needed.
  - CLAUDE is used ONLY for the genuinely fuzzy judgment calls inside each
    group: "are these actually the same vendor, and what's the best name?"
  - Low-confidence decisions are NOT applied automatically — they're flagged
    for a human to approve. You never let the model silently overwrite records.

Outputs
-------
  1. <input>_cleaned.csv      original data + a new "Vendor Name Canonical" column
  2. <input>_change_log.csv   every proposed merge, with Claude's reasoning,
                              confidence, and whether it was applied or needs review

Run it
------
  # 1) See it work with NO API key (rule-based grouping only):
  python clean_vendors.py checkbook_explorer_fy25_updated.csv --no-llm

  # 2) Full version (needs an Anthropic API key):
  export ANTHROPIC_API_KEY=sk-...
  python clean_vendors.py checkbook_explorer_fy25_updated.csv

  # Optional: build a small eval sample to hand-check accuracy
  python clean_vendors.py checkbook_explorer_fy25_updated.csv --eval 50
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher

VENDOR_COL = "Vendor Name"          # the messy column we clean
AMOUNT_COL = "Monetary Amount"      # used only for context in the report
MODEL = "claude-haiku-4-5-20251001" # cheap + fast; swap to a Sonnet model for tougher cases


# ----------------------------------------------------------------------------
# STEP 1 — deterministic cleanup (plain Python, no AI)
# ----------------------------------------------------------------------------
def basic_clean(name: str) -> str:
    """Fix the obvious, rule-based stuff: stray whitespace and comma spacing."""
    name = name.strip()
    name = re.sub(r"\s+", " ", name)        # collapse multiple spaces
    name = re.sub(r"\s*,\s*", ", ", name)   # normalize ", " spacing
    return name


def blocking_key(name: str) -> str:
    """
    A loose 'fingerprint' so likely-duplicate names land in the same group.
    Lowercases, drops punctuation and common company suffixes. This is how we
    AVOID asking Claude about every pair — we only ask within small groups.
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    for suffix in [" inc", " llc", " co", " corp", " ltd", " the",
                   " dba", " company", " incorporated", " lp", " pc"]:
        s = re.sub(suffix + r"\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ----------------------------------------------------------------------------
# STEP 2 — group candidate duplicates
# ----------------------------------------------------------------------------
def find_candidate_groups(names):
    """
    Return a list of groups (each a list of 2+ raw names) that MIGHT be the
    same vendor. Two passes: (a) shared blocking key, (b) high string
    similarity inside the same first letter, to catch typos the key misses.
    """
    by_key = defaultdict(set)
    for n in names:
        by_key[blocking_key(n)].add(n)

    groups = [sorted(v) for v in by_key.values() if len(v) > 1]

    # Second pass: near-identical names that ended up under different keys.
    seen = {n for g in groups for n in g}
    by_letter = defaultdict(list)
    for n in names:
        if n not in seen and n:
            by_letter[blocking_key(n)[:4]].append(n)  # tight prefix block = fast
    for bucket in by_letter.values():
        if len(bucket) > 60:   # skip pathological buckets to stay quick
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                if similar(blocking_key(bucket[i]), blocking_key(bucket[j])) >= 0.92:
                    groups.append(sorted([bucket[i], bucket[j]]))
    return groups


# ----------------------------------------------------------------------------
# STEP 3 — ask Claude to judge each candidate group
# ----------------------------------------------------------------------------
def ask_claude(groups, client):
    """
    Send candidate groups to Claude in small batches. For each group Claude
    returns: are these the same entity, the best canonical name, a confidence
    level, and one line of reasoning. Returns a list of decision dicts.
    """
    decisions = []
    BATCH = 15
    for start in range(0, len(groups), BATCH):
        batch = groups[start:start + BATCH]
        payload = [{"group_id": start + i, "names": g} for i, g in enumerate(batch)]
        prompt = (
            "You are cleaning a list of government vendor names. Some names below "
            "refer to the SAME real-world vendor entered inconsistently (punctuation, "
            "abbreviations, LLC vs Inc, typos, casing). For each group decide if ALL "
            "the names are the same vendor. Pick the single clearest, most complete "
            "canonical name. Be conservative: if a group mixes genuinely different "
            "vendors, set same_entity to false.\n\n"
            "Return ONLY a JSON array, one object per group, with keys: "
            "group_id (int), same_entity (bool), canonical_name (string), "
            "confidence ('high'|'medium'|'low'), reasoning (short string).\n\n"
            f"Groups:\n{json.dumps(payload, indent=2)}"
        )
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        # be forgiving about extra prose around the JSON
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            print("  ! could not parse a batch; skipping it", file=sys.stderr)
            continue
        for d in json.loads(match.group(0)):
            gid = d["group_id"]
            d["names"] = groups[gid]
            decisions.append(d)
    return decisions


def rule_only_decisions(groups):
    """Fallback for --no-llm: propose the longest name as canonical, but mark
    everything 'needs review' since no model judged it."""
    out = []
    for i, g in enumerate(groups):
        out.append({
            "group_id": i, "names": g, "same_entity": True,
            "canonical_name": max(g, key=len),
            "confidence": "low",
            "reasoning": "rule-based grouping only (no LLM); needs human review",
        })
    return out


# ----------------------------------------------------------------------------
# STEP 4 — apply decisions + write outputs
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip Claude; rule-based grouping only (no API key needed)")
    ap.add_argument("--eval", type=int, default=0,
                    help="also write a random sample of N applied merges to hand-check")
    args = ap.parse_args()

    # Load
    with open(args.csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if VENDOR_COL not in rows[0]:
        sys.exit(f"Column '{VENDOR_COL}' not found. Columns: {list(rows[0])}")
    print(f"Loaded {len(rows):,} rows")

    # Deterministic clean + gather unique names
    for r in rows:
        r[VENDOR_COL] = basic_clean(r[VENDOR_COL])
    names = sorted({r[VENDOR_COL] for r in rows if r[VENDOR_COL]})
    print(f"{len(names):,} unique vendor names after basic cleanup")

    # Group candidates
    groups = find_candidate_groups(names)
    print(f"{len(groups)} candidate duplicate groups found "
          f"(only these are sent to Claude)")

    # Judge
    if args.no_llm:
        decisions = rule_only_decisions(groups)
        print("Running in --no-llm mode: all merges flagged for human review.")
    else:
        try:
            from anthropic import Anthropic
        except ImportError:
            sys.exit("Run: pip install anthropic   (or use --no-llm)")
        client = Anthropic()  # reads ANTHROPIC_API_KEY from env
        print(f"Asking Claude ({MODEL}) to judge {len(groups)} groups...")
        decisions = ask_claude(groups, client)

    # Build name -> canonical map. Apply only confident, same-entity merges.
    canonical = {}
    log = []
    for d in decisions:
        same = d.get("same_entity", False)
        conf = d.get("confidence", "low")
        apply = bool(same) and conf in ("high", "medium")
        needs_review = (not apply)
        for n in d["names"]:
            if apply and n != d["canonical_name"]:
                canonical[n] = d["canonical_name"]
            log.append({
                "original_name": n,
                "canonical_name": d.get("canonical_name", ""),
                "same_entity": same,
                "confidence": conf,
                "applied": apply and n != d["canonical_name"],
                "needs_review": needs_review,
                "reasoning": d.get("reasoning", ""),
            })

    # Write cleaned data
    base = re.sub(r"\.csv$", "", args.csv_path)
    cleaned_path = base + "_cleaned.csv"
    fields = list(rows[0].keys()) + ["Vendor Name Canonical"]
    with open(cleaned_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            r["Vendor Name Canonical"] = canonical.get(r[VENDOR_COL], r[VENDOR_COL])
            w.writerow(r)

    # Write change log
    log_path = base + "_change_log.csv"
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        w.writeheader()
        w.writerows(log)

    applied = sum(1 for x in log if x["applied"])
    review = len({x["canonical_name"] for x in log if x["needs_review"]})
    print(f"\nDone.")
    print(f"  merges applied:        {applied}")
    print(f"  groups needing review: {review}")
    print(f"  -> {cleaned_path}")
    print(f"  -> {log_path}")

    # Optional eval sample
    if args.eval:
        import random
        applied_rows = [x for x in log if x["applied"]]
        sample = random.sample(applied_rows, min(args.eval, len(applied_rows)))
        eval_path = base + "_eval_sample.csv"
        with open(eval_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(sample[0].keys()) + ["correct? (y/n)"])
            w.writeheader()
            for s in sample:
                s["correct? (y/n)"] = ""
                w.writerow(s)
        print(f"  -> {eval_path}  (fill in the last column by hand, then count)")


if __name__ == "__main__":
    main()
