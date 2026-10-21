"""Инвентаризация текстовых файлов для Language Review."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("language-review")
except PackageNotFoundError:
    __version__ = "0.1.0"
