"""Local runs (`python -m web.app`) step past ports that are already in use."""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.app import free_port  # noqa: E402


def test_free_port_skips_ports_in_use():
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert free_port(taken, tries=5) != taken


def test_free_port_gives_up_when_all_are_taken():
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        with pytest.raises(SystemExit, match="all in use"):
            free_port(busy.getsockname()[1], tries=1)
