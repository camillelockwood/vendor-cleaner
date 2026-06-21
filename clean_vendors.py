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

Reliability choices:
  - Claude is called with temperature=0 so runs are reproducible.
  - Claude's chosen name is validated against the group (no invented names).
  - The API call retries on transient errors.
  - Overlapping candidate groups are merged so no vendor is judged twice.

Outputs
-------
  1. <input>_cleaned.csv      original data + a new "Vendor Name Canonical" column
  2. <input>_change_log.csv   every proposed merge, with Claude's reasoning,
                              confidence, total spend for that name, and whether
                              it was applied or needs review

Run it
------
  # See it work with NO API key (rule-based grouping only):
  python clean_vendors.py checkbook_explorer_fy25_updated.csv --no-llm

  # Full version (needs an Anthropic API key). Add --eval to also write an
  # accuracy sample from THIS SAME run:
  export ANTHROPIC_API_KEY=sk-...
  python clean_vendors.py checkbook_explorer_fy25_updated.csv --eval 50
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher

VENDOR_COL = "Vendor Name"          # the messy column we clean
AMOUNT_COL = "Monetary Amount"      # summed per vendor to show the $ impact of duplicates
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


def parse_amount(raw: str) -> float:
    """Turn a money string like '$1,234.56' into a float; 0.0 if unparseable."""
    try:
        return float(re.sub(r"[^0-9.\-]", "", raw or ""))
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------------
# STEP 2 — group candidate duplicates
# ----------------------------------------------------------------------------
def merge_overlapping(groups):
    """
    Merge any groups that share a name so each vendor is judged exactly once.
    Uses a small union-find over the names. Returns deduped groups (2+ names).
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for g in groups:
        for n in g[1:]:
            union(g[0], n)

    clusters = defaultdict(set)
    for g in groups:
        for n in g:
            clusters[find(n)].add(n)
    return [sorted(v) for v in clusters.values() if len(v) > 1]


def find_candidate_groups(names):
    """
    Return groups (each a list of 2+ raw names) that MIGHT be the same vendor.
    Two passes: (a) shared blocking key, (b) high string similarity within a
    tight prefix block, to catch typos the key misses. Overlapping groups are
    then merged so no name appears in more than one group.
    """
    by_key = defaultdict(set)
    for n in names:
        by_key[blocking_key(n)].add(n)
    groups = [sorted(v) for v in by_key.values() if len(v) > 1]

    seen = {n for g in groups for n in g}
    by_prefix = defaultdict(list)
    for n in names:
        if n not in seen and n:
            by_prefix[blocking_key(n)[:4]].append(n)  # tight prefix block = fast
    for bucket in by_prefix.values():
        if len(bucket) > 60:   # skip pathological buckets to stay quick
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                if similar(blocking_key(bucket[i]), blocking_key(bucket[j])) >= 0.92:
                    groups.append(sorted([bucket[i], bucket[j]]))

    return merge_overlapping(groups)


# ----------------------------------------------------------------------------
# STEP 3 — ask Claude to judge each candidate group
# ----------------------------------------------------------------------------
def _call_claude(client, prompt, retries=3):
    """Call the API with simple retry/backoff. Returns text, or None on failure."""
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                temperature=0,            # deterministic, reproducible runs
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception as e:            # noqa: BLE001 - any transient API error
            if attempt == retries - 1:
                print(f"  ! batch failed after {retries} tries: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


def ask_claude(groups, client):
    """
    Send candidate groups to Claude in small batches. For each group Claude
    returns: same_entity, the best canonical name, a confidence level, and a
    short reason. Returns a list of decision dicts.
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
            "the names are the same vendor. For canonical_name, choose EXACTLY ONE of "
            "the names shown in that group (do not invent a new name). Be conservative: "
            "if a group mixes genuinely different vendors, set same_entity to false.\n\n"
            "Return ONLY a JSON array, one object per group, with keys: "
            "group_id (int), same_entity (bool), canonical_name (string), "
            "confidence ('high'|'medium'|'low'), reasoning (short string).\n\n"
            f"Groups:\n{json.dumps(payload, indent=2)}"
        )
        text = _call_claude(client, prompt)
        if text is None:
            continue
        match = re.search(r"\[.*\]", text, re.DOTALL)  # forgive prose around the JSON
        if not match:
            print("  ! could not parse a batch; skipping it", file=sys.stderr)
            continue
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            print("  ! invalid JSON in a batch; skipping it", file=sys.stderr)
            continue
        for d in parsed:
            gid = d.get("group_id")
            if not isinstance(gid, int) or not (0 <= gid < len(groups)):
                continue
            d["names"] = groups[gid]
            decisions.append(d)
    return decisions


def rule_only_decisions(groups):
    """Fallback for --no-llm: propose the longest name as canonical, but mark
    everything 'needs review' since no model judged it."""
    return [{
        "group_id": i, "names": g, "same_entity": True,
        "canonical_name": max(g, key=len),
        "confidence": "low",
        "reasoning": "rule-based grouping only (no LLM); needs human review",
    } for i, g in enumerate(groups)]


# ----------------------------------------------------------------------------
# STEP 4 — apply decisions + write outputs
# ----------------------------------------------------------------------------
def _group_is_objectively_similar(names: list) -> bool:
    """
    Secondary gate: confirm every pair in the group clears a string-distance bar,
    independent of what the model says. Prevents auto-applying when Claude is
    overconfident on names that are actually quite different.
    """
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            key_sim = similar(blocking_key(names[i]), blocking_key(names[j]))
            raw_sim = similar(names[i].lower(), names[j].lower())
            if key_sim < 0.70 and raw_sim < 0.60:
                return False
    return True


def decide(d):
    """
    Turn one raw model decision into (canonical_name, apply_group).
    Two-signal gate: the model must say same_entity + high/medium confidence,
    AND the names must pass an objective string-distance check. Either signal
    alone can veto auto-apply; flagged decisions still appear in the change log.
    """
    group_names = d["names"]
    same = bool(d.get("same_entity", False))
    conf = d.get("confidence", "low")
    cname = (d.get("canonical_name") or "").strip()
    valid = cname in group_names
    if not valid:
        cname = max(group_names, key=len)        # safe fallback; no invented names
    obj_similar = _group_is_objectively_similar(group_names)
    apply_group = same and valid and conf in ("high", "medium") and obj_similar
    return cname, apply_group


def measure_recall(known_pairs_path: str, candidate_groups: list) -> None:
    """
    Report what fraction of known duplicate pairs the grouping step surfaced.
    Pairs not found were never sent to Claude — a gap no confidence gate can fix.

    known_pairs_path: CSV with columns name_a, name_b (one known-duplicate pair per row).
    """
    try:
        with open(known_pairs_path, encoding="utf-8-sig") as f:
            pairs = [(r["name_a"].strip(), r["name_b"].strip()) for r in csv.DictReader(f)]
    except FileNotFoundError:
        print(f"  ! recall file not found: {known_pairs_path}", file=sys.stderr)
        return

    found, missed = [], []
    for a, b in pairs:
        surfaced = any(a in g and b in g for g in candidate_groups)
        (found if surfaced else missed).append((a, b))

    pct = 100 * len(found) / len(pairs) if pairs else 0
    print(f"\nRecall check ({known_pairs_path}):")
    print(f"  Known duplicate pairs : {len(pairs)}")
    print(f"  Surfaced by grouping  : {len(found)} ({pct:.0f}%)")
    if missed:
        print(f"  Missed ({len(missed)}):")
        for a, b in missed:
            print(f"    '{a}'  vs  '{b}'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip Claude; rule-based grouping only (no API key needed)")
    ap.add_argument("--eval", type=int, default=0,
                    help="also write a random sample of N applied merges to hand-check")
    ap.add_argument("--recall", metavar="PAIRS_CSV", default="",
                    help="CSV of known duplicate pairs (columns: name_a, name_b) to "
                         "measure grouping recall against")
    args = ap.parse_args()

    # Load
    with open(args.csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("The CSV appears to be empty.")
    if VENDOR_COL not in rows[0]:
        sys.exit(f"Column '{VENDOR_COL}' not found. Columns: {list(rows[0])}")
    print(f"Loaded {len(rows):,} rows")

    # Deterministic clean + total spend per name (this is where AMOUNT_COL earns its keep)
    spend_by_name = defaultdict(float)
    for r in rows:
        r[VENDOR_COL] = basic_clean(r[VENDOR_COL])
        spend_by_name[r[VENDOR_COL]] += parse_amount(r.get(AMOUNT_COL, ""))
    names = sorted({r[VENDOR_COL] for r in rows if r[VENDOR_COL]})
    print(f"{len(names):,} unique vendor names after basic cleanup")

    # Group candidates
    groups = find_candidate_groups(names)
    print(f"{len(groups)} candidate duplicate groups found "
          f"(only these are sent to Claude)")

    # Optional recall measurement (checks the grouping step, not the model)
    if args.recall:
        measure_recall(args.recall, groups)

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
    canonical, log = {}, []
    for d in decisions:
        cname, apply_group = decide(d)
        for n in d["names"]:
            applied = apply_group and n != cname
            if applied:
                canonical[n] = cname
            log.append({
                "original_name": n,
                "canonical_name": cname,
                "same_entity": bool(d.get("same_entity", False)),
                "confidence": d.get("confidence", "low"),
                "vendor_total_spend": round(spend_by_name.get(n, 0.0), 2),
                "applied": applied,
                "needs_review": not apply_group,
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
    review = sum(1 for d in decisions if not decide(d)[1])
    print(f"\nDone.")
    print(f"  merges applied:        {applied}")
    print(f"  groups needing review: {review}")
    print(f"  -> {cleaned_path}")
    print(f"  -> {log_path}")

    # Optional eval sample (from THIS run; seeded so it's reproducible)
    if args.eval:
        random.seed(0)
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
