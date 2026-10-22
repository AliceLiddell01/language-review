"""Общий машинный контракт Windows-приёмки перед публикацией релиза."""

from __future__ import annotations

import os
import re
from typing import Any

from language_review.release_contract import REPOSITORY, SHA, ReleaseError

SCHEMA = "language-review.release-acceptance/1"
REQUIRED = frozenset(
    {
        "independent_download_integrity",
        "shell_free_bootstrap_and_launchers",
        "claude_not_selected_absent",
        "core_foreign_cwd_readonly",
        "controlled_extras_upgrade",
        "default_bootstrap_preserves_extras_and_path",
        "release_sdist_independent_install",
        "installer_user_path_new_process",
        "idempotent_install_setup",
        "codex_skill_ownership",
        "claude_skill_ownership",
        "missing_yaml_no_creation",
        "explicit_init_check",
        "tampered_asset_preserves_install",
        "uninstall_ownership",
        "safe_reinstall",
        "dpapi_environment_preserved",
    }
)
VERSION_CHECKS = frozenset(
    {
        "real_failing_candidate_preserves_previous",
        "controlled_version_upgrade_rollback",
    }
)
OPTIONAL = {
    "codex_live_discovery": "codex_not_requested",
    "real_vale_findings_clean_stale_hash": "vale_unavailable",
}
OUT_OF_SCOPE = {
    "public_release": "Публичный GitHub Release/tag требует отдельного разрешения выпуска.",
    "agent_turns": "Вызов модели Codex/Claude и агентские turn не запускались.",
    "memory_service": "Работоспособность worker/БД и откат миграций проверяет отдельный владелец.",
    "user_oauth": "OAuth пользовательского аккаунта не запускался.",
}


def source() -> dict[str, str]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return {"kind": "local"}
    return {
        "kind": "workflow",
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "commit": os.environ.get("GITHUB_SHA", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "workflow": ".github/workflows/release.yml",
    }


def report(
    manifest: dict[str, Any],
    manifest_sha256: str,
    checks: dict[str, dict[str, str]],
    predecessor: dict[str, str] | None = None,
) -> dict[str, Any]:
    checks = dict(checks)
    for name, reason in OPTIONAL.items():
        checks.setdefault(name, {"status": "NOT_RUN", "reason": reason})
    for name in VERSION_CHECKS:
        checks.setdefault(
            name,
            {
                "status": "NOT_RUN",
                "reason": "no_predecessor"
                if manifest["previous_stable"] == "none"
                else "predecessor_unavailable",
            },
        )
    not_run = {
        **OUT_OF_SCOPE,
        **{
            name: result["reason"]
            for name, result in checks.items()
            if result["status"] == "NOT_RUN"
        },
    }
    complete = all(checks.get(name, {}).get("status") == "PASS" for name in REQUIRED)
    complete &= all(
        checks[name]["status"] == "PASS" or checks[name].get("reason") == "no_predecessor"
        for name in VERSION_CHECKS
    )
    return {
        "schema": SCHEMA,
        "status": "PASS" if complete else "PARTIAL",
        "commit": manifest["commit"],
        "candidate_tag": manifest["tag"],
        "manifest_sha256": manifest_sha256,
        "environment": "Windows/Python 3.15",
        "source": source(),
        "predecessor": predecessor,
        "checks": checks,
        "not_run": not_run,
    }


def validate_report(evidence: dict[str, Any], manifest: dict[str, Any], checksum: str) -> None:
    if (
        evidence.get("schema") != SCHEMA
        or evidence.get("status") != "PASS"
        or evidence.get("commit") != manifest["commit"]
        or evidence.get("candidate_tag") != manifest["tag"]
        or evidence.get("manifest_sha256") != checksum
        or evidence.get("environment") != "Windows/Python 3.15"
    ):
        raise ReleaseError("Нет приёмки Windows именно этого комплекта.")
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or set(checks) != REQUIRED | VERSION_CHECKS | OPTIONAL.keys():
        raise ReleaseError("Неполный или неизвестный набор проверок Windows-приёмки.")
    not_run = dict(OUT_OF_SCOPE)
    for name, result in checks.items():
        if (
            not isinstance(result, dict)
            or set(result) - {"status", "detail", "reason"}
            or any(not isinstance(value, str) for value in result.values())
        ):
            raise ReleaseError("Некорректный результат проверки: " + name)
        if result.get("status") == "PASS" and "reason" not in result:
            continue
        allowed = OPTIONAL.get(name)
        if name in VERSION_CHECKS and manifest["previous_stable"] == "none":
            allowed = "no_predecessor"
        if not allowed or result.get("status") != "NOT_RUN" or result.get("reason") != allowed:
            raise ReleaseError("Обязательная проверка не выполнена: " + name)
        not_run[name] = allowed
    if evidence.get("not_run") != not_run:
        raise ReleaseError("Перечень NOT_RUN не соответствует выполненным проверкам.")
    predecessor = evidence.get("predecessor")
    if manifest["previous_stable"] != "none":
        if (
            not isinstance(predecessor, dict)
            or set(predecessor) != {"tag", "commit", "manifest_sha256"}
            or predecessor["tag"] != manifest["previous_stable"]
            or not isinstance(predecessor["commit"], str)
            or not SHA.fullmatch(predecessor["commit"])
            or not isinstance(predecessor["manifest_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", predecessor["manifest_sha256"])
        ):
            raise ReleaseError("Нет проверенного предыдущего стабильного кандидата.")
    elif predecessor is not None:
        raise ReleaseError("У первого выпуска не должно быть предыдущего стабильного кандидата.")
    origin = evidence.get("source")
    if (
        not isinstance(origin, dict)
        or origin.get("kind") != "workflow"
        or origin.get("repository") != REPOSITORY
        or origin.get("ref") != "refs/heads/main"
        or origin.get("event") != "workflow_dispatch"
        or origin.get("workflow") != ".github/workflows/release.yml"
        or not isinstance(origin.get("commit"), str)
        or not SHA.fullmatch(origin["commit"])
        or not isinstance(origin.get("run_id"), str)
        or not re.fullmatch(r"[1-9][0-9]*", origin["run_id"])
        or origin["run_id"] != os.environ.get("LR_CANDIDATE_RUN")
    ):
        raise ReleaseError("Нет подтверждённого источника отчёта prepare.")
