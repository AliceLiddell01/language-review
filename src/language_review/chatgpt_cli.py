"""Явные пользовательские операции подключения SIWC."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from language_review.chatgpt_auth import ChatGptAuth
from language_review.chatgpt_http import CloudError


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("chatgpt", help="Sign in with ChatGPT: подключение и сессии")
    sub = parser.add_subparsers(dest="chatgpt_command", required=True)
    login = sub.add_parser("login", help="Continue with ChatGPT в системном браузере")
    login.add_argument("--account", help="повторный вход в сохранённую регистрацию")
    login.add_argument("--consent", action="store_true", help="повторно запросить разрешение плана")
    login.add_argument("--timeout", type=float, default=300)
    sub.add_parser("status", help="сохранённые регистрации без токенов")
    select = sub.add_parser("select", help="явно выбрать сохранённую регистрацию")
    select.add_argument("account")
    sub.add_parser("models", help="актуальный каталог выбранного аккаунта")
    sub.add_parser("refresh", help="обновить сессию выбранного аккаунта")
    logout = sub.add_parser("logout", help="отозвать сессию и удалить локальные токены")
    logout.add_argument("--account")


def run(arguments: argparse.Namespace) -> int:
    try:
        auth = ChatGptAuth()
        command = arguments.chatgpt_command
        result: Any = None
        if command == "login":
            if not 1 <= arguments.timeout <= 600:
                raise CloudError("oauth_invalid_timeout")
            print(
                "Continue with ChatGPT — завершите вход в браузере; Ctrl+C отменяет вход.",
                file=sys.stderr,
            )
            selected = auth.login(
                arguments.account, timeout=arguments.timeout, consent=arguments.consent
            )
            result = {"selected": selected, "cloud_consent": "requires_check_allow_cloud"}
        elif command == "status":
            result = auth.status()
        elif command == "select":
            auth.select(arguments.account)
            result = {"selected": arguments.account}
        elif command == "refresh":
            auth.access_token(force=True)
            result = {"status": "refreshed"}
        elif command == "models":
            from language_review.engine_chatgpt import models

            active = auth.status()["active"]
            if not active:
                raise CloudError("oauth_sign_in_required")
            result = {"models": models(auth, active)}
        elif command == "logout":
            confirmed = auth.logout(arguments.account)
            result = {"status": "signed_out", "remote_revocation_confirmed": confirmed}
            if not confirmed:
                print(
                    "Удалённый отзыв не подтверждён. Доступ можно отключить: https://chatgpt.com/settings/usage",
                    file=sys.stderr,
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Вход отменён; активная регистрация сохранена.", file=sys.stderr)
        return 130
    except (CloudError, OSError) as exc:
        print(getattr(exc, "code", "chatgpt_storage_unavailable"), file=sys.stderr)
        return 5
