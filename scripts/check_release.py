#!/usr/bin/env python3
"""Release check: run before publishing this repository (or a branch of it) somewhere public.

What is scanned
  (default)          the tree of HEAD, exactly as committed (blobs read with git cat-file). Warns if the working tree
                     differs from HEAD, because then you are not looking at what you edited.
  --range <revs>     every commit in `git rev-list <revs>`: its full tree (git ls-tree -r -z + git cat-file) AND its
                     commit metadata. Use the exact range you will push, e.g. --range HEAD for a whole new history.
  --worktree         the working copy (tracked files and new files that aren't gitignored), for pre-commit use.

Content and file-name checks (every scanned file, its path included)
  * IDs: UUIDs, runs of 16 or more hex digits (32-hex IDs, 16-hex warehouse / workspace IDs, also next to '_'), and
    runs of 15 or more decimal digits. Only exact values listed for exactly that path in the allowlist pass.
  * any address in the databricks.com mail domain (any case).
  * binary blobs, symlinks and submodules are rejected unless the allowlist names the path.

Commit checks (--range)
  * author and committer e-mail must match --allow-email (default: a GitHub noreply address) and the name must be
    one of --allow-name (default: simtsc-db, repeatable);
  * no Co-authored-by or Signed-off-by line anywhere, no trailer at all (every trailer git interpret-trailers --parse
    finds, e.g. 'Author: X', 'Reviewer: X', is rejected unless its key is given with --allow-trailer KEY; default
    none), and no e-mail address anywhere in the message. Each message is read whole (git cat-file commit), never through
    a pipe that could stop early.

Allowlist: scripts/release_allowlist.txt AS COMMITTED in each scanned commit (missing = empty), so an uncommitted
or later exception never clears a finding; --worktree uses the working-copy file. --allowlist FILE overrides it
explicitly in every mode. Entries:
  id <path> <value>      the exact value is allowed in exactly that path
  binary <path>          the path may hold a binary blob
  symlink <path>         the path may be a symlink
Values listed in the allowlist file are allowed inside the allowlist file itself.

Exit codes: 0 clean, 1 findings, 2 usage or scanner error (also a failure).
Usage: scripts/check_release.py [--range <revs> | --worktree] [--allow-email ERE] [--allow-name NAME]...
                                [--allow-trailer KEY]... [--allowlist FILE] [--root DIR]
"""
import os
import re
import subprocess
import sys

DEFAULT_EMAIL = r"^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$"
DEFAULT_NAMES = ["simtsc-db"]
ALLOWLIST = "scripts/release_allowlist.txt"
_HEX = "0-9A-Fa-f"
ID_RES = [
    re.compile(rf"(?<![{_HEX}])[{_HEX}]{{8}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{12}}(?![{_HEX}])"),
    re.compile(rf"(?<![{_HEX}])[{_HEX}]{{16,}}(?![{_HEX}])"),
    re.compile(r"(?<![0-9])[0-9]{15,}(?![0-9])"),
]
CORP_MAIL = re.compile(r"@databricks\.com", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PERSON_LINE = re.compile(r"^\s*(co-authored-by|signed-off-by)\s*:", re.I | re.M)
IDENT = re.compile(rb"^(author|committer) (.*) <([^>]*)> -?\d+ [+-]\d{4}$")


class Error(Exception):
    pass


def git(root, *args, data=None) -> bytes:
    p = subprocess.run(["git", "-C", root, *args], input=data, capture_output=True)
    if p.returncode != 0:
        raise Error(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def parse_allowlist(text, label):
    """(ids {path: {values}}, special {(kind, path)}) from allowlist text; label names it in errors."""
    ids, special = {}, set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "id" and len(parts) == 3:
            ids.setdefault(parts[1], set()).add(parts[2])
        elif parts[0] in ("binary", "symlink") and len(parts) == 2:
            special.add((parts[0], parts[1]))
        else:
            raise Error(f"{label}:{n}: expected 'id <path> <value>', 'binary <path>' or 'symlink <path>'")
    ids.setdefault(ALLOWLIST, set()).update(v for vals in list(ids.values()) for v in vals)
    return ids, special


def read_allowlist_file(path):
    try:
        return parse_allowlist(open(path, encoding="utf-8").read(), path)
    except FileNotFoundError:
        raise Error(f"allowlist {path} not found") from None


def commit_allowlist(entries, blobs, rev):
    """The allowlist as committed in `rev` (missing = empty). Never the working copy: an uncommitted or later
    exception must not clear a finding in this commit."""
    for path, kind, sha in entries:
        if path == ALLOWLIST:
            if kind != "file":
                raise Error(f"{rev[:12]}:{ALLOWLIST} is not a regular file")
            try:
                return parse_allowlist(blobs[sha].decode("utf-8"), f"{rev[:12]}:{ALLOWLIST}")
            except UnicodeDecodeError:
                raise Error(f"{rev[:12]}:{ALLOWLIST} is not UTF-8 text") from None
    return {}, set()


def id_matches(text):
    """[(start, value)] for every ID-shaped value, overlapping matches merged (longest wins)."""
    spans = sorted({(m.start(), m.end()) for rx in ID_RES for m in rx.finditer(text)}, key=lambda s: (s[0], -s[1]))
    out, end = [], -1
    for s, e in spans:
        if s >= end:
            out.append((s, text[s:e])); end = e
    return out


def scan_text(path, text, allowed_ids):
    """Findings for one text: [(line, kind, value)]."""
    ok = allowed_ids.get(path, set())
    found = []
    for s, v in id_matches(text):
        if v not in ok:
            found.append((text.count("\n", 0, s) + 1, "NON-DUMMY ID", v))
    for m in CORP_MAIL.finditer(text):
        found.append((text.count("\n", 0, m.start()) + 1, "CORPORATE E-MAIL", m.group(0)))
    return found


def scan_entry(path, kind, data, allow):
    """kind: file / symlink / submodule. Returns [(line, kind, value)]; line 0 = the path or the whole file."""
    allowed_ids, special = allow
    found = [(0, f"{k} IN FILE NAME", v) for _, k, v in scan_text(path, path, allowed_ids)]
    if kind == "submodule":
        return found + [(0, "SUBMODULE", "not allowed")]
    if kind == "symlink" and ("symlink", path) not in special:
        return found + [(0, "SYMLINK", "add 'symlink <path>' to the allowlist if intended")]
    if kind == "symlink":
        return found
    try:
        if b"\0" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if ("binary", path) in special:
            return found
        return found + [(0, "BINARY", "add 'binary <path>' to the allowlist if intended")]
    return found + scan_text(path, text, allowed_ids)


def tree_entries(root, rev):
    """[(path, kind, sha)] of a commit's tree, NUL-delimited so any path survives unquoted."""
    out = []
    for rec in git(root, "ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, typ, sha = meta.split(b" ")
        kind = "submodule" if typ == b"commit" else "symlink" if mode == b"120000" else "file"
        out.append((path.decode("utf-8", errors="surrogateescape"), kind, sha.decode()))
    return out


def read_blobs(root, shas):
    """{sha: bytes} via one git cat-file --batch call."""
    shas = sorted(set(shas))
    if not shas:
        return {}
    raw = git(root, "cat-file", "--batch", data=("\n".join(shas) + "\n").encode())
    blobs, pos = {}, 0
    for sha in shas:
        nl = raw.index(b"\n", pos)
        head = raw[pos:nl].split(b" ")
        if len(head) != 3:
            raise Error(f"git cat-file: unexpected header {raw[pos:nl]!r}")
        size = int(head[2])
        blobs[sha] = raw[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def scan_tree(root, rev, override, cache):
    """Findings in commit `rev`'s tree, judged against the allowlist committed in that same commit (or `override`,
    an explicitly given --allowlist file)."""
    entries = tree_entries(root, rev)
    blobs = read_blobs(root, [sha for _, kind, sha in entries if kind == "file" and sha not in cache])
    cache.update(blobs)
    allow = override if override is not None else commit_allowlist(entries, cache, rev)
    found = []
    for path, kind, sha in entries:
        for line, k, v in scan_entry(path, kind, cache.get(sha, b""), allow):
            found.append((path, line, k, v))
    return found, len(entries)


def scan_worktree(root, allow):
    names = [n.decode("utf-8", errors="surrogateescape") for n in
             git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0") if n]
    found, n = [], 0
    for path in sorted(set(names)):
        full = os.path.join(root, path)
        if os.path.islink(full):
            kind, data = "symlink", b""
        elif os.path.isdir(full):
            kind, data = "submodule", b""
        elif os.path.isfile(full):
            kind, data = "file", open(full, "rb").read()
        else:
            continue                              # deleted in the working copy: not shared
        n += 1
        for line, k, v in scan_entry(path, kind, data, allow):
            found.append((path, line, k, v))
    return found, n


def parse_commit(raw: bytes):
    head, _, msg = raw.partition(b"\n\n")
    ids = {}
    for line in head.split(b"\n"):
        m = IDENT.match(line)
        if m:
            ids[m.group(1).decode()] = (m.group(2).decode(errors="replace"), m.group(3).decode(errors="replace"))
    return ids, msg.decode("utf-8", errors="replace")


def trailers(root, msg):
    out = git(root, "interpret-trailers", "--parse", data=msg.encode()).decode(errors="replace")
    return [t for t in out.splitlines() if t.strip()]


def check_commit(root, sha, email_re, names, permitted=()):
    ids, msg = parse_commit(git(root, "cat-file", "commit", sha))
    found = []
    for role in ("author", "committer"):
        if role not in ids:
            found.append(f"no {role} line")
            continue
        name, mail = ids[role]
        if not email_re.search(mail):
            found.append(f"{role} e-mail not allowed: {mail}")
        if name not in names:
            found.append(f"{role} name not allowed: {name}")
    for m in PERSON_LINE.finditer(msg):
        found.append(f"{m.group(1)} line in the message")
    allowed = {p.lower() for p in permitted}
    for t in trailers(root, msg):                    # every parsed trailer is rejected unless its key is permitted
        key = t.partition(":")[0].strip()
        if key.lower() not in allowed:
            found.append(f"trailer not permitted: {t.strip()[:120]}")
    for m in EMAIL.finditer(msg):
        found.append(f"e-mail address in the message: {m.group(0)}")
    return found


def main(argv) -> int:
    root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    rng, worktree, email, names, allowlist, permitted = None, False, DEFAULT_EMAIL, [], None, []
    it = iter(argv)
    try:
        for a in it:
            if a == "--range":
                rng = next(it)
            elif a == "--worktree":
                worktree = True
            elif a == "--allow-email":
                email = next(it)
            elif a == "--allow-name":
                names.append(next(it))
            elif a == "--allowlist":
                allowlist = next(it)
            elif a == "--allow-trailer":
                permitted.append(next(it))
            elif a == "--root":
                root = next(it)
            else:
                raise Error(f"unknown argument {a}")
    except StopIteration:
        print(f"check_release: {a} needs a value", file=sys.stderr)
        return 2
    except Error as e:
        print(f"check_release: {e}", file=sys.stderr)
        return 2
    if rng is not None and worktree:
        print("check_release: use either --range or --worktree, not both", file=sys.stderr)
        return 2
    if rng is not None and not rng.strip():
        print("check_release: --range is empty", file=sys.stderr)
        return 2
    names = names or DEFAULT_NAMES
    try:
        override = read_allowlist_file(allowlist) if allowlist is not None else None
        email_re = re.compile(email)
        findings = []
        if worktree:
            # pre-commit mode: the working-copy allowlist goes with the working-copy files
            wt = os.path.join(root, ALLOWLIST)
            found, n = scan_worktree(root, override if override is not None
                                     else read_allowlist_file(wt) if os.path.exists(wt) else ({}, set()))
            findings += [f"{k}: {p}{':' + str(ln) if ln else ''}: {v}" for p, ln, k, v in found]
            summary = f"working tree: {n} files"
        elif rng is not None:
            commits = git(root, "rev-list", rng, "--").decode().split()
            if not commits:
                raise Error(f"no commits in range {rng}")
            cache, seen = {}, {}
            for c in commits:
                found, _ = scan_tree(root, c, override, cache)
                for p, ln, k, v in found:
                    seen.setdefault((p, ln, k, v), c)        # report each finding once, at the newest commit
                for f in check_commit(root, c, email_re, names, permitted):
                    findings.append(f"COMMIT {c[:12]}: {f}")
            findings += [f"{k}: {c[:12]}:{p}{':' + str(ln) if ln else ''}: {v}" for (p, ln, k, v), c in seen.items()]
            summary = f"{len(commits)} commit(s) in {rng} (trees and metadata; e-mail /{email}/, names {names})"
        else:
            found, n = scan_tree(root, "HEAD", override, {})
            findings += [f"{k}: HEAD:{p}{':' + str(ln) if ln else ''}: {v}" for p, ln, k, v in found]
            summary = f"HEAD tree: {n} entries"
            if git(root, "status", "--porcelain", "-z"):
                print("check_release: WARNING: the working tree differs from HEAD; this checked HEAD's committed tree "
                      "(use --worktree for the working copy)", file=sys.stderr)
    except (Error, re.error, OSError, ValueError) as e:
        print(f"check_release: error: {e}", file=sys.stderr)
        return 2
    for f in findings[:300]:
        print(f)
    if findings:
        print(f"check_release: FAILED ({len(findings)} finding(s); {summary})")
        return 1
    print(f"check_release: clean ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
