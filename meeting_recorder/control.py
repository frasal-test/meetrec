from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Callable

from .config import (
    CONFIG_PATH,
    AppConfig,
    load_config,
    set_config_value,
    set_summary_value,
    set_summary_values,
)
from .llm import checks as llm_checks
from .llm.adapters import AdapterRegistry, default_registry
from .llm.errors import AuthError, LLMError, UnknownLLMError
from .llm.flow import run_llm_flow
from .llm.keys import InMemoryKeyStore
from .llm.models import MODEL_TABLE, find_model
from .llm.outcomes import OutcomeKind
from .llm.storage import default_emergency_dir
from .llm.transcript import load_saved_transcript
from .llm.ui import JsonLinesUI, ProtocolError
from .transcription import disable_pyannote_telemetry, load_env_files
from .session_transcription import SessionProcessor
from .sessions import (
    delete_audio_if_clean,
    enqueue_session,
    pending_sessions,
    read_json,
    recover_sessions,
    run_job,
)


# pyannote.audio 4 needs Python 3.10, and these three gated repositories must
# have their conditions accepted by the token's account. The fourth model it
# loads (wespeaker) is not gated.
PYANNOTE_GATED_REPOS = (
    "pyannote/segmentation-3.0",
    "pyannote/speaker-diarization-3.1",
    "pyannote/speaker-diarization-community-1",
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
RECORDER_BINARY = PROJECT_DIR / ".recorder"
MENUBAR_BINARY = (
    PROJECT_DIR / "MeetRec.app" / "Contents" / "MacOS" / "MeetRec"
)
SUMMARY_REQUIREMENTS = PROJECT_DIR / "requirements-summary.txt"

# Exit codes of `llm-run` (I16, T37). The Swift side relies on this table:
# change a value here and the app must change with it. Python itself uses 1
# (uncaught exception) and argparse 2 (bad arguments), so none of these.
LLM_RUN_EXIT_CODES: dict[OutcomeKind, int] = {
    OutcomeKind.DONE: 0,
    OutcomeKind.END_NO_LLM: 10,
    OutcomeKind.END_ERROR: 11,
    OutcomeKind.END_SAVE_ERROR: 12,
    OutcomeKind.END_FATAL: 13,
}
# The flow did not start: the offline startup check failed.
LLM_EXIT_STARTUP_CHECK_FAILED = 20
# The app sent something that is not the protocol (bad keys line, bad answer,
# input closed while an answer was due).
LLM_EXIT_PROTOCOL_ERROR = 21
# Anything else (missing transcript, unreadable configuration, a bug).
LLM_EXIT_INTERNAL_ERROR = 30

KEY_PROVIDERS = ("anthropic", "openai", "oci")
# Provider -> the SDK module it needs. OCI is served by the OpenAI SDK.
PROVIDER_SDK_MODULES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "oci": "openai",
}
PIP_INSTALL_TIMEOUT_SECONDS = 900


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="meetrec",
        description="Manage recording sessions and transcription jobs.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Configuration file.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("session", type=Path)
    enqueue.add_argument("--force", action="store_true")
    add_transcription_options(enqueue)

    process = subparsers.add_parser("process")
    process.add_argument("session", type=Path)
    process.add_argument("--force", action="store_true")
    add_transcription_options(process)

    worker = subparsers.add_parser("worker")
    worker.add_argument("--session", type=Path)
    worker.add_argument(
        "--once",
        action="store_true",
        help="Process the current queue once and exit.",
    )

    subparsers.add_parser("recover")
    subparsers.add_parser("doctor")
    subparsers.add_parser(
        "startup-check",
        help="Offline check of the LLM summary flow, with housekeeping.",
    )
    llm_run = subparsers.add_parser(
        "llm-run",
        help="Run the LLM summary flow on a saved transcript (JSON lines).",
    )
    llm_run.add_argument("session", type=Path)
    key_verify = subparsers.add_parser(
        "llm-key-verify",
        help="Verify an API key read from stdin (never from argv).",
    )
    key_verify.add_argument(
        "--provider", required=True, choices=KEY_PROVIDERS
    )
    key_verify.add_argument("--region")
    auto_summary = subparsers.add_parser(
        "auto-summary",
        help="Show or change the Automatic Summary setting.",
    )
    auto_actions = auto_summary.add_subparsers(
        dest="summary_action", required=True
    )
    auto_actions.add_parser("status")
    auto_on = auto_actions.add_parser("on")
    auto_on.add_argument("--model", required=True)
    auto_actions.add_parser("off")
    subparsers.add_parser("show-config")
    subparsers.add_parser("recordings-dir")
    delete_audio = subparsers.add_parser(
        "delete-audio",
        help="Show or set whether audio is deleted after a clean transcript.",
    )
    delete_audio.add_argument("state", nargs="?", choices=("on", "off"))
    return parser.parse_args(argv)


def add_transcription_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model")
    parser.add_argument(
        "--language",
        help="Language code, or 'auto' for automatic detection.",
    )
    diarization = parser.add_mutually_exclusive_group()
    diarization.add_argument(
        "--diarize-system",
        action="store_true",
        dest="diarize_system",
    )
    diarization.add_argument(
        "--no-diarize-system",
        action="store_false",
        dest="diarize_system",
    )
    parser.set_defaults(diarize_system=None)
    parser.add_argument("--num-speakers", type=int)


def requested_options(args: argparse.Namespace) -> dict[str, object]:
    options: dict[str, object] = {}
    if args.model:
        options["model"] = args.model
    if args.language:
        options["language"] = (
            None if args.language == "auto" else args.language
        )
    if args.diarize_system is not None:
        options["diarize_system"] = args.diarize_system
    if args.num_speakers is not None:
        options["num_speakers"] = args.num_speakers
    return options


def pyannote_version_check() -> tuple[bool, str]:
    # Python 3.9's importlib.metadata does not normalise names, and pip may
    # record the distribution with either spelling, so try both.
    installed = None
    for name in ("pyannote.audio", "pyannote_audio"):
        try:
            installed = importlib.metadata.version(name)
            break
        except importlib.metadata.PackageNotFoundError:
            continue
    if installed is None:
        return False, "missing — install requirements-diarization.txt"
    try:
        major = int(installed.split(".", 1)[0])
    except ValueError:
        major = 0
    if major >= 4:
        return True, f"version {installed}"
    return False, f"version {installed} — 4.x required"


# huggingface_hub's HTTP session has no timeout, so a stalled connection would
# hang doctor forever.
MODEL_ACCESS_TIMEOUT_SECONDS = 15


def model_access_check(repo: str, token: str) -> tuple[bool, str]:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_model_access_check, repo, token)
    try:
        return future.result(timeout=MODEL_ACCESS_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        return False, "could not check: timed out"
    finally:
        # Do not wait: the hung thread must not block the process from exiting.
        executor.shutdown(wait=False, cancel_futures=True)


def _model_access_check(repo: str, token: str) -> tuple[bool, str]:
    from huggingface_hub import auth_check
    from huggingface_hub.errors import (
        GatedRepoError,
        HfHubHTTPError,
        RepositoryNotFoundError,
    )

    try:
        auth_check(repo, token=token)
    except GatedRepoError:
        # Before RepositoryNotFoundError: it is a subclass of that one.
        return False, f"accept the conditions at https://huggingface.co/{repo}"
    except RepositoryNotFoundError:
        return False, "token rejected"
    except HfHubHTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 401:
            return False, "token rejected"
        if status == 403:
            return False, "token cannot read gated repositories — use a Read token"
        return False, f"could not check: {type(exc).__name__}"
    except Exception as exc:
        return False, f"could not check: {type(exc).__name__}"
    return True, "ok"


def run_startup_phase(
    config: AppConfig,
    config_path: Path,
    *,
    registry_factory: Callable[..., AdapterRegistry] = default_registry,
    templates_dir: Path | None = None,
    project_dir: Path | None = None,
) -> tuple[list[str], llm_checks.HousekeepingResult]:
    """Precondizione: controllo all'avvio (offline, nessuna chiamata di rete).

    Runs the startup check of the LLM flow and the housekeeping: orphan
    temporary files in the sessions and in the configuration directory, and
    the rotation of the menu bar logs, which live in the project directory.
    Returns (problems, housekeeping result); no problems means ok.
    """
    problems = llm_checks.startup_check(
        registry=registry_factory(config.summary),
        templates_dir=templates_dir,
    )
    result = llm_checks.housekeeping(
        [config.recordings_dir, config_path.expanduser().parent]
    )
    # The project directory holds the code and the virtual environment:
    # housekeeping() walks directories recursively and deletes `.*.tmp`, which
    # is right for sessions and configuration but not here. Only the logs,
    # which sit directly in it, are rotated.
    llm_checks.rotate_logs(project_dir or PROJECT_DIR, result)
    return problems, result


def housekeeping_to_dict(
    result: llm_checks.HousekeepingResult,
) -> dict[str, list[str]]:
    return {
        "removed_temp_files": [str(p) for p in result.removed_temp_files],
        "rotated_logs": [str(p) for p in result.rotated_logs],
    }


def startup_check_command(
    config_path: Path,
    stdout: IO[str],
    stderr: IO[str] | None = None,
    *,
    registry_factory: Callable[..., AdapterRegistry] = default_registry,
    templates_dir: Path | None = None,
    project_dir: Path | None = None,
) -> int:
    """Precondizione: controllo all'avvio.

    Prints one JSON object on stdout: {"ok", "problems", "housekeeping"}.
    Exit 0 when ok, 1 otherwise. No network call (T44): the Hugging Face
    checks belong to `doctor` only. Human text goes to stderr.
    """
    err = stderr if stderr is not None else sys.stderr
    config = load_config(config_path)
    problems, result = run_startup_phase(
        config,
        config_path,
        registry_factory=registry_factory,
        templates_dir=templates_dir,
        project_dir=project_dir,
    )
    stdout.write(
        json.dumps(
            {
                "ok": not problems,
                "problems": problems,
                "housekeeping": housekeeping_to_dict(result),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    stdout.flush()
    for problem in problems:
        print(f"Startup check: {problem}", file=err)
    return 0 if not problems else 1


def doctor(
    config_path: Path,
    *,
    registry_factory: Callable[..., AdapterRegistry] = default_registry,
) -> int:
    """`doctor` completo, in due fasi (flow.md, "Precondizione").

    Phase 1 is the startup check plus housekeeping, offline. Phase 2 is the
    rest, including the Hugging Face checks that need the network. Both count
    in the exit code, but only phase 1 gates the LLM flow.
    """
    config = load_config(config_path)
    checks: list[tuple[str, bool, str]] = []

    problems, housekeeping_result = run_startup_phase(
        config, config_path, registry_factory=registry_factory
    )
    if problems:
        for problem in problems:
            checks.append(("Startup check", False, problem))
    else:
        checks.append(
            (
                "Startup check",
                True,
                "prompt templates, model table, adapters",
            )
        )
    checks.append(
        (
            "Housekeeping",
            True,
            f"removed {len(housekeeping_result.removed_temp_files)} "
            f"orphan temp files, rotated "
            f"{len(housekeeping_result.rotated_logs)} logs",
        )
    )

    version = platform.mac_ver()[0]
    major = int(version.split(".", 1)[0]) if version else 0
    checks.append(("macOS 13+", major >= 13, version or "unknown"))
    checks.append(
        ("Swift compiler", shutil.which("swiftc") is not None, shutil.which("swiftc") or "missing")
    )
    checks.append(
        ("Recorder binary", RECORDER_BINARY.is_file(), str(RECORDER_BINARY))
    )
    checks.append(
        ("Menu bar binary", MENUBAR_BINARY.is_file(), str(MENUBAR_BINARY))
    )
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(
        ("Python 3.10+", sys.version_info >= (3, 10), python_version)
    )
    checks.append(
        (
            "faster-whisper",
            importlib.util.find_spec("faster_whisper") is not None,
            "Python module",
        )
    )
    if config.transcription.diarize_system:
        machine = platform.machine()
        checks.append(
            (
                "Apple Silicon",
                machine == "arm64",
                "required for speaker separation"
                if machine == "arm64"
                else machine,
            )
        )
        disable_pyannote_telemetry()
        pyannote_ok, pyannote_detail = pyannote_version_check()
        checks.append(("pyannote.audio", pyannote_ok, pyannote_detail))
        token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
        checks.append(
            (
                "Hugging Face token",
                bool(token),
                "HF_TOKEN or HUGGINGFACE_TOKEN",
            )
        )
        if token and pyannote_ok:
            for repo in PYANNOTE_GATED_REPOS:
                passed, detail = model_access_check(repo, token)
                checks.append((f"Model access: {repo}", passed, detail))

    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    checks.append(
        (
            "Recordings directory",
            os.access(config.recordings_dir, os.W_OK),
            str(config.recordings_dir),
        )
    )

    for name, passed, detail in checks:
        marker = "✓" if passed else "✗"
        print(f"{marker} {name}: {detail}")

    # Deliberately reported separately and excluded from the exit code: macOS
    # attributes TCC grants to the responsible process, so probing the binary
    # from here describes this terminal, not the menu-bar app. A failure would
    # be a false negative.
    if MENUBAR_BINARY.is_file():
        completed = subprocess.run(
            [str(MENUBAR_BINARY), "--doctor"],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = completed.stdout.strip() or completed.stderr.strip()
        print(f"\nPermissions as seen by this terminal: {detail}")
        print(
            "This reflects the calling process, not MeetRec.app. Check the "
            "real grants in System Settings → Privacy & Security → "
            "Microphone and Screen & System Audio Recording."
        )

    return 0 if all(item[1] for item in checks) else 1


def _emit(stream: IO[str], payload: dict[str, Any]) -> None:
    """One JSON object per line. Only protocol data goes through here."""
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def read_keys_message(stdin: IO[str]) -> InMemoryKeyStore:
    """Stato: CHECK_LENGTH (ingresso del processo LLM). The first stdin line
    of `llm-run`: {"type": "keys", "keys": {"<provider>": "<key>", ...}}.

    The keys stay in memory (I4); nothing here, including the error messages,
    ever repeats a value read from the line. Raises ProtocolError.
    """
    line = stdin.readline()
    if not line.strip():
        raise ProtocolError("the keys message is missing")
    try:
        message = json.loads(line)
    except ValueError:
        raise ProtocolError("the keys message is not valid JSON") from None
    if not isinstance(message, dict) or message.get("type") != "keys":
        raise ProtocolError("the first message must be of type keys")
    keys = message.get("keys")
    if not isinstance(keys, dict):
        raise ProtocolError("the keys message has no keys object")
    store = InMemoryKeyStore()
    for provider, key in keys.items():
        if not isinstance(key, str):
            raise ProtocolError("every key must be a string")
        # A provider that is not in the model table has nothing to use it.
        if key.strip() and provider in KEY_PROVIDERS:
            store[provider] = key.strip()
    return store


def run_llm_command(
    args: argparse.Namespace,
    stdin: IO[str],
    stdout: IO[str],
    *,
    registry_factory: Callable[..., AdapterRegistry] = default_registry,
    emergency_dir: Path | None = None,
    templates_dir: Path | None = None,
) -> int:
    """Stato: CHECK_LENGTH in poi (processo LLM, separato dal worker).

    Protocol, one JSON object per line. stdin: the keys message, then one
    answer per ask_choice / ask_retry. stdout: `ask_choice`, `ask_retry`,
    `progress` and `outcome` events (JsonLinesUI, key "event"), or one of
    {"event": "startup_check_failed", "problems": [...]},
    {"event": "protocol_error", "message": ...},
    {"event": "error", "error": <class name>} when the flow could not run.
    The exit code says how it ended (LLM_RUN_EXIT_CODES and the constants
    next to it). `registry_factory`, `emergency_dir` and `templates_dir` are
    injectable for the tests.
    """
    try:
        keystore = read_keys_message(stdin)
    except ProtocolError as exc:
        _emit(stdout, {"event": "protocol_error", "message": str(exc)})
        return LLM_EXIT_PROTOCOL_ERROR

    try:
        config = load_config(args.config)
        registry = registry_factory(config.summary)
        # Precondizione: the flow does not start if the offline check fails.
        problems = llm_checks.startup_check(
            registry=registry, templates_dir=templates_dir
        )
        if problems:
            _emit(
                stdout,
                {"event": "startup_check_failed", "problems": problems},
            )
            return LLM_EXIT_STARTUP_CHECK_FAILED
        session_dir = args.session.expanduser().resolve()
        transcript = load_saved_transcript(session_dir)
        outcome = run_llm_flow(
            transcript,
            summary_config=config.summary,
            registry=registry,
            keystore=keystore,
            ui=JsonLinesUI(stdin, stdout),
            session_dir=session_dir,
            emergency_dir=emergency_dir or default_emergency_dir(),
            templates_dir=templates_dir,
        )
    except ProtocolError as exc:
        _emit(stdout, {"event": "protocol_error", "message": str(exc)})
        return LLM_EXIT_PROTOCOL_ERROR
    except Exception as exc:
        # Only the class name: the message could carry paths or request
        # details, and nothing from here may leak a key.
        _emit(stdout, {"event": "error", "error": type(exc).__name__})
        return LLM_EXIT_INTERNAL_ERROR
    return LLM_RUN_EXIT_CODES[outcome.kind]


def sdk_importable(module_name: str) -> bool:
    """True if the provider SDK can be imported (no network)."""
    try:
        importlib.import_module(module_name)
    except ImportError:
        return False
    return True


def install_summary_requirements() -> bool:
    """Installs requirements-summary.txt into the interpreter that is running
    (the project's .venv). Output is captured: stdout is reserved for the
    protocol. True if pip succeeded."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(SUMMARY_REQUIREMENTS),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=PIP_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def make_key_verifier(provider: str, region: str | None) -> Any:
    """The real adapter of `provider`, used only for its verify_key. It gets
    an empty key store: the key under test is passed as an argument."""
    from .llm.adapters.anthropic import AnthropicAdapter
    from .llm.adapters.openai_compat import make_oci_adapter, make_openai_adapter

    store = InMemoryKeyStore()
    if provider == "anthropic":
        return AnthropicAdapter(store)
    if provider == "openai":
        return make_openai_adapter(store)
    return make_oci_adapter(store, region)


def key_verify_command(
    args: argparse.Namespace,
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str] | None = None,
    *,
    verifier_factory: Callable[[str, str | None], Any] | None = None,
) -> int:
    """Fuori dal flusso: verifica della key (I4).

    The key is read from stdin (one line, stripped), never from argv or the
    environment, and Python does not store it: after {"ok": true} the app
    saves it in the Keychain. Prints {"ok": true} or {"ok": false, "message":
    <user message>} and exits 0 or 1. If the provider's SDK is missing,
    requirements-summary.txt is installed first. For OCI the region must be
    a supported one, and it is saved in the configuration on success.
    """
    err = stderr if stderr is not None else sys.stderr

    def fail(message: str) -> int:
        _emit(stdout, {"ok": False, "message": message})
        return 1

    provider = args.provider
    region = (args.region or "").strip() or None
    if provider == "oci":
        from .llm.adapters.openai_compat import OCI_SUPPORTED_REGIONS

        if region not in OCI_SUPPORTED_REGIONS:
            return fail(
                "OCI region not supported. Available regions: "
                + ", ".join(OCI_SUPPORTED_REGIONS)
            )

    key = stdin.readline().strip()
    if not key:
        return fail(AuthError().user_message)

    module = PROVIDER_SDK_MODULES[provider]
    if not sdk_importable(module):
        print("Installing the LLM summary requirements...", file=err)
        installed = install_summary_requirements()
        importlib.invalidate_caches()
        if not installed or not sdk_importable(module):
            return fail(
                "Could not install the components this service needs. "
                "Check your connection and try again."
            )

    try:
        (verifier_factory or make_key_verifier)(provider, region).verify_key(key)
    except LLMError as exc:
        return fail(exc.user_message)
    except Exception:
        # Whatever it was, its message is not shown: it could name the key.
        return fail(UnknownLLMError().user_message)

    if provider == "oci":
        set_summary_value(args.config, "oci_region", region)
    _emit(stdout, {"ok": True})
    return 0


def summary_status(config: AppConfig) -> dict[str, Any]:
    """The Settings view of the summary configuration. Never keys."""
    return {
        "automatic": config.summary.automatic,
        "default_model": config.summary.default_model,
        "oci_region": config.summary.oci_region,
        "models": [
            {
                "provider": spec.provider,
                "model_id": spec.model_id,
                "label": spec.label,
            }
            for spec in MODEL_TABLE
        ],
    }


def auto_summary_command(
    args: argparse.Namespace, stdout: IO[str]
) -> int:
    """Fuori dal flusso: Settings, Automatic Summary (I5).

    `status` prints summary_status(). `on --model <id>` and `off` change the
    setting and print {"ok": true, ...status}; an unknown model prints
    {"ok": false, "message": ...} and exits 1.
    """
    action = args.summary_action
    if action == "status":
        _emit(stdout, summary_status(load_config(args.config)))
        return 0
    if action == "on":
        if find_model(args.model) is None:
            _emit(
                stdout,
                {"ok": False, "message": "Unknown model: choose one from the list."},
            )
            return 1
        # Only an explicit choice turns it on: the model comes from the
        # argument, the code never picks one.
        set_summary_values(
            args.config,
            {"automatic": True, "default_model": args.model},
        )
    else:
        # Off also forgets the model: turning it on again must be an
        # explicit choice with nothing preselected (I5, ui.md "Settings").
        set_summary_values(
            args.config,
            {"automatic": False, "default_model": None},
        )
    _emit(stdout, {"ok": True, **summary_status(load_config(args.config))})
    return 0


def process_queue(
    *,
    config_path: Path,
    requested: Path | None,
) -> int:
    config = load_config(config_path)
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    with worker_lock(config.recordings_dir):
        recover_sessions(
            config.recordings_dir,
            max_attempts=config.transcription.max_attempts,
        )
        sessions = pending_sessions(config.recordings_dir, requested)
        if not sessions:
            print("No pending sessions.")
            return 0

        exit_code = 0
        for session_dir in sessions:
            job = read_json(session_dir / "job.json")
            options = job.get("options")
            if not isinstance(options, dict):
                options = {}
            processor = SessionProcessor(config, options)
            result = run_job(session_dir, processor)
            print(
                f"{session_dir.name}: {result.state} "
                f"(attempt {result.attempts})"
            )
            if result.state != "complete":
                exit_code = 2
            elif config.delete_audio_after_transcription:
                delete_audio_if_clean(session_dir)
        return exit_code


@contextmanager
def worker_lock(recordings_dir: Path):
    lock_path = recordings_dir / ".worker.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> int:
    load_env_files()
    args = parse_args()

    if args.command in {"llm-run", "llm-key-verify"}:
        # stdout is the protocol channel: whatever else prints (a library, a
        # stray print) is sent to stderr so the app never reads it as JSON.
        protocol_out = sys.stdout
        with contextlib.redirect_stdout(sys.stderr):
            if args.command == "llm-run":
                return run_llm_command(args, sys.stdin, protocol_out)
            return key_verify_command(args, sys.stdin, protocol_out)

    config = load_config(args.config)

    if args.command == "show-config":
        print(
            json.dumps(
                {
                    "recordings_dir": str(config.recordings_dir),
                    "on_stop": config.on_stop,
                    "delete_audio_after_transcription": (
                        config.delete_audio_after_transcription
                    ),
                    "transcription": vars(config.transcription),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "recordings-dir":
        print(config.recordings_dir)
        return 0
    if args.command == "delete-audio":
        enabled = config.delete_audio_after_transcription
        if args.state is not None:
            enabled = args.state == "on"
            set_config_value(
                args.config, "delete_audio_after_transcription", enabled
            )
        print("on" if enabled else "off")
        return 0
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "startup-check":
        return startup_check_command(args.config, sys.stdout)
    if args.command == "auto-summary":
        return auto_summary_command(args, sys.stdout)
    if args.command == "recover":
        recovered = recover_sessions(
            config.recordings_dir,
            max_attempts=config.transcription.max_attempts,
        )
        for session in recovered:
            print(f"Recovered: {session}")
        print(f"Recovered sessions: {len(recovered)}")
        return 0
    if args.command in {"enqueue", "process"}:
        enqueue_session(
            args.session,
            max_attempts=config.transcription.max_attempts,
            force=args.force,
            options=requested_options(args),
        )
        print(f"Queued: {args.session}")
        if args.command == "enqueue":
            return 0
        return process_queue(
            config_path=args.config,
            requested=args.session,
        )
    if args.command == "worker":
        return process_queue(
            config_path=args.config,
            requested=args.session,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
