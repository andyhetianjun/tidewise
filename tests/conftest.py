import os

import pytest

from tidewise.fixtures import FixtureSource
from tidewise.service import TideService


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("TIDEWISE_OFFLINE", "1")
    monkeypatch.delenv("TIDEWISE_NOW", raising=False)
    monkeypatch.setenv("TIDEWISE_CACHE_DIR", str(tmp_path / "cache"))
    yield
    os.environ.pop("TIDEWISE_NOW", None)  # `tidewise eval` sets it directly


@pytest.fixture
def service() -> TideService:
    return TideService(FixtureSource())
