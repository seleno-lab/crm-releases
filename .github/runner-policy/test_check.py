import datetime as dt
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

import yaml

from check import PolicyError, audit, check_installation, expand_matrix, load_workflow, read_exceptions
from sync import install

LABEL = "blacksmith-2vcpu-ubuntu-2404"


class RunnerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".github/workflows").mkdir(parents=True)

    def write(self, jobs, name="ci.yml", events=None):
        path = self.root / ".github/workflows" / name
        path.write_text(yaml.safe_dump({"on": events or {"push": None}, "jobs": jobs}))
        return path

    def errors(self, exceptions=None):
        return audit(self.root, [LABEL], exceptions or [])

    def exception(self, selector, job="build"):
        return {"workflow": ".github/workflows/ci.yml", "job": job,
                "selector": selector, "owner": "Owner", "reason": "temporary qualification",
                "approval": "https://linear.app/seleno-lab/issue/SEL-2118", "expires": "2099-01-01"}

    def test_literals_and_label_arrays(self):
        for runner in (LABEL, [LABEL], {"labels": [LABEL]}):
            with self.subTest(runner=runner):
                self.write({"build": {"runs-on": runner}})
                self.assertEqual(self.errors(), [])

    def test_refuses_github_self_hosted_unknown_and_empty_runners(self):
        for runner in ("ubuntu-latest", "windows-2022", "macos-15", "self-hosted",
                       "blacksmith-made-up", [LABEL, "self-hosted"], [], None,
                       {"group": "other", "labels": LABEL}):
            with self.subTest(runner=runner):
                self.write({"build": {"runs-on": runner}})
                self.assertTrue(self.errors())

    def test_matrix_include_cannot_hide_github_runner(self):
        self.write({"build": {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": {
            "os": [LABEL], "include": [{"os": "ubuntu-latest"}]
        }}}})
        self.assertTrue(self.errors())

    def test_exclude_and_reinclude(self):
        matrix = {"os": [LABEL, "ubuntu-latest"], "exclude": [{"os": "ubuntu-latest"}]}
        job = {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": matrix}}
        self.write({"build": job})
        self.assertEqual(self.errors(), [])
        matrix["include"] = [{"os": "ubuntu-latest"}]
        self.write({"build": job})
        self.assertTrue(self.errors())

    def test_include_overwrites_added_value(self):
        self.write({"build": {"runs-on": "${{ matrix.runner }}", "strategy": {"matrix": {
            "version": [1], "include": [{"runner": LABEL}, {"runner": "ubuntu-latest"}]
        }}}})
        self.assertTrue(self.errors())

    def test_github_documented_matrix_example(self):
        actual = expand_matrix({"fruit": ["apple", "pear"], "animal": ["cat", "dog"], "include": [
            {"color": "green"}, {"color": "pink", "animal": "cat"},
            {"fruit": "apple", "shape": "circle"}, {"fruit": "banana"}, {"fruit": "banana", "animal": "cat"}
        ]})
        self.assertEqual(actual, [
            {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
            {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
            {"fruit": "pear", "animal": "cat", "color": "pink"},
            {"fruit": "pear", "animal": "dog", "color": "green"},
            {"fruit": "banana"}, {"fruit": "banana", "animal": "cat"}
        ])

    def test_include_only_nested_matrix(self):
        self.write({"build": {"runs-on": "${{ matrix.runner.label }}", "strategy": {"matrix": {
            "include": [{"runner": {"label": LABEL}}]
        }}}})
        self.assertEqual(self.errors(), [])

    def test_unresolved_and_nested_expressions(self):
        for runner in ("${{ inputs.runner }}", "${{ vars.RUNNER }}", "${{ matrix.os || 'ubuntu-latest' }}"):
            self.write({"build": {"runs-on": runner}})
            self.assertTrue(self.errors())
        self.write({"build": {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": {"os": ["${{ vars.RUNNER }}"]}}}})
        self.assertTrue(self.errors())

    def test_dynamic_matrix_is_safe_only_with_static_runner(self):
        job = {"runs-on": LABEL, "strategy": {"matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}}
        self.write({"build": job})
        self.assertEqual(self.errors(), [])
        job["runs-on"] = "${{ matrix.os }}"
        self.write({"build": job})
        self.assertTrue(self.errors())

    def test_local_reusable_workflow_is_inspected(self):
        self.write({"call": {"uses": "./.github/workflows/reuse.yaml"}})
        self.write({"build": {"runs-on": LABEL}}, "reuse.yaml", {"workflow_call": None})
        self.assertEqual(self.errors(), [])
        self.write({"build": {"runs-on": "ubuntu-latest"}}, "reuse.yaml", {"workflow_call": None})
        self.assertTrue(self.errors())

    def test_local_caller_exception_cannot_exempt_callee(self):
        ref = "./.github/workflows/reuse.yaml"
        self.write({"call": {"uses": ref}})
        self.write({"build": {"runs-on": "ubuntu-latest"}}, "reuse.yaml", {"workflow_call": None})
        self.assertTrue(self.errors([self.exception(ref, "call")]))

    def test_missing_noncallable_and_cyclic_reusable_workflows(self):
        self.write({"call": {"uses": "./.github/workflows/missing.yml"}})
        self.assertTrue(self.errors())
        self.write({"call": {"uses": "./.github/workflows/ci.yml"}})
        self.assertTrue(self.errors())
        self.write({"call": {"uses": "./.github/workflows/ci.yml"}}, events={"workflow_call": None})
        self.assertTrue(self.errors())

    def test_remote_workflow_needs_pin_and_exception(self):
        ref = "org/repo/.github/workflows/build.yml@" + "a" * 40
        self.write({"build": {"uses": ref}})
        self.assertTrue(self.errors())
        self.assertEqual(self.errors([self.exception(ref)]), [])
        ref = "org/repo/.github/workflows/build.yml@main"
        self.write({"build": {"uses": ref}})
        self.assertTrue(self.errors([self.exception(ref)]))

    def test_exception_scope_and_stale_exception(self):
        self.write({"build": {"runs-on": "ubuntu-latest"}, "other": {"runs-on": "ubuntu-latest"}})
        self.assertTrue(self.errors([self.exception("ubuntu-latest")]))
        self.write({"build": {"runs-on": "ubuntu-latest"}})
        self.assertEqual(self.errors([self.exception("ubuntu-latest")]), [])
        self.assertTrue(self.errors([self.exception("ubuntu-24.04")]))

    def test_expiry_metadata_and_duplicate_exceptions(self):
        path = self.root / "exceptions.json"
        item = self.exception("ubuntu-latest")
        path.write_text(json.dumps([item]))
        self.assertEqual(read_exceptions(path, dt.date(2026, 11, 28)), [item])
        with self.assertRaises(PolicyError):
            read_exceptions(path, dt.date(2099, 1, 1))
        for invalid in ([item, item], [{**item, "owner": ""}], [{**item, "approval": "yes"}]):
            path.write_text(json.dumps(invalid))
            with self.assertRaises(PolicyError):
                read_exceptions(path, dt.date(2026, 11, 28))

    def test_duplicate_keys_and_invalid_yaml(self):
        path = self.root / ".github/workflows/ci.yml"
        for content in ("jobs: {x: {runs-on: ubuntu-latest, runs-on: " + LABEL + "}}", "jobs: [", "jobs: {}"):
            path.write_text(content)
            self.assertTrue(self.errors())

    def test_yaml_on_key_and_anchors(self):
        path = self.root / ".github/workflows/ci.yml"
        path.write_text(f"on: push\njobs:\n  build: &job\n    runs-on: {LABEL}\n  again: *job\n")
        self.assertIn("on", load_workflow(path))
        self.assertEqual(self.errors(), [])

    def test_workflow_symlink_and_bad_job_shape(self):
        target = self.root / "target.yml"
        target.write_text(f"jobs: {{x: {{runs-on: {LABEL}}}}}")
        path = self.root / ".github/workflows/ci.yml"
        path.symlink_to(target)
        self.assertTrue(self.errors())

        path.unlink()
        self.write({"build": {"runs-on": LABEL, "uses": "./.github/workflows/other.yml"}})
        self.assertTrue(self.errors())

    def test_reserved_required_check_name(self):
        self.write({"build": {"runs-on": LABEL, "name": "Blacksmith runner policy"}})
        self.assertTrue(self.errors())

    def test_install_preserves_docs_and_exceptions_and_detects_drift(self):
        source = Path(__file__).parent
        (self.root / "AGENTS.md").write_text("# Existing\n\nKeep this instruction.\n")
        exceptions = self.root / ".github/runner-policy-exceptions.json"
        exceptions.write_text(json.dumps([self.exception("ubuntu-latest")]))
        original_exceptions = exceptions.read_text()
        install(source, self.root, "a" * 40)
        first = (self.root / "AGENTS.md").read_text()
        install(source, self.root, "a" * 40)
        self.assertEqual((self.root / "AGENTS.md").read_text(), first)
        self.assertIn("Keep this instruction.", first)
        self.assertEqual(exceptions.read_text(), original_exceptions)
        check_installation(self.root, self.root / ".github/runner-policy")
        path = self.root / ".github/workflows/blacksmith-policy.yml"
        path.write_text(path.read_text().replace("  pull_request:", "  pull_request:\n    paths: ['docs/**']"))
        with self.assertRaises(PolicyError):
            check_installation(self.root, self.root / ".github/runner-policy")

        install(source, self.root, "a" * 40)
        (self.root / ".github/runner-policy/check.py").write_text("pass\n")
        with self.assertRaises(PolicyError):
            check_installation(self.root, self.root / ".github/runner-policy")

    def test_cli_accepts_clean_install_and_rejects_workflow_regression(self):
        install(Path(__file__).parent, self.root, "a" * 40)
        command = [sys.executable, str(self.root / ".github/runner-policy/check.py"), "--root", str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write({"build": {"runs-on": "ubuntu-latest"}})
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(".github/workflows/ci.yml / build", result.stderr)


if __name__ == "__main__":
    unittest.main()
