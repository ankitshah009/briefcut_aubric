"""Every test starts with no saved settings, whatever the machine running it has."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A developer's own ~/.vedit/config.json and Keychain must not change a result,
    # and no test may write to the real ones.
    home = tmp_path_factory.mktemp("vedit-home")
    monkeypatch.setenv("VEDIT_HOME", str(home))
    monkeypatch.setenv("VEDIT_NO_KEYCHAIN", "1")
    return home


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config) -> int:
    """``-n auto`` takes the cores that are free right now.

    Render tests start FFmpeg, so several suites at once would otherwise run far
    more processes than the machine has cores, and every one of them would slow down.
    """
    cores = os.cpu_count() or 2
    try:
        busy = os.getloadavg()[0]
    except OSError:
        busy = 0.0
    return max(2, min(cores, round(cores - busy)))


class _NoNetwork:
    """An opener that refuses every request: a test that reaches Meta must stub it."""

    def open(self, request, timeout=None):  # noqa: ARG002 - same shape as OpenerDirector.open
        from urllib.error import URLError

        raise URLError("tests make no network calls; stub vedit.meta._request or the Meta function under test")


@pytest.fixture(autouse=True)
def _no_meta_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Meta is the only model provider. A Meta-mode test that forgets a stub must
    # fail fast and offline, never send a clip or spend money.
    monkeypatch.setattr("vedit.meta.build_opener", lambda *handlers: _NoNetwork())
