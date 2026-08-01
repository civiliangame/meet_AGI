"""The activation script's process-takeover guard.

`kindred.py` kills whatever holds its port. Port 5000 is shared with Flask, macOS
AirPlay Receiver, and plenty else, so the check that decides *what* is safe to kill is
the one piece of that script worth testing: getting it wrong destroys somebody's
unrelated work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from kindred import looks_like_kindred  # noqa: E402


@pytest.mark.parametrize(
    "cmdline",
    [
        'C:\py\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 5000',
        "/repo/backend/.venv/bin/python -m uvicorn app.main:app --port 5000 --reload",
        "python -m UVICORN APP.MAIN:app",
    ],
)
def test_recognises_our_server(cmdline: str) -> None:
    assert looks_like_kindred(cmdline)


@pytest.mark.parametrize(
    "cmdline",
    [
        "/usr/bin/python -m flask run --port 5000",
        "node /srv/app/server.js",
        "/usr/libexec/rapportd",  # macOS AirPlay Receiver, the classic port-5000 squatter
        "python -m uvicorn other_project.main:app --port 5000",
        "",
    ],
)
def test_refuses_anything_else(cmdline: str) -> None:
    assert not looks_like_kindred(cmdline)
