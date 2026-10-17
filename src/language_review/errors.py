class LanguageReviewError(Exception):
    """Базовая ошибка, которую можно безопасно показать пользователю."""


class ConfigurationError(LanguageReviewError):
    """Некорректная или недоступная конфигурация."""


class InputError(LanguageReviewError):
    """Некорректный или небезопасный входной путь."""


class OutputError(LanguageReviewError):
    """Ошибка сохранения отчёта."""
