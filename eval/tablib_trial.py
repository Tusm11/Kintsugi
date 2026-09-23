"""Kintsugi trial on tablib: seed real regressions, run the full pipeline, report what happened.

What this does, end to end, with nothing simulated inside Kintsugi:

  1. Clones jazzband/tablib (once) into eval/.work/tablib and pins it to a
     known-good commit whose test suite passes.
  2. For each scenario, creates a local branch with ONE seeded regression
     committed on top of that base, and runs tablib's real tests to capture the
     real failure log. A seed that doesn't break the tests is reported and skipped.
  3. Feeds each failure to KintsugiPipeline exactly like a CI webhook would:
     classification -> attribution (model) -> executed counterfactual (revert the
     suspected hunk, re-run the failing tests) -> fix-cache lookup -> repair
     (model, context buckets) -> output guardrail -> confidence gate -> scope
     guard -> Verifier (applies the patch in a git worktree, runs the tests)
     -> Action Layer.
  4. Pass 2 replays the same failures to measure the fix-cache.
  5. Writes eval/results/tablib-<timestamp>.{json,md} and a metrics .jsonl.

The ONE deliberate difference from production: GitHub is DRY-RUN. The Action
Layer builds the exact PR (blobs/tree/commit/branch/pull payloads, from the
patched files) but the requests are recorded, not sent, because jazzband/tablib
is not your repository. Nothing is pushed or posted anywhere.

Usage (from the Kintsugi repo root, in Kintsugi's virtualenv):

    pip install -r eval/requirements-tablib.txt
    # .env: SEMANTIC_PROVIDER, STRUCTURAL_PROVIDER, the matching *_API_KEY and *_MODEL
    python eval/tablib_trial.py

    python eval/tablib_trial.py --only height_off_by_one sort_reverse_inverted
    python eval/tablib_trial.py --validate-seeds     # no model calls: just check each seed breaks tests
    python eval/tablib_trial.py --stub-model         # plumbing check with an ORACLE stub; NOT a result
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TABLIB_URL = "https://github.com/jazzband/tablib.git"
TABLIB_BASE = "a36c9654742d0cf07675a79df10581b3e1344555"  # "Hash pin GitHub Actions (#640)", suite green
REPO_NAME = "jazzband/tablib"
WORK = ROOT / "eval" / ".work"
RESULTS = ROOT / "eval" / "results"
LOG_TAIL_CHARS = 6000


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    kind: str                 # "code" (a committed regression) or "infra" (tests pass; CI log shows an infra error)
    file: str = ""
    old: str = ""
    new: str = ""
    infra_log: str = ""
    expected_layer: str = "semantic"


SCENARIOS: List[Scenario] = [
    Scenario("height_off_by_one", "Dataset.height reports one row too few", "code",
             "src/tablib/core.py",
             "        return len(self._data)\n\n    @property\n    def width(self):",
             "        return len(self._data) - 1\n\n    @property\n    def width(self):"),
    Scenario("width_off_by_one", "Dataset.width reports one column too many", "code",
             "src/tablib/core.py",
             "            return len(self._data[0])\n",
             "            return len(self._data[0]) + 1\n"),
    Scenario("sort_reverse_inverted", "Dataset.sort(col_name) ignores/inverts reverse=", "code",
             "src/tablib/core.py",
             "            _sorted = sorted(self.dict, key=itemgetter(col), reverse=reverse)\n"
             "            _dset = Dataset(headers=self.headers, title=self.title)\n\n"
             "            for item in _sorted:\n                row = [item[key] for key in self.headers]",
             "            _sorted = sorted(self.dict, key=itemgetter(col), reverse=not reverse)\n"
             "            _dset = Dataset(headers=self.headers, title=self.title)\n\n"
             "            for item in _sorted:\n                row = [item[key] for key in self.headers]"),
    Scenario("transpose_branch_swapped", "transpose() picks the wrong code path for headers", "code",
             "src/tablib/core.py",
             "        if self.headers is None:\n            return self._transpose_without_headers()",
             "        if self.headers is not None:\n            return self._transpose_without_headers()"),
    Scenario("remove_duplicates_noop", "remove_duplicates() keeps duplicates", "code",
             "src/tablib/core.py",
             "if not (tuple(row) in seen or seen.add(tuple(row)))",
             "if not (tuple(row) in seen and seen.add(tuple(row)))"),
    Scenario("row_lpush_appends", "Row.lpush() inserts at the end instead of the front", "code",
             "src/tablib/core.py",
             "    def lpush(self, value):\n        self.insert(0, value)",
             "    def lpush(self, value):\n        self.insert(len(self._row), value)"),
    Scenario("csv_default_delimiter", "CSV default delimiter changed to ';'", "code",
             "src/tablib/formats/_csv.py",
             "    DEFAULT_DELIMITER = ','",
             "    DEFAULT_DELIMITER = ';'"),
    Scenario("json_ascii_escaping", "JSON export escapes non-ASCII", "code",
             "src/tablib/formats/_json.py",
             "            dataset.dict, default=serialize_objects_handler, ensure_ascii=False\n",
             "            dataset.dict, default=serialize_objects_handler, ensure_ascii=True\n"),
    Scenario("tsv_syntax_error", "Missing colon breaks the TSV format module (import-time SyntaxError)", "code",
             "src/tablib/formats/_tsv.py",
             "class TSVFormat(CSVFormat):",
             "class TSVFormat(CSVFormat)",
             expected_layer="structural"),
    Scenario("infra_connection_timeout", "CI network timeout while installing dependencies", "infra",
             infra_log=("Collecting odfpy\n  ERROR: Connection timeout while fetching "
                        "https://files.pythonhosted.org/packages/odfpy-1.4.1.tar.gz (network timeout after 30s)\n"
                        "Error: Process completed with exit code 1."),
             expected_layer="mechanical"),
]


# ---------------------------------------------------------------------------
# tablib checkout + seeding
# ---------------------------------------------------------------------------

def sh(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=kintsugi-trial", "-c", "user.email=trial@kintsugi.local",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode(errors="replace").strip()


def test_argv() -> List[str]:
    # addopts cleared: tablib's pytest.ini requires pytest-cov and html reports.
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "-rfE", "--tb=short"]


# tablib has timezone-dependent tests (e.g. dbfpy getDate from a timestamp) that
# only pass in UTC, which is what its CI uses. "UTC0" is understood by both
# POSIX and the Windows C runtime.
TEST_TZ = "UTC0"


def run_tablib_tests(cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH="src", PYTHONDONTWRITEBYTECODE="1", TZ=TEST_TZ)
    return subprocess.run(test_argv(), cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=600)


def prepare_tablib(path: Path) -> None:
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {TABLIB_URL} -> {path}")
        # LF checkout on every OS: model patches are LF, and a CRLF working tree
        # (Windows' core.autocrlf=true default) would make `git apply` reject them.
        subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", TABLIB_URL, str(path)], check=True)
    if sh(path, "config", "--get", "core.autocrlf", check=False) != "false":
        sh(path, "config", "core.autocrlf", "false")
        sh(path, "rm", "-q", "--cached", "-r", ".")
    try:
        sh(path, "cat-file", "-e", f"{TABLIB_BASE}^{{commit}}")
    except RuntimeError:
        sh(path, "fetch", "-q", "origin")
    sh(path, "checkout", "-q", "--detach", TABLIB_BASE)
    sh(path, "reset", "-q", "--hard")
    sh(path, "clean", "-qfdx", "-e", "__pycache__")
    base = run_tablib_tests(path)
    if base.returncode != 0:
        tail = base.stdout.decode(errors="replace")[-2000:]
        raise SystemExit(f"tablib's own suite fails at the pinned base commit; fix the environment first:\n{tail}")


def seed(path: Path, sc: Scenario) -> Dict[str, Any]:
    """Commit the regression on a local branch and capture the real failure log."""
    branch = f"kintsugi-trial/{sc.id}"
    sh(path, "checkout", "-q", "-B", branch, TABLIB_BASE)
    if sc.kind == "infra":
        sh(path, "commit", "-q", "--allow-empty", "-m", f"trial: {sc.title}")
        sha = sh(path, "rev-parse", "HEAD")
        return {"sha": sha, "diff": "", "logs": sc.infra_log, "seed_ok": True,
                "note": "no code change; CI log carries an infrastructure error"}
    target = path / sc.file
    text = target.read_text(encoding="utf-8")
    if text.count(sc.old) != 1:
        return {"seed_ok": False, "note": f"seed anchor found {text.count(sc.old)} times in {sc.file}"}
    target.write_text(text.replace(sc.old, sc.new), encoding="utf-8", newline="\n")
    sh(path, "commit", "-q", "-am", f"trial: {sc.title}")
    sha = sh(path, "rev-parse", "HEAD")
    diff = sh(path, "diff", TABLIB_BASE, sha) + "\n"
    result = run_tablib_tests(path)
    out = result.stdout.decode(errors="replace")
    sh(path, "checkout", "-q", "--detach", TABLIB_BASE)
    if result.returncode == 0:
        return {"sha": sha, "seed_ok": False, "note": "seeded change did not break any test"}
    return {"sha": sha, "diff": diff, "logs": out[-LOG_TAIL_CHARS:], "seed_ok": True,
            "failing_tests": sum(1 for l in out.splitlines() if l.startswith(("FAILED ", "ERROR ")))}


# ---------------------------------------------------------------------------
# Instrumentation: counting provider, dry-run GitHub, oracle stub
# ---------------------------------------------------------------------------

class CountingProvider:
    """Wraps a ModelProvider; counts calls/tokens per scenario. Behaviour unchanged."""

    def __init__(self, inner):
        self.inner = inner
        self.reset()

    def reset(self):
        self.calls = 0
        self.failed_calls = 0
        self.tokens = 0

    def call_with_retry(self, prompt, budget_tokens, temperature=0.7):
        self.calls += 1
        ok, resp = self.inner.call_with_retry(prompt, budget_tokens, temperature)
        if ok and resp is not None:
            self.tokens += int(getattr(resp, "total_tokens", 0) or 0)
        else:
            self.failed_calls += 1
        return ok, resp

    def __getattr__(self, name):
        return getattr(self.inner, name)


def make_dry_run_github():
    from src.github_client import GitHubClient

    class DryRunGitHub(GitHubClient):
        """Records the exact GitHub API requests instead of sending them."""

        def __init__(self):
            super().__init__(token="dry-run", api_url="https://api.github.com")
            self.requests: List[Dict[str, Any]] = []

        def request(self, method, path, body=None):
            self.requests.append({"method": method, "path": path, "body": body})
            n = len(self.requests)
            if method == "GET" and "/git/commits/" in path:
                return {"tree": {"sha": "dry-run-base-tree"}}
            if path.endswith("/pulls"):
                return {"html_url": f"dry-run://{REPO_NAME}/pull/{n}"}
            if path.endswith("/issues"):
                return {"html_url": f"dry-run://{REPO_NAME}/issues/{n}"}
            return {"sha": f"dry-run-sha-{n}"}

    return DryRunGitHub()


class OracleStub:
    """PLUMBING CHECK ONLY. Answers with the known cause and the exact revert patch.

    It knows the answer, so a "heal" under this stub says nothing about
    Kintsugi's diagnosis or repair quality; it only proves the sandbox, gates,
    Verifier, dry-run PR, cache and report are wired correctly.
    """

    def __init__(self):
        self.scenario: Optional[Scenario] = None
        self.revert_patch = ""

    def get_name(self):
        return "oracle-stub"

    def call_with_retry(self, prompt, budget_tokens, temperature=0.7):
        from src.model_provider import ModelResponse

        sc = self.scenario
        target = f"{sc.file} ({sc.title})" if sc and sc.file else "infrastructure"
        if prompt.startswith("Analyze this test failure and identify the root cause"):
            text = f"ROOT_CAUSE: {target}\nWHY_IT_FAILS: seeded regression\nALTERNATIVES:\nCONFIDENCE: 1"
        elif prompt.startswith("Given this suspected root cause, what evidence suggests this is NOT"):
            text = "NONE"
        elif prompt.startswith("Given this suspected root cause, list evidence that supports"):
            text = f"The failing commit changes {sc.file if sc else 'nothing'}"
        elif "SOLUTION:" in prompt:
            text = f"PROBLEM: {target}\nSOLUTION:\n```diff\n{self.revert_patch}```\nVERIFICATION: run tests"
        else:
            text = (f"ROOT_CAUSE: {target}\nPROPOSED_FIX:\n```diff\n{self.revert_patch}```\n"
                    f"WHY_THIS_WORKS: reverts the regression\nRISK_ASSESSMENT: none")
        return True, ModelResponse(content=text, input_tokens=0, output_tokens=0, stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Running + reporting
# ---------------------------------------------------------------------------

def summarize_run(run, status, summary, provider: CountingProvider, github, elapsed: float) -> Dict[str, Any]:
    from src.models import StepType

    out: Dict[str, Any] = {"final_status": status.value, "summary": summary, "seconds": round(elapsed, 1),
                           "model_calls": provider.calls, "model_failed_calls": provider.failed_calls,
                           "model_tokens": provider.tokens}
    for step in run.steps:
        o = step.output
        if step.type == StepType.ATTRIBUTION and "classified_layer" in o:
            out["classified_as"] = o["classified_layer"]
        elif step.type == StepType.ATTRIBUTION and step.attribution:
            a = step.attribution
            out["attribution"] = {
                "claimed_cause": a.claimed_cause, "counterfactual": a.counterfactual_result,
                "source": a.attribution_source, "evidence_for": a.evidence_for,
                "evidence_against": a.evidence_against, "from_cache": o.get("source") == "fix_cache",
            }
        elif step.type == StepType.REPAIR:
            ro = o.get("repair_output") or {}
            out.setdefault("repairs", []).append({
                "layer": step.layer.value, "ok": o.get("success"),
                "source": ro.get("source", "generation"), "bucket": (ro.get("context_bucket") or {}).get("tier"),
                "action": o.get("action_taken"), "patch": (ro.get("fix_patch") or "")[:1500],
                "description": (o.get("repair_description") or "")[:400],
            })
        elif "auto_apply_eligible" in o:
            out["confidence_gate"] = {"eligible": o["auto_apply_eligible"], "reason": o.get("reasoning")}
        elif "in_scope" in o:
            out["scope_guard"] = {"in_scope": o["in_scope"], "reason": o.get("reasoning")}
        elif "passed" in o:
            r = o.get("test_results") or {}
            out.setdefault("verifications", []).append({
                "passed": o["passed"], "reason": o.get("reason"),
                "tests_passed": r.get("tests_passed"), "tests_failed": r.get("tests_failed"),
            })
    pr = [r for r in github.requests if r["path"].endswith("/pulls")]
    if pr:
        blobs = [r for r in github.requests if r["path"].endswith("/git/trees")]
        out["dry_run_pr"] = {"title": pr[-1]["body"]["title"], "base": pr[-1]["body"]["base"],
                             "files": [e["path"] for e in (blobs[-1]["body"]["tree"] if blobs else [])]}
    issues = [r for r in github.requests if r["path"].endswith("/issues")]
    if issues:
        out["dry_run_escalation_issue"] = issues[-1]["body"]["title"]
    return out


def build_pipeline(tablib_path: Path, provider, metrics_path: Path):
    from src.cache_metrics import CacheMetrics
    from src.fix_cache import FixCache
    from src.pipeline import KintsugiPipeline
    from src.rate_guard import RateAnomalyGuard
    from src.sandbox import RepoSandbox

    os.environ["PYTHONPATH"] = "src"  # passed through to tablib's tests so they import the worktree's tablib
    sandbox = RepoSandbox(repo_paths={REPO_NAME: str(tablib_path)}, test_command=test_argv(), timeout_seconds=600)
    github = make_dry_run_github()
    cache = FixCache(metrics=CacheMetrics(sink_path=str(metrics_path)))
    pipeline = KintsugiPipeline(semantic_provider=provider, structural_provider=provider,
                                fix_cache=cache, sandbox=sandbox, github=github)
    # 20 back-to-back events for one repo would trip the per-minute flood guard;
    # that guard is not what this trial measures.
    pipeline.rate_guard = RateAnomalyGuard(threshold_per_window=10_000)
    return pipeline, github


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tablib-path", type=Path, default=WORK / "tablib")
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--passes", type=int, default=2, help="2 = replay every failure once to measure the fix-cache")
    ap.add_argument("--validate-seeds", action="store_true", help="only check that each seed breaks the tests")
    ap.add_argument("--stub-model", action="store_true", help="ORACLE stub instead of a real model (plumbing check)")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    # The sandbox passes TZ through to tablib's tests (Verifier + counterfactual).
    os.environ["TZ"] = TEST_TZ
    os.environ.pop("GITHUB_TOKEN", None)  # belt and braces: nothing may reach real GitHub

    scenarios = [s for s in SCENARIOS if not args.only or s.id in args.only]
    tablib = args.tablib_path.resolve()
    prepare_tablib(tablib)
    seeds = {sc.id: seed(tablib, sc) for sc in scenarios}

    if args.validate_seeds:
        for sc in scenarios:
            s = seeds[sc.id]
            print(f"{'OK ' if s['seed_ok'] else 'BAD'} {sc.id:28} {s.get('failing_tests', '-')!s:>3} failing  {s.get('note', '')}")
        return 0 if all(s["seed_ok"] for s in seeds.values()) else 1

    if args.stub_model:
        base_provider = OracleStub()
        model_label = "ORACLE STUB (plumbing check only; not a result)"
    else:
        from src.model_provider import get_provider

        prov = os.getenv("SEMANTIC_PROVIDER", "")
        model_var = f"{prov.upper()}_MODEL"
        if not prov or not os.getenv(f"{prov.upper()}_API_KEY") or not os.getenv(model_var):
            raise SystemExit(f"Set SEMANTIC_PROVIDER, {prov.upper() or '<PROVIDER>'}_API_KEY and {model_var} in .env "
                             "(STRUCTURAL_PROVIDER likewise). See eval/README.md.")
        os.environ.setdefault("MAX_RETRIES", "2")
        base_provider = get_provider("semantic")
        model_label = f"{prov} / {os.getenv(model_var)}"

    provider = CountingProvider(base_provider)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(parents=True, exist_ok=True)
    metrics_path = RESULTS / f"tablib-{stamp}-metrics.jsonl"
    pipeline, github = build_pipeline(tablib, provider, metrics_path)

    from src.ingestion import WebhookEvent

    results: List[Dict[str, Any]] = []
    for pass_no in range(1, args.passes + 1):
        for sc in scenarios:
            s = seeds[sc.id]
            row: Dict[str, Any] = {"pass": pass_no, "scenario": sc.id, "title": sc.title,
                                   "expected_layer": sc.expected_layer}
            if not s["seed_ok"]:
                row.update(final_status="skipped", summary=s["note"])
                results.append(row)
                continue
            if isinstance(base_provider, OracleStub):
                base_provider.scenario = sc
                base_provider.revert_patch = (sh(tablib, "diff", s["sha"], TABLIB_BASE) + "\n") if s["diff"] else ""
            provider.reset()
            github.requests.clear()
            event = WebhookEvent(
                source="github", repo=REPO_NAME, commit=s["sha"], branch="master", build_id=f"trial-{stamp}-p{pass_no}",
                failure_logs=s["logs"], diff=s["diff"], commit_message=f"trial: {sc.title}",
                webhook_id=f"trial-{stamp}-{pass_no}-{sc.id}", timestamp=datetime.now(timezone.utc),
                metadata={"trial_pass": pass_no},
            )
            pipeline.attribution.last_counterfactual_detail = None
            run = pipeline.ingest_event(event)
            started = time.monotonic()
            status, summary = pipeline.process_run(run)
            row.update(summarize_run(run, status, summary, provider, github, time.monotonic() - started))
            # Guardrail decisions from the audit log (the v1 mechanical/structural path
            # records them there but not as Run steps).
            checks = [e for e in pipeline.get_audit_trail(run.id) if e.get("event_type") == "guardrail_check"]
            row["guardrails"] = [{"name": e["guardrail"], "passed": e["passed"], "reason": e["reason"]} for e in checks]
            for e in checks:
                if e["guardrail"] == "confidence_gate" and "confidence_gate" not in row:
                    row["confidence_gate"] = {"eligible": e["passed"], "reason": e["reason"]}
                if e["guardrail"] == "scope_guard" and "scope_guard" not in row:
                    row["scope_guard"] = {"in_scope": e["passed"], "reason": e["reason"]}
            if row.get("attribution") and not row["attribution"]["from_cache"]:
                row["counterfactual_detail"] = pipeline.attribution.last_counterfactual_detail
            results.append(row)
            print(f"[pass {pass_no}] {sc.id:28} -> {row['final_status']:9} {row.get('classified_as', '?'):10} "
                  f"calls={row['model_calls']:<2} {row['summary'][:90]}")

    report = {"generated_at": stamp, "model": model_label, "tablib_base": TABLIB_BASE,
              "stub": isinstance(base_provider, OracleStub), "results": results,
              "cache_metrics": pipeline.get_cache_metrics()}
    (RESULTS / f"tablib-{stamp}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (RESULTS / f"tablib-{stamp}.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"\nReport: {RESULTS / f'tablib-{stamp}.md'}")
    return 0


def render_markdown(report: Dict[str, Any]) -> str:
    rows = report["results"]
    lines = [f"# Kintsugi on tablib: {report['generated_at']}", ""]
    if report["stub"]:
        lines += ["> **ORACLE STUB RUN: plumbing check only.** The stub model was handed the correct cause "
                  "and the exact revert patch. Nothing here measures Kintsugi's diagnosis or repair quality.", ""]
    lines += [f"- Model: `{report['model']}`",
              f"- tablib base: `{report['tablib_base'][:12]}` (jazzband/tablib), one seeded regression per scenario",
              "- GitHub: dry-run (PR/issue requests built and recorded, not sent)", ""]
    for pass_no in sorted({r["pass"] for r in rows}):
        pr = [r for r in rows if r["pass"] == pass_no and r["final_status"] != "skipped"]
        healed = sum(r["final_status"] == "healed" for r in pr)
        verified = sum(any(v["passed"] for v in r.get("verifications", [])) for r in pr)
        lines += [f"## Pass {pass_no}{' (replay: fix-cache)' if pass_no > 1 else ''}", "",
                  f"**{healed}/{len(pr)} healed** (verified fix + PR prepared), {verified}/{len(pr)} produced a "
                  f"Verifier-passing fix, {len(pr) - healed} escalated.", "",
                  "| Scenario | Classified | Counterfactual | Gate | Verified | Outcome | Model calls | Tokens | Time |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in [r for r in rows if r["pass"] == pass_no]:
            if r["final_status"] == "skipped":
                lines.append(f"| {r['scenario']} | | | | | skipped: {r['summary']} | | | |")
                continue
            attr = r.get("attribution") or {}
            gate = r.get("confidence_gate", {})
            ver = r.get("verifications", [])
            outcome = "PR prepared" if r["final_status"] == "healed" else "escalated"
            src = " (cache)" if any(x.get("source") == "fix_cache" for x in r.get("repairs", [])) else ""
            lines.append(
                f"| {r['scenario']} | {r.get('classified_as', '?')} (exp. {r['expected_layer']}) | "
                f"{attr.get('counterfactual', 'n/a')} | {'pass' if gate.get('eligible') else 'fail' if gate else 'n/a'} | "
                f"{'yes' if any(v['passed'] for v in ver) else 'no' if ver else 'not run'} | {outcome}{src} | "
                f"{r['model_calls']} | {r['model_tokens']} | {r['seconds']}s |")
        lines.append("")
    lines += ["## Why each escalation happened", ""]
    for r in rows:
        if r["final_status"] == "escalated":
            gate = r.get("confidence_gate", {})
            ver = [v["reason"] for v in r.get("verifications", []) if not v["passed"]]
            failed = [g for g in r.get("guardrails", []) if not g["passed"]]
            failed_gen = [x["description"] for x in r.get("repairs", []) if not x.get("ok") and x.get("description")]
            if ver:
                why = f"Verifier: {ver[-1]}"
            elif failed:
                why = f"{failed[0]['name']}: {failed[0]['reason']}"
            elif failed_gen:
                why = f"Repair generation failed: {failed_gen[-1][:200]}"
            else:
                why = r["summary"]
            lines.append(f"- pass {r['pass']} `{r['scenario']}`: {why}")
    cm = report.get("cache_metrics") or {}
    lines += ["", "## Fix-cache", "", "```json", json.dumps(cm, indent=2, default=str), "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
