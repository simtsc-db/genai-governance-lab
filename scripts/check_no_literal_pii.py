#!/usr/bin/env python3
"""Fail if a shared file contains literal PII-like values.

Pattern and denylist coverage (it can't recognise every personal value; it stops the known shapes and names):
  * NRIC/FIN-shaped strings      [STFGM] + 7 digits + letter, any letter case
  * Singapore phone numbers      [35689] + 3 digits + 4 digits (allocated levels 3/6/8/9, plus the reserved level 5
                                 the lab's fixtures use), with optional space / dot / hyphen between the groups and
                                 an optional 65 / +65 / (+65) prefix
  * realistic name tokens        scripts/pii_name_tokens.txt (+ untracked local/pii_name_tokens.txt): whole words,
                                 case-insensitive, multi-word tokens also when wrapped across lines
in every tracked file and new file that isn't gitignored (default), or in the STAGED content (--staged).

Synthetic values in this lab are generated at runtime (checks.fixture_row, synthetic_sentence), so no literal is
needed anywhere. The only exception: lines in src/lib/test_checks.py marked '# pii-fixture' (negative-test fixtures).
Those must themselves be fictional: NRIC-shaped values start with S or T and use an invalid check letter, phone numbers
start with 5 (a reserved level, can't be dialled in Singapore), and no name token may appear.

Usage: scripts/check_no_literal_pii.py [--staged] [--names FILE]      exit 0 clean, 1 found, 2 scanner error
"""
import os
import re
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SELF = "scripts/check_no_literal_pii.py"
NAMES_FILE = "scripts/pii_name_tokens.txt"
LOCAL_NAMES = "local/pii_name_tokens.txt"
FIXTURE_FILE = "src/lib/test_checks.py"
FIXTURE_MARK = "# pii-fixture"
NRIC = re.compile(r"(?<![A-Za-z0-9])[STFGM][0-9]{7}[A-Z](?![A-Za-z0-9])", re.I)
PHONE = re.compile(r"(?<![0-9A-Za-z+.])(?<![0-9]-)(?:(?:\(\+?65\)|\+?65)[ .-]?)?[35689][0-9]{3}[ .-]?[0-9]{4}(?![0-9A-Za-z]|-[0-9])")
# A matched phone number split into its optional country code and exactly eight national digits.
PHONE_PARTS = re.compile(r"(?:(\(\+?65\)|\+?65)[ .-]?)?([35689][0-9]{3})[ .-]?([0-9]{4})")
INVALID_ST_CHECK = set("KLMNPQRUVWY")     # S/T check letters are A-J and Z


def load_tokens(paths) -> list:
    out = []
    for p in paths:
        if p and os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                t = line.strip()
                if t and not t.startswith("#"):
                    out.append(t)
    return out


def token_re(tokens):
    if not tokens:
        return None
    alt = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in sorted(set(tokens), key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z])(?:{alt})(?![A-Za-z])", re.I)


def fictional_fixture(kind: str, value: str) -> bool:
    if kind == "nric":
        return value[0] in "ST" and value[-1] in INVALID_ST_CHECK      # upper case only, invalid check letter
    if kind == "phone":
        # The country code is stripped only when present (65 / +65 / (+65) followed by exactly 8 national digits), so a
        # bare 655xxxxx is the level-6 number 655x xxxx, never a level-5 fixture.
        m = PHONE_PARTS.fullmatch(value)
        national = (m.group(2) + m.group(3)) if m else ""
        return len(national) == 8 and national.startswith("5")
    return False


def scan(files: dict, tokens: list) -> list:
    """files: {path: text}. Returns [(path, line_no, kind, value)] for every finding. The whole text is scanned, so a
    multi-word name wrapped across lines is found too."""
    names = token_re(tokens)
    hits = []
    for path, text in files.items():
        lines = text.splitlines()
        starts = [0]
        for line in text.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))

        def line_of(pos):
            lo, hi = 0, len(starts) - 1
            while lo < hi - 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if starts[mid] <= pos else (lo, mid)
            return lo + 1
        for kind, rx in (("nric", NRIC), ("phone", PHONE), ("name", names)):
            if rx is None:
                continue
            for m in rx.finditer(text):
                n = line_of(m.start())
                line = lines[n - 1] if n - 1 < len(lines) else ""
                fixture = path == FIXTURE_FILE and FIXTURE_MARK in line and "\n" not in m.group(0)
                if kind != "name" and fixture and fictional_fixture(kind, m.group(0)):
                    continue
                hits.append((path, n, kind, " ".join(m.group(0).split())))
    return hits


def shared_files() -> list:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    return [f for f in out if f not in (SELF, NAMES_FILE) and os.path.isfile(os.path.join(ROOT, f))]


def staged_files() -> dict:
    """{path: staged content} for files added / copied / modified / renamed in the index."""
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split("\0")
    out = {}
    for f in (n for n in names if n and n not in (SELF, NAMES_FILE)):
        blob = subprocess.run(["git", "show", f":{f}"], cwd=ROOT, capture_output=True, check=True).stdout
        try:
            out[f] = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue                          # binary file
    return out


def main(argv) -> int:
    names_file = os.path.join(ROOT, NAMES_FILE)
    if "--names" in argv:
        names_file = argv[argv.index("--names") + 1]
    try:
        tokens = load_tokens([names_file, os.path.join(ROOT, LOCAL_NAMES)])
        if not tokens:
            print(f"check_no_literal_pii: no name tokens loaded from {names_file}", file=sys.stderr)
            return 2
        if "--staged" in argv:
            files = staged_files()
        else:
            files = {}
            for f in shared_files():
                try:
                    files[f] = open(os.path.join(ROOT, f), encoding="utf-8").read()
                except UnicodeDecodeError:
                    continue                  # binary file
    except Exception as e:  # noqa: BLE001
        print(f"check_no_literal_pii: scanner error: {e}", file=sys.stderr)
        return 2
    hits = scan(files, tokens)
    for path, n, kind, value in hits[:200]:
        print(f"LITERAL {kind}: {path}:{n}: {value!r}")
    if hits:
        print(f"check_no_literal_pii: FAILED ({len(hits)} finding(s) in {len(files)} files)")
        return 1
    print(f"check_no_literal_pii: clean ({len(files)} {'staged ' if '--staged' in argv else ''}files, "
          f"{len(tokens)} name tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
