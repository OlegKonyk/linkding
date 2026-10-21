#!/usr/bin/env python3
"""sdlc-ci: the in-repo suites and the honesty checks that need no second stack.

The same file runs in two places: in the fork's GitHub Actions job (committed by Ship as
`.github/sdlc/sdlc_ci.py`) and on this machine through `sdlc ci <run>` in the head checkout. Both
read the same expectations file (`.github/sdlc/expect.json`, written by Ship from the run), so the
commands are identical.

It runs the setup commands, collects each suite at the head and at the base commit, runs each
suite with a JUnit report, and computes:

  suite_passes         every suite exits 0, with no failure or error, and ran at least one test
  suite_ran            per suite: every test collected at base is still collected at head, every
                       declared new test is collected, every collected test ran, and nothing was
                       skipped except the declared known skips (a new test never may be)
  protected_untouched  the diff from base edits, deletes or renames nothing under the protected
                       globs, and adds no skip or xfail there (new files are allowed)

It writes `<out-dir>/sdlc-ci-result.json` ({version, source, head_sha, base_sha, ticket, run,
checks, counts, started, finished, commands, conclusion}) and the JUnit files beside it, and in
GitHub Actions the job summary. Exit 0 when every check passes, 1 otherwise, 2 on bad input.
Standard library only, because it runs before the application's environment exists.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

RESULT = "sdlc-ci-result.json"
CHECKS = ("suite_passes", "suite_ran", "protected_untouched")
SETUP_TIMEOUT_S = 1200
SUITE_TIMEOUT_S = 1800
# What adding a skip or an xfail looks like in pytest and unittest code.
SKIP_RE = re.compile(
    r"pytest\.mark\.(skip|skipif|xfail)\b|pytest\.(skip|xfail|importorskip)\(|"
    r"unittest\.(skip|skipIf|skipUnless|expectedFailure)\b|@(skip|skipIf|skipUnless|expectedFailure)\b|\.skipTest\("
)
VERBS = {"M": "edits", "D": "deletes", "R": "renames", "C": "copies", "T": "changes the type of", "U": "leaves unmerged"}


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def say(msg: str) -> None:
    print(f"sdlc-ci: {msg}", flush=True)


# Running commands -------------------------------------------------------------------------------


def app_env(tree: Path) -> dict[str, str]:
    """The environment for the application's commands: its own `.venv` first on PATH, nothing of
    the caller's virtual environment left over (`sdlc ci` itself runs inside the pipeline's)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX")}
    venv = tree / ".venv"
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join([str(venv / "bin"), env.get("PATH", "")])
    env.setdefault("CI", "true")
    return env


def run(args: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str, float]:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)
        rc, out = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or b""
        rc, out = -1, (raw.decode(errors="replace") if isinstance(raw, bytes) else raw) + f"\ntimed out after {timeout}s"
    except FileNotFoundError as exc:
        rc, out = 127, str(exc)
    return rc, out, round(time.monotonic() - t0, 2)


def git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def tail(out: str, n: int = 15) -> list[str]:
    return out.rstrip().splitlines()[-n:]


# Pytest ids -------------------------------------------------------------------------------------


def strip_workers(args: list[str]) -> list[str]:
    """The command without pytest-xdist's worker count, for a plain collection."""
    out, skip_next = [], False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in ("-n", "--numprocesses"):
            skip_next = True
            continue
        if a.startswith("--numprocesses=") or re.fullmatch(r"-n(\d+|auto|logical)", a):
            continue
        out.append(a)
    return out


def collected_ids(out: str) -> list[str]:
    """Node ids from `pytest --collect-only -q`: one `path.py::...` per line."""
    return [ln.strip() for ln in out.splitlines() if re.match(r"^[^\s].*\.py::", ln)]


def junit_key(nodeid: str) -> str:
    """A pytest node id as its JUnit `classname::name`, by pytest's own mangling (junitxml.mangle_test_address)."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]) + "::" + names[-1]


RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


def parse_junit(path: Path) -> dict[str, str]:
    """`classname::name` -> passed | skipped | failed | error (the worst, when a test has two entries)."""
    cases: dict[str, str] = {}
    for tc in ET.parse(path).getroot().iter("testcase"):
        key = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        if tc.find("error") is not None:
            outcome = "error"
        elif tc.find("failure") is not None:
            outcome = "failed"
        elif tc.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        if RANK[outcome] >= RANK.get(cases.get(key, "passed"), 0):
            cases[key] = outcome
    return cases


# The protected-files check ----------------------------------------------------------------------


def matches(path: str, globs: list[str]) -> bool:
    """A glob match where `**` spans folders (PurePath.full_match, Python 3.13), or fnmatch's `*` that does too."""
    full = getattr(PurePosixPath(path), "full_match", None)
    return any(fnmatch.fnmatchcase(path, g) or (full is not None and full(g)) for g in globs)


def protected_untouched(tree: Path, base: str, globs: list[str]) -> dict:
    if not globs:
        return {"status": "pass", "detail": "no protected globs declared"}
    changed = git(["diff", "--name-status", "-M", "--no-color", base, "HEAD"], tree)
    violations, added = [], []
    for line in changed.splitlines():
        status, *paths = line.split("\t")
        if not any(matches(p, globs) for p in paths):
            continue
        if status.startswith("A"):
            added.append(paths[-1])
            diff = git(["diff", "--no-color", "-U0", base, "HEAD", "--", paths[-1]], tree)
            for dl in diff.splitlines():
                if dl.startswith("+") and not dl.startswith("+++") and SKIP_RE.search(dl):
                    violations.append(f"{paths[-1]} adds a skip or xfail: {dl[1:].strip()[:120]}")
        else:
            violations.append(f"{' -> '.join(paths)}: {VERBS.get(status[0], status)} a protected file")
    if violations:
        return {"status": "fail", "detail": "; ".join(violations), "violations": violations, "added": added}
    return {"status": "pass", "detail": f"{len(added)} new file(s) under protected paths, none edited, deleted or skipped",
            "added": added}


# The suites -------------------------------------------------------------------------------------


def suite_ran(name: str, base_ids: list[str], head_ids: list[str], outcomes: dict[str, str], declared_new: set[str],
              known_skips: set[str]) -> tuple[list[str], dict]:
    """Problems with one suite's run, and its counts."""
    head_keys = {junit_key(i) for i in head_ids}
    base_keys = {junit_key(i) for i in base_ids}
    new_keys = {junit_key(i) for i in declared_new if junit_key(i) in head_keys}
    declared_here = {junit_key(i) for i in declared_new}
    skipped = {k for k, v in outcomes.items() if v == "skipped"}
    known = {junit_key(i) for i in known_skips}
    problems = []
    lost = sorted(base_keys - head_keys)
    if lost:
        problems.append(f"{name}: {len(lost)} test(s) collected at base are gone at head: {', '.join(lost[:5])}")
    not_ran = sorted(head_keys - set(outcomes))
    if not_ran:
        problems.append(f"{name}: {len(not_ran)} collected test(s) did not run: {', '.join(not_ran[:5])}")
    bad_skips = sorted((skipped - known) | (skipped & declared_here))
    if bad_skips:
        problems.append(f"{name}: {len(bad_skips)} unexpected skip(s): {', '.join(bad_skips[:5])}")
    extra = sorted(head_keys - base_keys - declared_here)
    counts = {
        "base": len(base_keys), "collected": len(head_keys), "expected": len(base_keys | new_keys),
        "new": len(head_keys - base_keys), "declared_new": len(new_keys), "undeclared_new": len(extra),
        "ran": len(outcomes), "passed": sum(v == "passed" for v in outcomes.values()),
        "failed": sum(v == "failed" for v in outcomes.values()), "errors": sum(v == "error" for v in outcomes.values()),
        "skipped": len(skipped), "known_skips_hit": len(skipped & known),
    }
    return problems, counts


def run_suites(tree: Path, expect: dict, out_dir: Path, env: dict[str, str]) -> tuple[dict, dict, list[str]]:
    """Collect at base and head, run each suite; returns (counts, per-suite raw facts, problems)."""
    suites: dict[str, list[str]] = expect["suites"]
    declared_new = set(expect.get("new_tests", []))
    known = {s["id"] if isinstance(s, dict) else s for s in expect.get("known_skips", [])}
    counts, facts, problems = {}, {}, []
    with tempfile.TemporaryDirectory(prefix="sdlc-ci-base-") as tmp:
        base_tree = Path(tmp) / "base"
        git(["worktree", "add", "--detach", "--quiet", str(base_tree), expect["base_sha"]], tree)
        try:
            for d in expect.get("dirs", []):
                (base_tree / d).mkdir(parents=True, exist_ok=True)
            for name, cmd in suites.items():
                say(f"collect {name} at base and head")
                rc_b, out_b, _ = run([*strip_workers(cmd), "--collect-only", "-q"], base_tree, env, SUITE_TIMEOUT_S)
                rc_h, out_h, _ = run([*strip_workers(cmd), "--collect-only", "-q"], tree, env, SUITE_TIMEOUT_S)
                base_ids, head_ids = collected_ids(out_b), collected_ids(out_h)
                if rc_b != 0:
                    problems.append(f"{name}: collection at base exited {rc_b}: {' | '.join(tail(out_b, 3))}")
                if rc_h != 0:
                    problems.append(f"{name}: collection at head exited {rc_h}: {' | '.join(tail(out_h, 3))}")
                junit = out_dir / f"junit-{name}.xml"
                say(f"run {name}: {' '.join(cmd)}")
                rc, out, secs = run([*cmd, f"--junitxml={junit}"], tree, env, SUITE_TIMEOUT_S)
                say(f"{name} exited {rc} in {secs}s: {tail(out, 1)[0] if out.strip() else ''}")
                outcomes = parse_junit(junit) if junit.exists() else {}
                p, c = suite_ran(name, base_ids, head_ids, outcomes, declared_new, known)
                problems += p
                c.update(returncode=rc, seconds=secs)
                counts[name] = c
                facts[name] = {"returncode": rc, "tail": tail(out), "outcomes": outcomes}
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(base_tree)], cwd=tree, capture_output=True)
    collected_all = set().union(*(set(f["outcomes"]) for f in facts.values())) if facts else set()
    missing = sorted(i for i in declared_new if junit_key(i) not in collected_all)
    if missing:
        problems.append(f"{len(missing)} declared new test(s) never ran: {', '.join(missing[:5])}")
    return counts, facts, problems


def evaluate(counts: dict, facts: dict, ran_problems: list[str], protected: dict) -> dict:
    bad = [f"{n}: exit {f['returncode']}, {counts[n]['failed']} failed, {counts[n]['errors']} errors, {counts[n]['ran']} ran"
           for n, f in facts.items()
           if f["returncode"] != 0 or counts[n]["failed"] or counts[n]["errors"] or not counts[n]["ran"]]
    summary = "; ".join(f"{n}: {c['passed']} passed, {c['skipped']} skipped of {c['collected']} collected"
                        for n, c in counts.items())
    return {
        "suite_passes": {"status": "fail", "detail": "; ".join(bad)} if bad or not facts
        else {"status": "pass", "detail": summary},
        "suite_ran": {"status": "fail", "detail": "; ".join(ran_problems)} if ran_problems or not facts
        else {"status": "pass", "detail": "every base test still collected, every collected test ran, "
                                           "only the known skips skipped; " + summary},
        "protected_untouched": protected,
    }


# The summary for a person -----------------------------------------------------------------------


def markdown(result: dict) -> str:
    lines = [f"## sdlc-ci: {result['conclusion']}", "",
             f"Head `{result['head_sha']}`, base `{result['base_sha']}`, ticket `{result.get('ticket', '')}`.", "",
             "| Check | Status | Detail |", "|---|---|---|"]
    for name, c in result["checks"].items():
        lines.append(f"| {name} | {c['status']} | {str(c.get('detail', '')).replace('|', '/')[:400]} |")
    lines += ["", "| Suite | Base | Collected | New | Ran | Passed | Failed | Errors | Skipped | Seconds |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in result["counts"].items():
        lines.append(f"| {name} | {c['base']} | {c['collected']} | {c['new']} | {c['ran']} | {c['passed']} | "
                     f"{c['failed']} | {c['errors']} | {c['skipped']} | {c['seconds']} |")
    lines += ["", "<details><summary>Result JSON</summary>", "", "```json", json.dumps(result, indent=2), "```",
              "", "</details>", ""]
    return "\n".join(lines)


# Main -------------------------------------------------------------------------------------------


def ci(tree: Path, expect: dict, out_dir: Path) -> dict:
    started = now()
    out_dir.mkdir(parents=True, exist_ok=True)
    env = app_env(tree)
    head = git(["rev-parse", "HEAD"], tree).strip()
    for d in expect.get("dirs", []):
        (tree / d).mkdir(parents=True, exist_ok=True)
    setup_log, setup_failed = [], ""
    for cmd in expect.get("setup", []):
        say(f"setup: {' '.join(cmd)}")
        rc, out, secs = run(cmd, tree, env, SETUP_TIMEOUT_S)
        setup_log.append({"cmd": cmd, "returncode": rc, "seconds": secs})
        if rc != 0:
            setup_failed = f"setup failed: {' '.join(cmd)} exited {rc}: {' | '.join(tail(out, 5))}"
            say(setup_failed)
            break
    protected = protected_untouched(tree, expect["base_sha"],
                                    list(expect.get("protected", [])) + list(expect.get("mocks_protected", [])))
    if setup_failed:
        counts, facts, problems = {}, {}, [setup_failed]
        checks = evaluate(counts, facts, problems, protected)
        checks["suite_passes"]["detail"] = setup_failed
    else:
        counts, facts, problems = run_suites(tree, expect, out_dir, env)
        checks = evaluate(counts, facts, problems, protected)
    result = {
        "version": 1,
        "source": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local",
        "head_sha": head,
        "base_sha": expect["base_sha"],
        "ticket": expect.get("ticket", ""),
        "run": expect.get("run", ""),
        "checks": checks,
        "counts": counts,
        "started": started,
        "finished": now(),
        "commands": {"setup": setup_log, "suites": expect["suites"]},
        "conclusion": "pass" if all(c["status"] == "pass" for c in checks.values()) else "fail",
    }
    (out_dir / RESULT).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(markdown(result))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect", required=True, type=Path, help="the expectations file Ship wrote")
    ap.add_argument("--out-dir", required=True, type=Path, help="where the result JSON and JUnit files go")
    ap.add_argument("--tree", default=Path.cwd(), type=Path, help="the head checkout (default: the current folder)")
    a = ap.parse_args(argv)
    if not a.expect.exists():
        print(f"sdlc-ci: no expectations file at {a.expect}; Ship writes it on the ticket branch", file=sys.stderr)
        return 2
    expect = json.loads(a.expect.read_text())
    missing = [k for k in ("base_sha", "suites") if not expect.get(k)]
    if missing:
        print(f"sdlc-ci: the expectations file lacks {missing}", file=sys.stderr)
        return 2
    if shutil.which("git") is None:
        print("sdlc-ci: git is not on PATH", file=sys.stderr)
        return 2
    result = ci(a.tree.resolve(), expect, a.out_dir.resolve())
    for name, c in result["checks"].items():
        say(f"{name}: {c['status']}: {c.get('detail', '')}")
    say(f"conclusion: {result['conclusion']}")
    return 0 if result["conclusion"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
