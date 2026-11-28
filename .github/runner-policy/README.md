# Blacksmith runner policy (SEL-2118)

Canonical source: `seleno-lab/seleno-infra/.github/runner-policy`. Other repositories
carry identical files plus a `source.json` recording the full upstream commit and
SHA-256 hashes. CI runs the local reviewed copy, never a script fetched from main.
The hash manifest detects accidental local drift; it is not a signature.

## Run and maintain

Use Python 3.10+:

```sh
python3 -m venv /tmp/seleno-runner-policy-venv
/tmp/seleno-runner-policy-venv/bin/pip install --only-binary=:all: --require-hashes -r .github/runner-policy/requirements.txt
/tmp/seleno-runner-policy-venv/bin/python -m unittest discover -s .github/runner-policy -p 'test_*.py'
/tmp/seleno-runner-policy-venv/bin/python .github/runner-policy/check.py
```

Change the canonical files in infra, add positive/negative tests, refresh its manifest
with `python3 .github/runner-policy/sync.py --canonical`, and land through its normal
checks. Then, from that clean pinned infra checkout, distribute to each consumer:

```sh
python3 .github/runner-policy/sync.py /path/to/consumer
```

The updater preserves unrelated AGENTS.md content and existing exception records.
Review each diff, run the guard, use a `kc/` branch and Conventional Commits, and
land only the exact commit whose required checks passed. Record the rollout and
any deferred repositories in SEL-2118. Add new valid runner sizes to policy.json
centrally; a label starting with blacksmith alone is insufficient.

## Coverage and limitations

Every .yml/.yaml workflow is parsed, including inactive/manual/scheduled workflows.
The checker rejects duplicate YAML keys, unapproved literal runners, unapproved
matrix combinations, unresolved runner expressions and missing/cyclic local
reusable workflows. A dynamic matrix unrelated to a literal runner is fine.
Runner matrices must be static; choose explicit Blacksmith runner labels instead
of expressions based on inputs, vars, outputs or arbitrary functions. YAML merge
keys are accepted only when they do not override a mapping key.

Static matrix axes, nested object values, include and exclude are evaluated using
GitHub's documented combination rules. Remote reusable workflows need a full
commit pin and a scoped reviewed exception, because their runner declarations
cannot be verified from this checkout. Actions used inside steps do not select
the host runner and are not reusable workflow calls.

This guard prevents config regressions. It does not prove that every label is
currently available in the account or that deployments and releases work; run
appropriate authorized qualification. It does not inspect historical branches/tags
or remove already incurred GitHub minutes. Old refs may still select old runners.

## Exceptions

Default `.github/runner-policy-exceptions.json` is `[]`. An owner must explicitly
approve an exception before it is added; record evidence rather than self-approving.
Each entry has exactly these fields:

```json
{
  "workflow": ".github/workflows/example.yml",
  "job": "example",
  "selector": "ubuntu-24.04",
  "owner": "Named responsible owner",
  "reason": "Concrete temporary need",
  "approval": "https://linear.app/seleno-lab/issue/APPROVED-ISSUE",
  "expires": "YYYY-MM-DD"
}
```

`selector` must equal the exact runs-on value (string/list/object) or the complete
pinned remote reusable workflow reference. Exceptions expire at the start of the
specified UTC date. Expired, stale and duplicate exceptions fail CI. A local reusable
caller cannot exempt the jobs in its callee. Do not weaken secrets, permissions,
environment gates or tests as part of a runner exception.

## Required check and new repositories

The same workflow runs on every branch push and every PR, without path filters or
job-level skips. `Blacksmith runner policy` must be required on the default branch,
from the GitHub Actions app (integration_id 15368), with strict up-to-date checks.
The additive repository ruleset `Blacksmith runner policy` in `ruleset.json` does
this without changing existing protections. After the guard passes on the candidate
commit, create the ruleset via the repository rulesets API, preserving other rulesets.
If a same-named ruleset exists, compare it first; do not create duplicates or overwrite
unrelated rules. Verify the effective branch rules and the installed rule afterwards.
Record permission/plan failures explicitly; merely adding the workflow is not enforcement.

For a new repo, copy this bundle, append its AGENTS section and install the workflow
before the first code commit. Push a candidate branch, let the guard pass, establish
the intended default branch using normal repository setup, and apply/verify the
required-check rule. Confirm the Blacksmith app can access the new repo. The empty
`board` repo is explicitly deferred until its first code commit (user decision,
28 Nov 2026); do not initialize it solely for this policy.

The checker, workflow and ruleset are reviewable code/config. A contributor able to
rewrite the checker or forge its check job can defeat a repo-local guard; an admin
can edit protection. This is not tamper-proof organization enforcement. Stronger
independent enforcement needs org-level required workflows or an external app and
appropriate access. No org-admin credentials are assumed or broadened by this rollout.

Matrix reference: https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations
