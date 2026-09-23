# Kintsugi trial on tablib

`eval/tablib_trial.py` runs the whole Kintsugi pipeline against real regressions seeded into [jazzband/tablib](https://github.com/jazzband/tablib) and writes a report. Nothing inside Kintsugi is simulated: diagnosis uses your configured model, the counterfactual and the Verifier run tablib's real test suite in git worktrees, and the Action Layer builds real PR payloads.

**The one exception is GitHub, which runs in dry-run mode.** The PR and escalation-issue requests are built and recorded but not sent, because jazzband/tablib isn't your repo.

## Run it (Windows, from `D:\Kintsugi`)

```
.venv\Scripts\activate
pip install -r requirements.txt -r eval\requirements-tablib.txt
```

In `.env`, set the provider, its key and **its model** (the model has no default):

```
SEMANTIC_PROVIDER=groq
STRUCTURAL_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=<a current Groq chat model id from console.groq.com/docs/models>
```

Then:

```
python eval\tablib_trial.py --validate-seeds     # no model calls; every line should say OK
python eval\tablib_trial.py                      # the real trial (~20 Runs)
```

The first command clones tablib into `eval\.work\tablib` (git-ignored) and pins it to a commit whose suite passes. The report is written to `eval\results\tablib-<timestamp>.md`, alongside the `.json` and a metrics `.jsonl`.

`--only <ids>` runs a subset, and `--passes 1` skips the cache replay.

## Scenarios

Each code scenario is a single committed regression on top of tablib `a36c965`. `--validate-seeds` checks that every one of them actually breaks tablib's tests.

| id | regression | tablib tests failing |
|---|---|---|
| `height_off_by_one` | `Dataset.height` returns one less | 54 |
| `width_off_by_one` | `Dataset.width` returns one more | 159 |
| `sort_reverse_inverted` | `sort(col_name, reverse=)` inverted | 1 |
| `transpose_branch_swapped` | `transpose()` takes the wrong headers branch | 3 |
| `remove_duplicates_noop` | `remove_duplicates()` keeps duplicates | 1 |
| `row_lpush_appends` | `Row.lpush()` appends instead of prepending | 1 |
| `csv_default_delimiter` | CSV default delimiter `,` → `;` | 9 |
| `json_ascii_escaping` | JSON export escapes non-ASCII | 1 |
| `tsv_syntax_error` | missing `:` in the TSV module (import-time SyntaxError) | 3 |
| `infra_connection_timeout` | no code change; the CI log carries a network timeout | n/a |

Pass 2 replays every failure as a new CI build to measure the fix-cache. Cached patches are still re-verified by running the tests.

## What the report tells you, and what it doesn't

- **Healed** means a Verifier-passing patch went through every gate, and a PR was prepared (dry-run). That is the claim you can make.
- The escalation section gives the exact reason for each escalation: which gate refused, or what the Verifier saw.
- These are seeded regressions, one per commit, in a small, well-tested library. The results say nothing about large codebases, multi-cause failures or flaky suites.
- `--stub-model` swaps the model for an oracle that is handed the answer. It exists only to check the plumbing. **Never report stub numbers as results.** The report says so in bold at the top whenever the stub was used.

## Known limits this trial will show

- **Structural and mechanical failures always escalate.** On the v1 path, the Confidence Gate requires an attribution, which only semantic Runs produce. So `tsv_syntax_error` and `infra_connection_timeout` end with "No attribution evidence available", even though the structural fix may be correct.
- **The Input Guardrail's injection regexes are broad.** `run.*(code|shell|command|script)` matched an innocent commit message ("CI runner lost the network (code is fine)") during harness development.
