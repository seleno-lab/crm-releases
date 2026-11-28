# Repository instructions

<!-- blacksmith-runner-policy:start -->
## GitHub Actions runner policy

All new or modified workflow jobs must use approved Blacksmith runners, including
scheduled, manual, release, reusable workflows and every runner-matrix entry.
Use the existing runner sizes in this repo and the explicit labels in
[the shared policy](.github/runner-policy/policy.json). Do not introduce GitHub-hosted
`ubuntu-*`, `windows-*`, `macos-*`, generic self-hosted labels or automatic fallback.

Run `python3 .github/runner-policy/check.py` with the pinned dependencies before
landing workflow changes, and validate the affected checks. Keep the always-on
`Blacksmith runner policy` required check enabled. An exception needs explicit
owner approval and a scoped, expiring record in `.github/runner-policy-exceptions.json`.
Do not add an exception just to make CI green.

The canonical checker and docs live in `seleno-lab/seleno-infra` at
`.github/runner-policy`. Update consumers with its `sync.py`; do not edit copied
checker files independently. See [runner policy maintenance](.github/runner-policy/README.md)
for setup, exceptions, required-check enforcement and new-repository onboarding.
<!-- blacksmith-runner-policy:end -->
