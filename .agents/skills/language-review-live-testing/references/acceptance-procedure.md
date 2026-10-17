# Проверка реального Language Review

1. Запусти `uv sync --locked`, статические checks и unit tests согласно `.codex/context/verification.md` — документу-владельцу Windows gate.
2. Запусти `scripts/verify-package.ps1` на Windows PowerShell; он должен собрать wheel и sdist, установить отдельно и вызвать *установленный* CLI, а не импортировать исходники через `PYTHONPATH`.
3. Сформируй синтетические входы `.rs`, `.cs`, `.cpp`, `.md` и ошибки: BOM, CRLF, Unicode, mixed encoding, binary, oversized, ignored paths, unreadable paths и symlink/cycle (там, где допускает ОС).
4. Для `scan` сравни JSON и статус с текущим публичным контрактом; проверь отсутствие записи во входные файлы и побочных эффектов при ошибках.
5. Для `config show` проверь precedence и masking токенов; `doctor` — доступные/недоступные явно объявленные локальные сервисы. Не обращайся к внешней сети без необходимости.
6. Проверь Ctrl-C/error codes только в изолированном окружении, без риска сломать пользовательскую сессию.
7. Удали созданный собственный temp, не удаляя чужие данные. Верни matrix результатов и не скрывай пропущенные платформы.
