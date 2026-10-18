"""Проверки Git-защиты локальных настроек и секретов."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from check_local_data_untracked import validate_local_data

IGNORE_RULES = ".env\n.env.*\n!.env.example\n*.local.toml\n.codex/local/\n"


class LocalDataGitGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git_config = self.root / "empty.gitconfig"
        self.git_config.write_text("", encoding="utf-8")
        self.git_env = os.environ.copy()
        self.git_env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.git_env["GIT_CONFIG_GLOBAL"] = str(self.git_config)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.git_env.pop(name, None)
        subprocess.run(
            ["git", "init", "--quiet", str(self.root)],
            check=True,
            capture_output=True,
            env=self.git_env,
        )
        (self.root / ".gitignore").write_text(IGNORE_RULES, encoding="utf-8")

    def git_add(self, path, *, force=False):
        command = ["git", "-C", str(self.root), "add"]
        if force:
            command.append("-f")
        command.extend(["--", path])
        subprocess.run(command, check=True, capture_output=True, env=self.git_env)

    def test_valid_rules_allow_example_and_need_no_local_directories(self):
        (self.root / ".env.example").write_text("EXAMPLE=value\n", encoding="utf-8")
        self.git_add(".env.example")
        self.assertEqual(validate_local_data(self.root), [])

    def test_force_added_secret_is_rejected_without_reading_its_contents(self):
        secret = "private-token-must-not-appear-in-diagnostics"
        (self.root / ".env").write_text(secret, encoding="utf-8")
        self.git_add(".env", force=True)
        errors = validate_local_data(self.root)
        self.assertTrue(any(".env" in error for error in errors))
        self.assertNotIn(secret, "\n".join(errors))

    def test_force_added_personal_toml_is_rejected(self):
        (self.root / "user.local.toml").write_text("token = 'private'\n", encoding="utf-8")
        self.git_add("user.local.toml", force=True)
        self.assertTrue(any("user.local.toml" in error for error in validate_local_data(self.root)))

    def test_force_added_dotenv_and_codex_local_files_are_rejected(self):
        paths = (".env.private", ".codex/local/private.toml")
        for path in paths:
            with self.subTest(path=path):
                file = self.root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("private-value\n", encoding="utf-8")
                self.git_add(path, force=True)
                self.assertTrue(any(path in error for error in validate_local_data(self.root)))

    def test_weakened_ignore_exception_is_rejected_by_git_check_ignore(self):
        (self.root / ".gitignore").write_text(IGNORE_RULES + "!.env.private\n", encoding="utf-8")
        errors = validate_local_data(self.root)
        self.assertTrue(
            any("не защищает обязательный путь: .env.private" in error for error in errors)
        )

    def test_unreviewed_ignore_exception_is_rejected(self):
        (self.root / ".gitignore").write_text(IGNORE_RULES + "!.env.production\n", encoding="utf-8")
        errors = validate_local_data(self.root)
        self.assertTrue(any("Неподтверждённое исключение" in error for error in errors))

    def test_missing_gitignore_fails_closed(self):
        (self.root / ".gitignore").unlink()
        self.assertTrue(
            any("Отсутствует .gitignore" in error for error in validate_local_data(self.root))
        )


if __name__ == "__main__":
    unittest.main()
