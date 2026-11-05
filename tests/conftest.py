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
    # A shell with private mode on must not reroute Meta tests to a local model.
    monkeypatch.delenv("VEDIT_PRIVATE_MODEL", raising=False)
    monkeypatch.delenv("VEDIT_PRIVATE_MODEL_URL", raising=False)
    # Nor may a gateway or director chosen in the shell: a loopback gateway would pass the network guard below.
    monkeypatch.delenv("VEDIT_META_BASE_URL", raising=False)
    monkeypatch.delenv("VEDIT_META_DIRECTOR", raising=False)
    return home


# Caption pages measured with Arial's widths. Liberation Sans shares them and Noto Sans is close. DejaVu Sans,
# the usual Linux default, is about a tenth wider, so the same line breaks into more pages there.
_ARIAL_WIDTH_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
)


@pytest.fixture
def arial_width_captions(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the caption font for a test whose expected pages were measured in Arial."""
    for candidate in map(Path, _ARIAL_WIDTH_FONTS):
        if candidate.is_file():
            monkeypatch.setenv("VEDIT_FONT", str(candidate))
            return candidate
    pytest.skip("needs Arial, Liberation Sans or Noto Sans: these caption pages were measured with Arial's widths")


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


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
NO_NETWORK = "tests make no network calls; stub vedit.meta._request or the Meta function under test"


def loopback_url(url: str) -> bool:
    """Whether ``url`` names this machine by its loopback address, and nothing else."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and host in LOOPBACK_HOSTS
        and parts.username is None
        and parts.password is None
    )


class _LoopbackOnly:
    """An opener that reaches only this machine: a test that reaches Meta must stub it.

    A fake server on 127.0.0.1 (the SAM stream tests run one) is still reached,
    through a real opener with the caller's handlers and no proxy, so a proxy
    in the environment can never carry the request off the machine.
    """

    def __init__(self, handlers: tuple) -> None:
        self.handlers = handlers

    def open(self, request, data=None, timeout=None):  # same shape as OpenerDirector.open
        from urllib.error import URLError
        from urllib.request import ProxyHandler, build_opener

        url = request if isinstance(request, str) else request.full_url
        if not loopback_url(url):
            raise URLError(NO_NETWORK)
        opener = build_opener(ProxyHandler({}), *self.handlers)
        return opener.open(request, data) if timeout is None else opener.open(request, data, timeout)


@pytest.fixture(autouse=True)
def _no_meta_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # Meta is the only model provider. A Meta-mode test that forgets a stub must
    # fail fast and offline, never send a clip or spend money. Loopback stays
    # open so a test may answer Meta's routes from a fake server of its own.
    monkeypatch.setattr("vedit.meta.build_opener", lambda *handlers: _LoopbackOnly(handlers))
