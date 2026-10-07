"""Run the dependency-free frontend contract checks with Node."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_transfer_frontend_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", str(root / "tests" / "test_transfer_frontend.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
