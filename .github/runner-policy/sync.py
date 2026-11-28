#!/usr/bin/env python3
"""Install the reviewed canonical bundle without fetching or executing remote code."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

START = "<!-- blacksmith-runner-policy:start -->"
END = "<!-- blacksmith-runner-policy:end -->"


def install(source, target, revision):
    bundle = target / ".github/runner-policy"
    bundle.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in source.iterdir() if p.is_file() and p.name != "source.json")
    hashes = {}
    for path in files:
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if source.resolve() != bundle.resolve():
            shutil.copyfile(path, bundle / path.name)
    (bundle / "source.json").write_text(json.dumps({
        "repository": "seleno-lab/seleno-infra",
        "revision": revision,
        "sha256": hashes,
    }, indent=2) + "\n")
    workflows = target / ".github/workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundle / "workflow.yml", workflows / "blacksmith-policy.yml")
    exceptions = target / ".github/runner-policy-exceptions.json"
    if not exceptions.exists():
        exceptions.write_text("[]\n")
    agents = target / "AGENTS.md"
    original = agents.read_text() if agents.exists() else "# Repository instructions\n"
    section = f"{START}\n{(bundle / 'AGENTS-section.md').read_text().strip()}\n{END}"
    if START in original or END in original:
        if original.count(START) != 1 or original.count(END) != 1 or original.index(END) < original.index(START):
            raise ValueError("malformed runner-policy markers in AGENTS.md")
        start, end = original.index(START), original.index(END) + len(END)
        updated = original[:start] + section + original[end:]
    else:
        updated = original.rstrip() + "\n\n" + section + "\n"
    agents.write_text(updated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, nargs="?")
    parser.add_argument("--canonical", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    repo = source.parent.parent
    if args.canonical:
        if args.target:
            parser.error("--canonical does not take a target")
        install(source, repo, "canonical")
    else:
        if not args.target:
            parser.error("provide a consumer checkout path")
        manifest = json.loads((source / "source.json").read_text())
        if manifest.get("revision") != "canonical":
            parser.error("distribute only from the canonical infra checkout")
        dirty = subprocess.check_output([
            "git", "-C", str(repo), "status", "--porcelain", "--", ".github/runner-policy"
        ], text=True)
        if dirty:
            parser.error("commit the canonical bundle before distributing it")
        revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        install(source, args.target.resolve(), revision)


if __name__ == "__main__":
    main()
