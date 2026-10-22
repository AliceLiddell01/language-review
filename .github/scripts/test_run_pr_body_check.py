"""Проверяет выбор trusted-base валидатора PR и безопасные сообщения workflow."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from run_pr_body_check import choose_checker, run_check


class PrBodyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.trusted_base = self.root / "trusted-base"
        self.event = self.root / "event.json"
        self.write_event("", login="reviewer", branch="feature/test")

    def write_event(self, body: str, *, login: str, branch: str) -> None:
        self.event.write_text(
            json.dumps(
                {"pull_request": {"body": body, "user": {"login": login}, "head": {"ref": branch}}}
            ),
            encoding="utf-8",
        )

    def write_checker(self, root: Path, valid_body: str) -> Path:
        checker = root / ".github" / "scripts" / "check_pr_body.py"
        checker.parent.mkdir(parents=True, exist_ok=True)
        checker.write_text(
            "def is_automation_pr(payload):\n"
            "    pull_request = payload.get('pull_request') or {}\n"
            "    user = pull_request.get('user') or {}\n"
            "    head = pull_request.get('head') or {}\n"
            "    return (\n"
            "        user.get('login') == 'renovate[bot]'\n"
            "        and head.get('ref', '').startswith('renovate/')\n"
            "    )\n"
            "def validate(body):\n"
            f"    return [] if body == {valid_body!r} else ['invalid']\n",
            encoding="utf-8",
        )
        return checker

    def test_trusted_base_checker_is_selected_and_its_exit_code_is_returned(self):
        trusted_checker = self.write_checker(self.trusted_base, "trusted")
        self.write_checker(self.repository, "repository")
        self.write_event("trusted", login="reviewer", branch="feature/test")

        selected, is_trusted = choose_checker(self.repository, self.trusted_base)

        self.assertEqual(selected, trusted_checker)
        self.assertTrue(is_trusted)
        self.assertEqual(run_check(self.event, self.repository, self.trusted_base), 0)

    def test_missing_trusted_checker_uses_current_checkout(self):
        current_checker = self.write_checker(self.repository, "repository")
        self.write_event("repository", login="reviewer", branch="feature/test")

        selected, is_trusted = choose_checker(self.repository, self.trusted_base)

        self.assertEqual(selected, current_checker)
        self.assertFalse(is_trusted)
        self.assertEqual(run_check(self.event, self.repository, self.trusted_base), 0)

    def test_invalid_body_does_not_appear_in_workflow_logs(self):
        self.write_checker(self.trusted_base, "valid")
        marker_text = "marker-review-body-7d6b"
        self.write_event(marker_text, login="reviewer", branch="feature/test")
        output = io.StringIO()
        errors = io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = run_check(self.event, self.repository, self.trusted_base)

        self.assertEqual(status, 1)
        self.assertNotIn(marker_text, output.getvalue() + errors.getvalue())
        self.assertIn("не соответствует обязательной структуре", errors.getvalue())

    def test_dependency_bot_exemption_uses_event_metadata(self):
        self.write_checker(self.trusted_base, "never-valid")
        self.write_event("", login="renovate[bot]", branch="renovate/update")

        self.assertEqual(run_check(self.event, self.repository, self.trusted_base), 0)

    def test_malformed_event_does_not_echo_payload(self):
        marker_text = "marker-invalid-event-7d6b"
        self.event.write_text(marker_text, encoding="utf-8")
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            status = run_check(self.event, self.repository, self.trusted_base)

        self.assertEqual(status, 2)
        self.assertNotIn(marker_text, errors.getvalue())


if __name__ == "__main__":
    unittest.main()
