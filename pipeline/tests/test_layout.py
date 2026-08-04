from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PIPELINE_ROOT.parent


def test_pipeline_uses_flat_script_layout() -> None:
    assert (PIPELINE_ROOT / "main.py").is_file()
    assert (PIPELINE_ROOT / "requirements.txt").is_file()
    assert not (PIPELINE_ROOT / "pyproject.toml").exists()
    assert not (PIPELINE_ROOT / "src").exists()
    assert not (PIPELINE_ROOT / "__init__.py").exists()

    python_files = list(PIPELINE_ROOT.glob("*.py")) + list(
        (PIPELINE_ROOT / "tests").glob("*.py")
    )
    former_package_name = "youtube_" + "fetch_pipeline"
    assert all(former_package_name not in path.read_text() for path in python_files)


def test_main_script_runs_without_an_installed_project_package() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(PIPELINE_ROOT / "main.py"), "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Fetch and analyze YouTube subtitles" in result.stdout
