#!/usr/bin/env python3
"""
Set up an isolated Python environment for pipeline verification tools.

This environment is kept separate from the main TTS runtime to avoid dependency
conflicts (e.g. whisperx / resemblyzer may require different torch/numpy versions
than the Chatterbox TTS engine).

Usage:
    python scripts/setup_verification_env.py

The environment is created at `.verification_venv/` by default.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VENV_DIR = Path(".verification_venv")

# Core packages for the verification stack.
# Pin ranges loosely so pip can resolve a compatible set.
REQUIREMENTS = [
    # WhisperX and its heavy dependencies.
    "torch>=2.0.0,<2.6.0",
    "torchaudio>=2.0.0,<2.6.0",
    "numpy<2",  # whisperx / pyannote still expect numpy 1.x
    "whisperx",
    # Speaker embedding.
    "resemblyzer",
    # Content accuracy.
    "jiwer",
    # Spectral analysis (also available in main env, but pinned here for isolation).
    "librosa",
    "soundfile",
]


def _python_executable(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pip_executable(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "pip.exe"
    return venv_dir / "bin" / "pip"


def create_venv(venv_dir: Path, base_python: str = sys.executable) -> Path:
    """Create a fresh virtual environment."""
    if venv_dir.exists():
        logger.warning(f"Environment already exists at {venv_dir}; reusing it.")
    else:
        logger.info(f"Creating verification venv at {venv_dir} using {base_python}...")
        subprocess.run(
            [base_python, "-m", "venv", str(venv_dir)],
            check=True,
        )
        logger.info("Virtual environment created.")
    return _python_executable(venv_dir)


def install_packages(pip_exe: Path) -> None:
    """Install verification packages into the venv."""
    logger.info("Upgrading pip...")
    subprocess.run(
        [str(pip_exe), "install", "--upgrade", "pip", "setuptools", "wheel"],
        check=True,
    )

    logger.info("Installing verification packages (this may take several minutes)...")
    cmd = [str(pip_exe), "install"] + REQUIREMENTS
    subprocess.run(cmd, check=True)
    logger.info("Verification packages installed.")


def smoke_test(python_exe: Path) -> None:
    """Quick import check for the key tools."""
    logger.info("Running smoke tests...")
    script = "import whisperx, resemblyzer, jiwer, librosa; print('OK')"
    result = subprocess.run(
        [str(python_exe), "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(f"Smoke test failed:\n{result.stderr}")
        raise RuntimeError("Verification environment smoke test failed.")
    logger.info("Smoke tests passed.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up isolated verification environment for TTS pipeline"
    )
    parser.add_argument(
        "--venv-dir",
        type=Path,
        default=DEFAULT_VENV_DIR,
        help=f"Directory for the virtual environment (default: {DEFAULT_VENV_DIR})",
    )
    parser.add_argument(
        "--base-python",
        default=sys.executable,
        help="Python interpreter used to create the venv",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the post-install smoke test",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        python_exe = create_venv(args.venv_dir, args.base_python)
        pip_exe = _pip_executable(args.venv_dir)
        install_packages(pip_exe)
        if not args.skip_smoke:
            smoke_test(python_exe)
        logger.info(
            f"Verification environment ready at {args.venv_dir}. "
            f"Python: {python_exe}"
        )
        return 0
    except Exception as e:
        logger.error(f"Failed to set up verification environment: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
