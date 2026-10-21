import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# BOTPPL_IP_TAG_KEY is read at app.main import time (the app refuses to start
# without it), and test modules import app.main at collection time -- so it
# must be in the environment before pytest collects anything. The env fixture
# sets it explicitly too, like BOTPPL_DB. Valid key: 32 bytes of hex.
TEST_IP_TAG_KEY = "ab" * 32
os.environ.setdefault("BOTPPL_IP_TAG_KEY", TEST_IP_TAG_KEY)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTPPL_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("BOTPPL_IP_TAG_KEY", TEST_IP_TAG_KEY)
    # never read the dev box's /etc; the file is absent unless a test writes it
    monkeypatch.setenv("BOTPPL_SUPPORT_LINKS", str(tmp_path / "support-links.json"))


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
