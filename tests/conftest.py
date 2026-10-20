import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTPPL_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    # never read the dev box's /etc; the file is absent unless a test writes it
    monkeypatch.setenv("BOTPPL_SUPPORT_LINKS", str(tmp_path / "support-links.json"))


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
