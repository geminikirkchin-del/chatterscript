# Runner that invokes verification-tool subprocesses from the main TTS process.

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_VENV_DIR = Path(".verification_venv")


def _venv_python(venv_dir: Path = DEFAULT_VENV_DIR) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _wrapper_script_path() -> Path:
    """Absolute path to the whisperx_align.py wrapper script."""
    return Path(__file__).parent / "whisperx_align.py"


def find_verification_python(venv_dir: Optional[Path] = None) -> Optional[Path]:
    """Return the Python interpreter from the isolated verification venv if it exists."""
    venv = venv_dir or DEFAULT_VENV_DIR
    exe = _venv_python(venv)
    if exe.exists():
        return exe
    return None


def run_whisperx_align(
    audio_path: str,
    reference_text: str,
    language: str = "en",
    model_name: str = "small",
    device: str = "auto",
    venv_dir: Optional[Path] = None,
    timeout: Optional[int] = 600,
) -> Optional[Dict[str, Any]]:
    """
    Invoke the WhisperX alignment wrapper in the isolated venv.

    Returns the parsed JSON dict, or None if the subprocess fails or the venv
    is not available.
    """
    return _run_subprocess_wrapper(
        script=_wrapper_script_path(),
        cmd_args=[
            "--audio", audio_path,
            "--reference-text", reference_text,
            "--language", language,
            "--model", model_name,
            "--device", device,
        ],
        label="WhisperX alignment",
        timeout=timeout,
        venv_dir=venv_dir,
    )



def _resemblyzer_script_path() -> Path:
    return Path(__file__).parent / "resemblyzer_speaker.py"


def _librosa_script_path() -> Path:
    return Path(__file__).parent / "librosa_spectral.py"


def _run_subprocess_wrapper(
    script: Path,
    cmd_args: list,
    label: str,
    timeout: Optional[int] = 300,
    venv_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    python_exe = find_verification_python(venv_dir)
    if python_exe is None:
        logger.warning(
            f"Verification venv not found at {venv_dir or DEFAULT_VENV_DIR}. "
            "Run: python scripts/setup_verification_env.py"
        )
        return None

    cmd = [str(python_exe), str(script)] + cmd_args
    logger.info(f"Running {label} wrapper")
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error(f"{label} wrapper timed out after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"Failed to run {label} wrapper: {e}")
        return None

    if result.returncode != 0:
        logger.error(
            f"{label} wrapper failed (exit {result.returncode}):\n{result.stderr[-2000:]}"
        )
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse {label} wrapper output: {e}\n{result.stdout[-2000:]}")
        return None


def run_resemblyzer_speaker(
    audio_path: str,
    reference_voice_path: str,
    timeout: Optional[int] = 300,
) -> Optional[Dict[str, Any]]:
    """Invoke the Resemblyzer speaker similarity wrapper in the isolated venv."""
    return _run_subprocess_wrapper(
        script=_resemblyzer_script_path(),
        cmd_args=["--audio", audio_path, "--reference-voice", reference_voice_path],
        label="Resemblyzer speaker",
        timeout=timeout,
    )


def run_librosa_spectral(
    audio_path: str,
    reference_voice_path: str,
    timeout: Optional[int] = 300,
) -> Optional[Dict[str, Any]]:
    """Invoke the Librosa spectral analysis wrapper in the isolated venv."""
    return _run_subprocess_wrapper(
        script=_librosa_script_path(),
        cmd_args=["--audio", audio_path, "--reference-voice", reference_voice_path],
        label="Librosa spectral",
        timeout=timeout,
    )
