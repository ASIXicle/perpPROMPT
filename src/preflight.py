"""Pre-flight sanity check for perpPROMPT cycle runners.

Catches the class of bugs the May 27 dry-run debugging surfaced:
template KeyError from missing slots, module import failures from
wrong type annotations, etc. Designed to be cheap — no llama-server
calls, no ChromaDB writes, no MCP network calls.

What it checks:

1. Every cycle module imports cleanly (think, dream, digest,
   scaffold_bootstrap). Catches type-annotation bugs that crash
   at module-load time.
2. Every template renders against a stub context with every slot
   the corresponding build_*_context function provides. Catches
   KeyError leaks (the {date} slot bug that broke the first dry-run).
3. tests/dry_run.sh exists and is executable.
4. systemd/ unit files exist and are syntactically well-formed
   (basic [Unit]/[Service]/[Install] section presence).
5. (Optional, --check-services) llama-server chat and embedding
   ports respond to a TCP connect. No actual inference, just
   reachability.

Returns 0 on success, non-zero per failure mode (1=template,
2=import, 3=wrapper, 4=systemd, 5=service-port).

CLI:
  python -m src.preflight                   # all checks except --check-services
  python -m src.preflight --check-services  # also probe :8080 and :8081

Intended invocation:
  - Manually before naming ceremony
  - Optionally via systemd ExecStartPre= on cycle services for
    continuous drift catching (slightly slows each cycle start)
  - In CI if/when CI is added

Phase C.3. No deps on cycle runners themselves — imports them only
to verify they import.
"""

import argparse
import importlib
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config


logger = logging.getLogger(__name__)


# Exit codes
EXIT_SUCCESS = 0
EXIT_TEMPLATE_ERROR = 1
EXIT_IMPORT_ERROR = 2
EXIT_WRAPPER_ERROR = 3
EXIT_SYSTEMD_ERROR = 4
EXIT_SERVICE_PORT_ERROR = 5


# =============================================================================
# Check: module imports
# =============================================================================

CYCLE_MODULES = ["think", "dream", "digest", "scaffold_bootstrap", "context",
                 "config", "mcp_client", "llama_client"]


def check_imports() -> list[str]:
    """Try to import every cycle module. Returns list of error strings."""
    errors = []
    for mod_short in CYCLE_MODULES:
        mod_full = f"src.{mod_short}"
        try:
            importlib.import_module(mod_full)
            logger.debug("import OK: %s", mod_full)
        except Exception as e:
            errors.append(f"{mod_full} import failed: {type(e).__name__}: {e}")
    return errors


# =============================================================================
# Check: template rendering
# =============================================================================

def _stub_context_think() -> dict:
    """Stub values matching build_think_context's return shape."""
    return {
        "agent_name": "testbird",
        "bootstrap_identity": "[stub identity]",
        "project_focus": "[stub focus]",
        "last_N_amq": "(no unread messages)",
        "last_M_memories": "(no memories yet)",
        "date": datetime.now(timezone.utc).date().isoformat(),
    }


def _stub_context_dream() -> dict:
    """Stub values matching build_dream_context's return shape (minus side-channels)."""
    return {
        "agent_name": "testbird",
        "ancient_memory": "(stub ancient)",
        "recent_memory": "(stub recent)",
        "random_memory": "(stub random)",
        "news_item": "(stub news)",
        "amq_fragment": "(stub fragment)",
    }


def _stub_context_dream_free() -> dict:
    """Stub values matching build_dream_free_context (no agent_name slot)."""
    return {
        "ancient_memory": "(stub ancient)",
        "recent_memory": "(stub recent)",
        "random_memory": "(stub random)",
        "news_item": "(stub news)",
        "amq_fragment": "(stub fragment)",
    }


def check_templates() -> list[str]:
    """Render every template with stub context. Returns list of errors."""
    errors = []

    template_checks = [
        (config.THINK_TEMPLATE, _stub_context_think(), "think.md"),
        (config.DREAM_TEMPLATE, _stub_context_dream(), "dream.md"),
        (config.DREAM_FREE_TEMPLATE, _stub_context_dream_free(), "dream.free.md"),
    ]

    for path, stub, name in template_checks:
        if not path.exists():
            errors.append(f"template missing: {path}")
            continue
        try:
            text = path.read_text()
            rendered = text.format(**stub)
            logger.debug("template OK: %s rendered to %d chars", name, len(rendered))
        except KeyError as e:
            errors.append(
                f"template {name} has unfilled slot: {e}. "
                f"Either add it to the corresponding build_*_context function "
                f"or escape the brace pair as {{{{...}}}} in the template."
            )
        except Exception as e:
            errors.append(f"template {name} render failed: {type(e).__name__}: {e}")

    return errors


# =============================================================================
# Check: tests/dry_run.sh
# =============================================================================

def check_wrapper() -> list[str]:
    """Verify tests/dry_run.sh exists and is executable."""
    errors = []
    wrapper = config.REPO_ROOT / "tests" / "dry_run.sh"
    if not wrapper.exists():
        errors.append(f"dry-run wrapper missing: {wrapper}")
    elif not wrapper.is_file():
        errors.append(f"dry-run wrapper is not a file: {wrapper}")
    else:
        mode = wrapper.stat().st_mode
        if not (mode & 0o111):
            errors.append(
                f"dry-run wrapper not executable: {wrapper} (mode {oct(mode)}). "
                f"Run: chmod +x {wrapper}"
            )
    return errors


# =============================================================================
# Check: systemd unit files
# =============================================================================

REQUIRED_UNITS = [
    "perpprompt-thinking.service",
    "perpprompt-thinking.timer",
    "perpprompt-dreaming.service",
    "perpprompt-dreaming.timer",
    "perpprompt-digest.service",
    "perpprompt-digest.timer",
]


def check_systemd_units() -> list[str]:
    """Verify systemd unit files exist and have basic structure."""
    errors = []
    units_dir = config.REPO_ROOT / "systemd"
    if not units_dir.exists():
        return [f"systemd/ directory missing at {units_dir}"]

    for name in REQUIRED_UNITS:
        path = units_dir / name
        if not path.exists():
            errors.append(f"unit file missing: {name}")
            continue
        text = path.read_text()
        if name.endswith(".service"):
            if "[Service]" not in text or "[Unit]" not in text:
                errors.append(f"unit {name} missing [Unit] or [Service] section")
            if "ExecStart=" not in text:
                errors.append(f"service unit {name} has no ExecStart=")
        elif name.endswith(".timer"):
            if "[Timer]" not in text or "[Unit]" not in text:
                errors.append(f"unit {name} missing [Unit] or [Timer] section")
            if "Unit=" not in text:
                errors.append(f"timer unit {name} has no Unit= directive")

    return errors


# =============================================================================
# Check: optional, service-port reachability
# =============================================================================

def check_service_ports(timeout_sec: float = 2.0) -> list[str]:
    """TCP-connect to llama-server chat (8080) and embedding (8081) ports.

    No HTTP, no actual inference call — just a connect to verify the
    services are up. Slow networks tolerated via timeout_sec.
    """
    errors = []
    ports_to_check = [
        ("llama-server chat", "127.0.0.1", 8080),
        ("llama-server embedding", "127.0.0.1", 8081),
    ]

    for label, host, port in ports_to_check:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        try:
            sock.connect((host, port))
            logger.debug("%s reachable at %s:%d", label, host, port)
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            errors.append(
                f"{label} unreachable at {host}:{port} ({type(e).__name__}: {e}). "
                f"Cycles will fail. Start with: systemctl start "
                f"llama-server.service llama-server-embedding.service"
            )
        finally:
            sock.close()

    return errors


# =============================================================================
# Top-level orchestration
# =============================================================================

def run_all_checks(check_services: bool = False) -> int:
    """Run every check, log results, return exit code (0 = all pass).

    Each check produces a list of error strings. We collect them, log
    them, and return the FIRST non-zero exit code that fired. (Could
    be made to OR multiple exit codes but that loses signal.)
    """
    all_pass = True
    first_exit_code = EXIT_SUCCESS

    logger.info("=" * 60)
    logger.info("perpPROMPT pre-flight check")
    logger.info("=" * 60)

    checks = [
        ("module imports", check_imports, EXIT_IMPORT_ERROR),
        ("template rendering", check_templates, EXIT_TEMPLATE_ERROR),
        ("dry-run wrapper", check_wrapper, EXIT_WRAPPER_ERROR),
        ("systemd unit files", check_systemd_units, EXIT_SYSTEMD_ERROR),
    ]
    if check_services:
        checks.append(("service port reachability", check_service_ports, EXIT_SERVICE_PORT_ERROR))

    for label, check_fn, exit_code in checks:
        logger.info("Checking: %s ...", label)
        errors = check_fn()
        if errors:
            all_pass = False
            for err in errors:
                logger.error("  FAIL: %s", err)
            if first_exit_code == EXIT_SUCCESS:
                first_exit_code = exit_code
        else:
            logger.info("  PASS")

    logger.info("=" * 60)
    if all_pass:
        logger.info("ALL PRE-FLIGHT CHECKS PASSED")
        return EXIT_SUCCESS
    else:
        logger.error("PRE-FLIGHT FAILED — fix above issues before running cycles")
        return first_exit_code


def main():
    parser = argparse.ArgumentParser(
        description="Run pre-flight sanity checks for perpPROMPT cycle runners.",
    )
    parser.add_argument(
        "--check-services",
        action="store_true",
        help="Also probe llama-server chat (8080) and embedding (8081) port reachability.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    sys.exit(run_all_checks(check_services=args.check_services))


if __name__ == "__main__":
    main()
