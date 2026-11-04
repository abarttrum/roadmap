#!/usr/bin/env python3
"""
Roster validator: checks a patient roster CSV against a roster spec and writes
an exception report a customer can act on.

    python3 roster_validator.py synthetic_roster.csv
    python3 roster_validator.py synthetic_roster.csv -o sample_exception_report.txt

Python standard library only. Runs locally, sends nothing anywhere.
The report never prints names or birth dates, so it is safer to pass around
than the roster itself. It still lists member IDs, so treat it as sensitive.

New customer? Change the SPEC block. The checks stay the same.
Exit code: 0 = clean or warnings only, 1 = errors to fix, 2 = could not run.
"""
import argparse, csv, re, sys
from collections import Counter, defaultdict
from datetime import date, datetime

# ---------------- SPEC: the only part that changes per customer ----------------
SPEC = {
    "name": "Sample roster spec v1 (synthetic, not any real customer's)",
    "required": ["member_id", "last_name", "first_name", "dob", "sex", "program",
                 "attributed_npi", "attributed_tin", "attribution_start"],
    "optional": ["attribution_end"],
    "dates": ["dob", "attribution_start", "attribution_end"],
    "date_format": "%Y-%m-%d",
    "allowed": {"sex": {"F", "M", "U"}, "program": {"MSSP", "MA", "COMM"}},
    # Real NPIs start with 1 or 2. This sample also allows 9, a prefix no real
    # NPI uses, so the synthetic data cannot match a real provider. Drop the 9 for live use.
    "npi_first_digits": "129",
    # Who is in the contract: NPI -> (TIN, date they joined). From the customer's participant list.
    "participants": {
        "9000000015": ("900000011", "2026-01-01"),
        "9000000023": ("900000011", "2026-01-01"),
        "9000000031": ("900000022", "2026-01-01"),
        "9000000049": ("900000022", "2026-01-01"),
    },
}
# Plain-language rule list: code -> (severity, what it means, who fixes it)
RULES = {
    "REQ-1":  ("ERROR", "Required field is blank", "Customer data contact"),
    "NPI-1":  ("ERROR", "NPI is not 10 digits or has a bad first digit", "Customer data contact"),
    "NPI-2":  ("ERROR", "NPI fails the check-digit test (likely a typo)", "Customer data contact"),
    "DATE-1": ("ERROR", "Date is not a real date in the spec format", "Customer data contact"),
    "DATE-2": ("ERROR", "Birth date is in the future", "Customer data contact"),
    "DATE-3": ("ERROR", "Attribution ends before it starts", "Customer data contact"),
    "VAL-1":  ("ERROR", "Value is not one the spec allows", "Implementation lead + customer"),
    "TIN-1":  ("ERROR", "TIN is not 9 digits", "Customer data contact"),
    "DUP-1":  ("ERROR", "Same member, provider and start date listed twice", "Customer data contact"),
    "ATT-1":  ("ERROR", "Attributed NPI is not on the participant list", "Customer contract owner"),
    "ATT-2":  ("ERROR", "NPI is on the list, but under a different TIN", "Customer contract owner"),
    "ATT-3":  ("ERROR", "Member attributed to two providers at the same time", "Customer contract owner"),
    "ATT-4":  ("WARN",  "Attribution starts before the provider joined", "Customer contract owner"),
    "DUP-2":  ("WARN",  "Same name and birth date under two member IDs", "Customer data contact"),
}
# -------------------------------------------------------------------------------


def npi_check_digit_ok(npi):
    """Luhn check with the 80840 health-industry prefix, as NPIs are defined."""
    total = 0
    for i, ch in enumerate(reversed("80840" + npi)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def parse(value):
    try:
        return datetime.strptime(value, SPEC["date_format"]).date()
    except ValueError:
        return None


def check_row(row, line, today):
    found = []

    def add(rule, field, detail=""):
        found.append((line, row.get("member_id") or "(blank)", rule, field, detail))

    for field in SPEC["required"]:
        if not row.get(field):
            add("REQ-1", field, "blank")

    for field in SPEC["dates"]:
        if row.get(field) and parse(row[field]) is None:
            add("DATE-1", field, f"should look like {date(2026, 1, 31).strftime(SPEC['date_format'])}")  # value stays hidden

    dob, start, end = (parse(row.get(f) or "") for f in ("dob", "attribution_start", "attribution_end"))
    if dob and dob > today:
        add("DATE-2", "dob", "date hidden")
    if start and end and end < start:
        add("DATE-3", "attribution_end", f"{row['attribution_start']} to {row['attribution_end']}")

    for field, allowed in SPEC["allowed"].items():
        if row.get(field) and row[field] not in allowed:
            add("VAL-1", field, f"got {row[field]!r}")

    tin = row.get("attributed_tin", "")
    if tin and not re.fullmatch(r"\d{9}", tin):
        add("TIN-1", "attributed_tin", f"got {tin!r}")

    npi = row.get("attributed_npi", "")
    if npi:
        if not re.fullmatch(r"\d{10}", npi) or npi[0] not in SPEC["npi_first_digits"]:
            add("NPI-1", "attributed_npi", f"got {npi!r}")
        elif not npi_check_digit_ok(npi):
            add("NPI-2", "attributed_npi", f"got {npi!r}")
        elif npi not in SPEC["participants"]:
            add("ATT-1", "attributed_npi", f"got {npi!r}")
        else:
            list_tin, joined = SPEC["participants"][npi]
            if re.fullmatch(r"\d{9}", tin) and tin != list_tin:
                add("ATT-2", "attributed_tin", f"file {tin}, list {list_tin}")
            if start and start < parse(joined):
                add("ATT-4", "attribution_start", f"starts {row['attribution_start']}, joined {joined}")
    return found


def check_across_rows(rows):
    """Checks that need more than one row. rows = list of (line, row)."""
    found = []
    by_member, by_person = defaultdict(list), defaultdict(set)
    for line, row in rows:
        if row.get("member_id"):
            by_member[row["member_id"]].append((line, row))
        person = (row.get("last_name"), row.get("first_name"), row.get("dob"))
        if all(person) and row.get("member_id"):
            by_person[person].add(row["member_id"])

    for member, entries in by_member.items():
        seen = set()
        for i, (line, row) in enumerate(entries):
            key = (row.get("attributed_npi"), row.get("attribution_start"))
            if key in seen:
                found.append((line, member, "DUP-1", "member_id", f"copy of line {entries[0][0]}"))
                continue
            seen.add(key)
            for other_line, other in entries[:i]:
                if other.get("attributed_npi") == row.get("attributed_npi"):
                    continue
                a0, b0 = parse(other.get("attribution_start") or ""), parse(row.get("attribution_start") or "")
                a1 = parse(other.get("attribution_end") or "") or date.max
                b1 = parse(row.get("attribution_end") or "") or date.max
                if a0 and b0 and a0 <= b1 and b0 <= a1:
                    found.append((line, member, "ATT-3", "attributed_npi", f"overlaps line {other_line}"))

    for ids in by_person.values():
        if len(ids) > 1:
            lines = [ln for ln, r in rows if r.get("member_id") in ids]
            first, *others = sorted(ids)
            found.append((min(lines), first, "DUP-2", "member_id", f"also {', '.join(others)}, line {max(lines)}"))
    return found


def build_report(path, rows, exceptions, today):
    errors = [e for e in exceptions if RULES[e[2]][0] == "ERROR"]
    warns = [e for e in exceptions if RULES[e[2]][0] == "WARN"]
    bad_lines = {e[0] for e in errors}
    verdict = "HOLD: fix errors and resend" if errors else "PASS: ready to load"
    out = ["ROSTER EXCEPTION REPORT", "=" * 72,
           f"File:      {path}", f"Spec:      {SPEC['name']}", f"Run date:  {today}",
           "Privacy:   names and birth dates are never printed in this report", "",
           f"Rows read:             {len(rows)}",
           f"Rows ready to load:    {len(rows) - len(bad_lines)}",
           f"Rows held for errors:  {len(bad_lines)}",
           f"Errors / warnings:     {len(errors)} / {len(warns)}",
           f"Verdict:               {verdict}", "", "SUMMARY BY RULE", "-" * 72,
           f"{'Rule':<7}{'Level':<7}{'Count':<7}{'What it means':<52}"]
    counts = Counter(e[2] for e in exceptions)
    for code in sorted(counts, key=lambda c: (RULES[c][0], -counts[c], c)):
        sev, meaning, _ = RULES[code]
        out.append(f"{code:<7}{sev:<7}{counts[code]:<7}{meaning}")
    out += ["", "WHO NEEDS TO ACT", "-" * 72]
    owners = Counter(RULES[e[2]][2] for e in exceptions)
    out += [f"{n:>3}  {owner}" for owner, n in owners.most_common()]
    out += ["", "DETAIL", "-" * 72, f"{'Line':<6}{'Member':<12}{'Rule':<8}{'Field':<19}Detail"]
    for line, member, rule, field, detail in sorted(exceptions):
        out.append(f"{line:<6}{member:<12}{rule:<8}{field:<19}{detail}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Validate a roster CSV against the spec.")
    ap.add_argument("csv_file")
    ap.add_argument("-o", "--output", help="also save the report to this file")
    args = ap.parse_args()
    try:
        with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            missing = [c for c in SPEC["required"] if c not in (reader.fieldnames or [])]
            if missing:
                print(f"Cannot check this file. Missing columns: {', '.join(missing)}")
                return 2
            rows = [(reader.line_num, {k: (v or "").strip() for k, v in r.items() if k}) for r in reader]
    except (OSError, UnicodeDecodeError) as err:
        print(f"Cannot read {args.csv_file}: {err}")
        return 2

    today = date.today()
    exceptions = [e for line, row in rows for e in check_row(row, line, today)]
    exceptions += check_across_rows(rows)
    report = build_report(args.csv_file, rows, exceptions, today)
    print(report, end="")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
    return 1 if any(RULES[e[2]][0] == "ERROR" for e in exceptions) else 0


if __name__ == "__main__":
    sys.exit(main())
