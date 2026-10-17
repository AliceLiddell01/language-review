from pathlib import Path

import pytest


@pytest.fixture
def isolated_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    return appdata
