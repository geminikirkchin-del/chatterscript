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
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_VENV_DIR = Path(".verification_venv")

# Core packages for the verification stack.
# Pin ranges loosely so pip can resolve a compatible set.
# NOTE: torch/torchaudio are intentionally NOT listed here — they are installed
# separately by install_torch(), which can target either CPU (PyPI default) or a
# CUDA build (pytorch.org index) via the --cuda flag.
REQUIREMENTS = [
    # WhisperX and its heavy dependencies.
    # torch>=2.6 is required so transformers can safely load alignment models
    # that are not published as safetensors (e.g. WhisperX Chinese wav2vec2).
    "numpy<2",  # whisperx / pyannote still expect numpy 1.x
    "scipy",
    "whisperx",
    # Speaker embedding dependencies. resemblyzer itself is installed separately
    # with --no-deps because its declared webrtcvad dependency requires a compiler
    # on Windows; webrtcvad-wheels provides pre-built binaries and satisfies the
    # runtime import.
    "webrtcvad-wheels",
    # Content accuracy.
    "jiwer",
    # Chinese text normalization for WER/CER comparison (Traditional→Simplified
    # and digit↔Chinese-numeral conversion).
    "opencc-python-reimplemented",
    "cn2an",
    # Spectral analysis (also available in main env, but pinned here for isolation).
    "librosa",
    "soundfile",
    "tqdm",
]

TORCH_VERSION = "2.6.0"
TORCHAUDIO_VERSION = "2.6.0"
# CUDA wheels live on the pytorch.org index, keyed by CUDA runtime version.
CUDA_INDEX_URLS = {
    "cu124": "https://download.pytorch.org/whl/cu124",
    "cu126": "https://download.pytorch.org/whl/cu126",
    "cu128": "https://download.pytorch.org/whl/cu128",
}


def _python_executable(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pip_executable(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "pip.exe"
    return venv_dir / "bin" / "pip"


def _venv_module_available(python_exe: str) -> bool:
    try:
        subprocess.run(
            [python_exe, "-m", "venv", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


def create_venv(venv_dir: Path, base_python: str = sys.executable) -> Path:
    """Create a fresh virtual environment."""
    if not _venv_module_available(base_python):
        # The embedded Python may not include venv; fall back to system python.
        fallback = "python"
        logger.warning(
            f"'{base_python}' does not support venv. Falling back to '{fallback}'."
        )
        base_python = fallback

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


def install_torch(pip_exe: Path, cuda: Optional[str] = None) -> None:
    """
    Install torch/torchaudio into the venv, CPU or CUDA build.

    The PyPI wheels are CPU-only; CUDA wheels come from the pytorch.org index.
    Pass cuda="cu126" (etc.) for a CUDA build matching your driver — check
    `nvidia-smi` for the maximum supported CUDA version and pick at or below it.
    """
    if cuda is None:
        logger.info(f"Installing CPU torch=={TORCH_VERSION} from PyPI...")
        cmd = [str(pip_exe), "install", f"torch=={TORCH_VERSION}", f"torchaudio=={TORCHAUDIO_VERSION}"]
    else:
        index_url = CUDA_INDEX_URLS[cuda]
        logger.info(f"Installing CUDA torch=={TORCH_VERSION} ({cuda}) from {index_url}...")
        cmd = [
            str(pip_exe), "install",
            f"torch=={TORCH_VERSION}", f"torchaudio=={TORCHAUDIO_VERSION}",
            "--index-url", index_url,
        ]
    subprocess.run(cmd, check=True)


def install_packages(pip_exe: Path) -> None:
    """Install verification packages into the venv."""
    # Avoid upgrading pip while it is running; venv already provides a working pip.
    logger.info("Ensuring setuptools and wheel are available...")
    # ctranslate2 (a whisperx dependency) still imports pkg_resources, which was
    # removed from setuptools 70+. Pin to the last version that ships it.
    subprocess.run(
        [str(pip_exe), "install", "--upgrade", "setuptools<70", "wheel"],
        check=True,
    )

    logger.info("Installing verification packages (this may take several minutes)...")
    cmd = [str(pip_exe), "install"] + REQUIREMENTS
    subprocess.run(cmd, check=True)
    logger.info("Verification packages installed.")

    # Some packages (e.g. torch 2.6+) pull in a newer setuptools that removed
    # pkg_resources, breaking ctranslate2. Downgrade setuptools back to the
    # last version that ships pkg_resources.
    logger.info("Downgrading setuptools to keep pkg_resources available...")
    subprocess.run(
        [str(pip_exe), "install", "--force-reinstall", "setuptools<70"],
        check=True,
    )

    _install_resemblyzer_without_webrtcvad(pip_exe)


def _install_resemblyzer_without_webrtcvad(pip_exe: Path) -> None:
    """
    Install resemblyzer while bypassing its webrtcvad build dependency.

    On Windows the source distribution of webrtcvad requires Visual C++ 14.0.
    webrtcvad-wheels (already installed above) provides pre-built binaries, so
    we download the resemblyzer wheel and install it with --no-deps.
    """
    import tempfile

    logger.info("Downloading resemblyzer wheel for --no-deps install...")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        subprocess.run(
            [str(pip_exe), "download", "--no-deps", "-d", str(tmp_path), "resemblyzer"],
            check=True,
        )
        wheels = list(tmp_path.glob("resemblyzer-*.whl"))
        if not wheels:
            raise RuntimeError("Failed to download resemblyzer wheel.")
        wheel = wheels[0]
        logger.info(f"Installing resemblyzer from {wheel.name} without deps...")
        subprocess.run(
            [str(pip_exe), "install", "--no-deps", str(wheel)],
            check=True,
        )
    logger.info("Resemblyzer installed.")


def smoke_test(python_exe: Path) -> None:
    """Quick import check for the key tools."""
    logger.info("Running smoke tests...")
    script = (
        "import whisperx, resemblyzer, jiwer, librosa, opencc, cn2an, torch; "
        "print('OK, torch cuda:', torch.cuda.is_available())"
    )
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
    parser.add_argument(
        "--cuda",
        choices=sorted(CUDA_INDEX_URLS.keys()),
        default=None,
        help="Install a CUDA torch build (e.g. cu126) instead of the CPU wheel. "
        "Check `nvidia-smi` for your driver's max CUDA version and pick at or below it.",
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
        install_torch(pip_exe, cuda=args.cuda)
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
