"""Проверяет выбор trusted-base валидатора PR и возврат его кода завершения."""

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
        self.event.write_text("{}", encoding="utf-8")

    def write_checker(self, root: Path, exit_code: int) -> Path:
        checker = root / ".github" / "scripts" / "check_pr_body.py"
        checker.parent.mkdir(parents=True, exist_ok=True)
        checker.write_text(f"raise SystemExit({exit_code})\n", encoding="utf-8")
        return checker

    def test_trusted_base_checker_is_selected_and_its_exit_code_is_returned(self):
        trusted_checker = self.write_checker(self.trusted_base, 7)
        self.write_checker(self.repository, 3)

        selected, is_trusted = choose_checker(self.repository, self.trusted_base)

        self.assertEqual(selected, trusted_checker)
        self.assertTrue(is_trusted)
        self.assertEqual(run_check(self.event, self.repository, self.trusted_base), 7)

    def test_missing_trusted_checker_uses_current_checkout(self):
        current_checker = self.write_checker(self.repository, 5)

        selected, is_trusted = choose_checker(self.repository, self.trusted_base)

        self.assertEqual(selected, current_checker)
        self.assertFalse(is_trusted)
        self.assertEqual(run_check(self.event, self.repository, self.trusted_base), 5)


if __name__ == "__main__":
    unittest.main()
