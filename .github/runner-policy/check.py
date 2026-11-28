#!/usr/bin/env python3
"""Fail closed on unapproved GitHub Actions runner selection (SEL-2118)."""
import argparse
import datetime as dt
import hashlib
import itertools
import json
from pathlib import Path
import re
import sys

import yaml


class PolicyError(ValueError):
    pass


class WorkflowLoader(yaml.SafeLoader):
    """GitHub's on key is not YAML 1.1's boolean; reject duplicate keys."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in values if tag != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF")
)


def mapping(loader, node, deep=False):
    # Flatten merges, but reject ambiguous duplicate/overridden selectors.
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool)):
            raise PolicyError("non-scalar YAML key")
        if key in result:
            raise PolicyError(f"duplicate YAML key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


WorkflowLoader.add_constructor("tag:yaml.org,2002:map", mapping)


def load_workflow(path):
    if path.is_symlink():
        raise PolicyError("workflow symlinks are not allowed")
    value = yaml.load(path.read_text(), Loader=WorkflowLoader)
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), dict) or not value["jobs"]:
        raise PolicyError("workflow must contain a nonempty jobs mapping")
    return value


def expand_matrix(matrix):
    if not isinstance(matrix, dict):
        raise PolicyError("runner matrix is dynamic or missing; use a static runner axis")
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    if any(not isinstance(v, list) or not v for v in axes.values()):
        raise PolicyError("runner matrix axes must be nonempty static lists")
    size = 1
    for values in axes.values():
        size *= len(values)
    if size > 256:
        raise PolicyError("runner matrix exceeds 256 combinations")
    original = [dict(zip(axes, values)) for values in itertools.product(*axes.values())] if axes else []
    exclude = matrix.get("exclude", [])
    include = matrix.get("include", [])
    if any(not isinstance(v, list) or any(not isinstance(x, dict) for x in v) for v in (exclude, include)):
        raise PolicyError("matrix include/exclude must be lists of objects")
    original = [row for row in original if not any(all(row.get(k) == v for k, v in ex.items()) for ex in exclude)]
    rows = [dict(row) for row in original]
    extra = []
    for inc in include:
        matched = False
        for base, row in zip(original, rows):
            if all(k not in base or base[k] == v for k, v in inc.items()):
                row.update(inc)
                matched = True
        if not matched:
            extra.append(dict(inc))
    rows += extra
    if not rows or len(rows) > 256:
        raise PolicyError("runner matrix has no combinations or exceeds 256 combinations")
    return rows


MATRIX = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][\w.-]*)\s*\}\}")


def resolve(selector, row):
    if isinstance(selector, str) and "${{" in selector:
        match = MATRIX.fullmatch(selector)
        if not match:
            raise PolicyError(f"unresolved runner expression: {selector}")
        value = row
        for key in match[1].split("."):
            if not isinstance(value, dict) or key not in value:
                raise PolicyError(f"missing matrix runner value: {selector}")
            value = value[key]
        if "${{" in json.dumps(value):
            raise PolicyError("nested runner expression is not static")
        return value
    if isinstance(selector, list):
        return [resolve(value, row) for value in selector]
    if isinstance(selector, dict):
        return {key: resolve(value, row) for key, value in selector.items()}
    return selector


def validate_runner(selector, allowed):
    # Groups affect placement and need explicit review, even with a familiar label.
    if isinstance(selector, dict):
        if set(selector) != {"labels"}:
            raise PolicyError("runner groups/unknown selector fields need an approved exception")
        selector = selector["labels"]
    labels = selector if isinstance(selector, list) else [selector]
    if not labels or any(not isinstance(label, str) or label not in allowed for label in labels):
        raise PolicyError(f"unapproved runner {selector!r}; use an approved Blacksmith label")


def read_exceptions(path, today):
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise PolicyError("exceptions must be a JSON list")
    keys = set()
    for item in data:
        fields = {"workflow", "job", "selector", "owner", "reason", "approval", "expires"}
        if not isinstance(item, dict) or set(item) != fields:
            raise PolicyError("exception must specify workflow/job/selector/owner/reason/approval/expires")
        if any(not isinstance(item[k], str) or not item[k].strip() for k in fields - {"selector"}):
            raise PolicyError("exception metadata must be nonempty strings")
        if dt.date.fromisoformat(item["expires"]) <= today:
            raise PolicyError(f"expired exception: {item['workflow']} / {item['job']}")
        if not item["approval"].startswith("https://"):
            raise PolicyError("exception needs an owner-approval evidence URL")
        key = (item["workflow"], item["job"])
        if key in keys:
            raise PolicyError("duplicate job exception")
        keys.add(key)
    return data


def audit(root, allowed, exceptions):
    errors = []
    workflows = {}
    directory = root / ".github/workflows"
    if directory.is_symlink():
        return [".github/workflows: symlink directory is not allowed"]
    paths = sorted(set(directory.glob("*.yml")) | set(directory.glob("*.yaml")))
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            workflows[relative] = load_workflow(path)
        except (PolicyError, yaml.YAMLError, OSError, RecursionError) as exc:
            errors.append(f"{relative}: {exc}")
    edges = {path: [] for path in workflows}
    used = set()
    for path, workflow in workflows.items():
        for name, job in workflow["jobs"].items():
            try:
                if not isinstance(job, dict):
                    raise PolicyError("job must be a mapping")
                if job.get("name") == "Blacksmith runner policy" and path != ".github/workflows/blacksmith-policy.yml":
                    raise PolicyError("reserved required-check name belongs only to blacksmith-policy.yml")
                has_runner, has_call = "runs-on" in job, "uses" in job
                if has_runner == has_call:
                    raise PolicyError("job must have exactly one of runs-on or uses")
                selector = job["runs-on"] if has_runner else job["uses"]
                approved = next((e for e in exceptions if e["workflow"] == path and e["job"] == name and e["selector"] == selector), None)
                if has_call:
                    if not isinstance(selector, str):
                        raise PolicyError("reusable workflow reference must be a string")
                    if selector.startswith("./"):
                        target = selector[2:]
                        if target not in workflows:
                            raise PolicyError(f"local reusable workflow missing or invalid: {selector}")
                        events = workflows[target].get("on", {})
                        if "workflow_call" not in (events if isinstance(events, (dict, list)) else [events]):
                            raise PolicyError(f"reusable workflow lacks workflow_call: {selector}")
                        edges[path].append(target)
                    elif not re.fullmatch(r"[\w.-]+/[\w.-]+/\.github/workflows/[\w.-]+\.ya?ml@[0-9a-f]{40}", selector) or not approved:
                        raise PolicyError("remote reusable workflow needs a full commit pin and job-specific approved exception")
                elif not approved:
                    dynamic = "${{" in json.dumps(selector)
                    rows = expand_matrix(job.get("strategy", {}).get("matrix")) if dynamic else [{}]
                    for row in rows:
                        validate_runner(resolve(selector, row), allowed)
                if approved:
                    used.add((path, name))
            except (ValueError, TypeError, AttributeError, RecursionError) as exc:
                errors.append(f"{path} / {name}: {exc}")
    def visit(path, stack):
        if path in stack:
            raise PolicyError("reusable workflow cycle: " + " -> ".join([*stack, path]))
        for target in edges[path]:
            visit(target, [*stack, path])
    try:
        for path in edges:
            visit(path, [])
    except (PolicyError, RecursionError) as exc:
        errors.append(str(exc))
    for item in exceptions:
        if (item["workflow"], item["job"]) not in used:
            errors.append(f"stale exception: {item['workflow']} / {item['job']}")
    return errors


def check_installation(root, bundle):
    source = json.loads((bundle / "source.json").read_text())
    if source.get("repository") != "seleno-lab/seleno-infra":
        raise PolicyError("canonical source must be seleno-lab/seleno-infra")
    revision = source.get("revision", "")
    if revision != "canonical" and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise PolicyError("source revision must be a full commit pin")
    for name, digest in source["sha256"].items():
        if Path(name).name != name or (bundle / name).is_symlink():
            raise PolicyError("invalid bundle file")
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise PolicyError(f"shared bundle drift: {name}; update from canonical source")
    template = (bundle / "workflow.yml").read_bytes()
    actual = root / ".github/workflows/blacksmith-policy.yml"
    if actual.is_symlink() or actual.read_bytes() != template:
        raise PolicyError("policy workflow differs from the canonical always-on template")
    agents = (root / "AGENTS.md").read_text()
    if (bundle / "AGENTS-section.md").read_text().strip() not in agents:
        raise PolicyError("root AGENTS.md is missing the canonical runner policy")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    bundle = root / ".github/runner-policy"
    try:
        check_installation(root, bundle)
        policy = json.loads((bundle / "policy.json").read_text())
        allowed = policy["allowed_labels"]
        if not isinstance(allowed, list) or not allowed or any(not isinstance(x, str) or not x.startswith("blacksmith-") for x in allowed):
            raise PolicyError("allowed_labels must contain only explicit Blacksmith labels")
        exceptions = read_exceptions(root / ".github/runner-policy-exceptions.json", dt.datetime.now(dt.timezone.utc).date())
        errors = audit(root, allowed, exceptions)
        if errors:
            raise PolicyError("\n".join(errors))
    except (PolicyError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Blacksmith runner policy failed:\n{exc}", file=sys.stderr)
        return 1
    print("Blacksmith runner policy passed: all workflow runners approved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
