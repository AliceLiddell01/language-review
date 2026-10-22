"""Проверяет regression gate на файлах, workflow и генерации shell-артефактов."""

import importlib.util
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from check_shell_automation import source_automation_errors, tracked_shell_files, workflow_errors

_VERIFIER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify-package.py"
_SPEC = importlib.util.spec_from_file_location("verify_package", _VERIFIER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)


class ShellAutomationGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def initialize_git(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)

    def test_tracked_input_sample_is_not_treated_as_automation(self):
        self.initialize_git()
        sample = self.root / "tests" / "fixtures" / "extract" / "sample.ps1"
        sample.parent.mkdir(parents=True)
        sample.write_text("Write-Output 'исходный пример'\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", "tests/fixtures/extract/sample.ps1"],
            check=True,
            capture_output=True,
        )

        self.assertEqual(tracked_shell_files(self.root), [])

    def test_tracked_shell_executable_is_reported(self):
        self.initialize_git()
        script = self.root / "scripts" / "verify.ps1"
        script.parent.mkdir()
        script.write_text("Write-Output 'служебный сценарий'\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", "scripts/verify.ps1"],
            check=True,
            capture_output=True,
        )

        self.assertEqual(tracked_shell_files(self.root), ["100644 scripts/verify.ps1"])

    def test_tracked_extensionless_shell_executable_is_reported(self):
        self.initialize_git()
        script = self.root / "tools" / "verify"
        script.parent.mkdir()
        script.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", "tools/verify"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "update-index", "--chmod=+x", "--", "tools/verify"],
            check=True,
            capture_output=True,
        )

        self.assertEqual(tracked_shell_files(self.root), ["100755 tools/verify"])

    def test_tracked_extensionless_script_with_set_e_is_reported(self):
        self.initialize_git()
        script = self.root / "tools" / "verify"
        script.parent.mkdir(parents=True)
        script.write_text("set -e\nif true; then\n  echo bad\nfi\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", "tools/verify"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "update-index", "--chmod=+x", "--", "tools/verify"],
            check=True,
            capture_output=True,
        )

        self.assertEqual(tracked_shell_files(self.root), ["100755 tools/verify"])

    def test_tracked_extensionless_script_with_set_e_without_branch_is_reported(self):
        self.initialize_git()
        script = self.root / "tools" / "verify"
        script.parent.mkdir(parents=True)
        script.write_text("set -e\nprintf 'ready\\n'\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--", "tools/verify"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "update-index", "--chmod=+x", "--", "tools/verify"],
            check=True,
            capture_output=True,
        )

        self.assertEqual(tracked_shell_files(self.root), ["100755 tools/verify"])

    def test_python_file_writer_that_targets_shell_output_is_reported(self):
        source = 'from pathlib import Path\nPath("bin/launcher.cmd").write_text("content")\n'

        errors = source_automation_errors(source)

        self.assertTrue(any("генерирует файл shell-сценария" in error for error in errors))

    def test_open_variants_reject_writes_to_shell_files(self):
        sources = (
            "open('bin/launcher.cmd', mode='w')\n",
            "import io\nio.open('bin/launcher.cmd', mode='w')\n",
            "from pathlib import Path\nPath('bin/launcher.cmd').open(mode='w')\n",
        )

        for source in sources:
            with self.subTest(source=source):
                errors = source_automation_errors(source)
                self.assertTrue(any("открывает shell-файл для записи" in error for error in errors))

        self.assertEqual(
            source_automation_errors("from pathlib import Path\nPath('notes.md').open(mode='r')\n"),
            [],
        )

    def test_launcher_generator_returning_shell_output_is_reported(self):
        source = (
            "def launcher_files(root):\n"
            "    return {root / 'bin' / 'language-review.cmd': b'legacy'}\n"
        )

        errors = source_automation_errors(source)

        self.assertTrue(any("генератор launcher_files" in error for error in errors))

    def test_generated_shell_content_is_reported_without_a_shell_suffix(self):
        source = (
            "from pathlib import Path\n"
            "Path('bin/launcher').write_text('#!/usr/bin/env sh\\nexec app\\n')\n"
            "def generate_artifacts(root):\n"
            "    return {root / 'bin' / 'runner': b'#!/bin/bash\\nexec app\\n'}\n"
        )

        errors = source_automation_errors(source)

        self.assertTrue(any("генерирует shell-содержимое" in error for error in errors))
        self.assertTrue(any("возвращает shell-содержимое" in error for error in errors))

    def test_shell_interpreter_and_shell_true_are_reported(self):
        source = (
            "import subprocess\n"
            "subprocess.run(['pwsh', '-File', 'task.ps1'])\n"
            "subprocess.run(['python', 'task.py'], shell=True)\n"
        )

        errors = source_automation_errors(source)

        self.assertTrue(any("shell-интерпретатор" in error for error in errors))
        self.assertTrue(any("shell=True" in error for error in errors))

    def test_windows_shell_executables_with_exe_and_full_path_are_reported(self):
        sources = (
            "import subprocess\n"
            "subprocess.run([r'C:\\Windows\\System32\\cmd.exe', '/c', 'whoami'])\n",
            "import subprocess\nsubprocess.run(['pwsh.exe', '-File', 'task.ps1'])\n",
        )

        for source in sources:
            with self.subTest(source=source):
                errors = source_automation_errors(source)
                self.assertTrue(any("shell-интерпретатор" in error for error in errors))

    def test_mentions_in_comments_and_input_reads_are_not_generators(self):
        source = (
            "from pathlib import Path\n"
            "# Пример имени файла: sample.ps1\n"
            "sample = Path('sample.ps1').read_text(encoding='utf-8')\n"
            "description = 'PowerShell parsers accept .ps1 input.'\n"
        )

        self.assertEqual(source_automation_errors(source), [])

    def test_workflow_accepts_one_external_command_without_inline_shell(self):
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "name: test\njobs:\n  check:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: uv run --locked pytest -n 8\n",
            encoding="utf-8",
        )

        self.assertEqual(workflow_errors(self.root), [])

    def test_windows_workflow_requires_python_shell(self):
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "name: test\njobs:\n  check:\n    runs-on: windows-latest\n"
            "    steps:\n      - run: uv sync --locked\n",
            encoding="utf-8",
        )

        errors = workflow_errors(self.root)

        self.assertTrue(any("shell: python" in error for error in errors))

    def test_workflow_accepts_python_launcher_step(self):
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "name: test\njobs:\n  check:\n    runs-on: windows-latest\n"
            "    steps:\n"
            "      - shell: python\n        run: |\n"
            "          import subprocess\n"
            "          raise SystemExit(subprocess.run("
            "['uv', 'python', 'install'], check=False).returncode)\n",
            encoding="utf-8",
        )

        self.assertEqual(workflow_errors(self.root), [])

    def test_workflow_level_shell_default_is_checked_and_inherited(self):
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        path = workflows / "ci.yml"
        path.write_text(
            "name: test\ndefaults:\n  run:\n    shell: bash\n"
            "jobs:\n  check:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: uv run pytest\n",
            encoding="utf-8",
        )

        errors = workflow_errors(self.root)

        self.assertTrue(any("задан shell bash" in error for error in errors))

        path.write_text(
            "name: test\ndefaults:\n  run:\n    shell: python\n"
            "jobs:\n  check:\n    runs-on: windows-latest\n"
            "    steps:\n      - run: |\n"
            "          import subprocess\n"
            "          raise SystemExit(subprocess.run("
            "['uv', 'sync', '--locked'], check=False).returncode)\n",
            encoding="utf-8",
        )

        self.assertEqual(workflow_errors(self.root), [])

    def test_malformed_workflow_defaults_and_steps_report_errors(self):
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "name: test\ndefaults:\n  run:\njobs:\n  check:\n"
            "    runs-on: windows-latest\n    steps:\n",
            encoding="utf-8",
        )

        errors = workflow_errors(self.root)

        self.assertTrue(any("defaults.run должен быть объектом" in error for error in errors))
        self.assertTrue(any("steps должен быть списком" in error for error in errors))

    def test_workflow_inline_script_and_shell_are_reported(self):
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "name: test\njobs:\n  check:\n    steps:\n"
            "      - shell: bash\n        run: |\n"
            "          if true; then\n"
            "            echo bad\n"
            "          fi\n",
            encoding="utf-8",
        )

        errors = workflow_errors(self.root)

        self.assertTrue(any("inline-сценарием" in error for error in errors))
        self.assertTrue(any("задан shell" in error for error in errors))

    def test_built_wheel_shell_asset_is_rejected_but_input_sample_is_allowed(self):
        artifact = self.root / "package.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("language_review/module.py", "pass\n")
            archive.writestr(
                "language_review/distribution_assets/cleanup.ps1", "Write-Output bad\n"
            )

        with self.assertRaises(_VERIFIER.SmokeFailure):
            _VERIFIER._assert_no_shell_outputs(artifact)

        artifact = self.root / "sample-only.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("language_review/module.py", "pass\n")
            archive.writestr(
                "language_review/tests/fixtures/extract/sample.ps1", "Write-Output sample\n"
            )

        _VERIFIER._assert_no_shell_outputs(artifact)

    def test_built_wheel_extensionless_script_is_rejected_but_docs_are_allowed(self):
        artifact = self.root / "content-check.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("language_review/bin/launcher", "#!/usr/bin/env sh\nexec app\n")

        with self.assertRaises(_VERIFIER.SmokeFailure):
            _VERIFIER._assert_no_shell_outputs(artifact)

        artifact = self.root / "set-e-check.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(
                "language_review/bin/runner", "set -e\nif true; then\n  echo bad\nfi\n"
            )

        with self.assertRaises(_VERIFIER.SmokeFailure):
            _VERIFIER._assert_no_shell_outputs(artifact)

        artifact = self.root / "docs-only.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("language_review/README.md", "```sh\n#!/bin/sh\nexit 0\n```\n")
            archive.writestr(
                "language_review/notes.txt", "Reference: use `sh` to read this sample.\n"
            )

        _VERIFIER._assert_no_shell_outputs(artifact)


if __name__ == "__main__":
    unittest.main()
