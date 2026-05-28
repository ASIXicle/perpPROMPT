#!/usr/bin/env bash
# Maximum-isolation dry-run wrapper for perpPROMPT cycle runners.
#
# Holden's "we get one chance to give birth to our dreamer" directive
# (2026-05-27) — dry-run cycles must not touch the real bird's
# ChromaDB substrate. Not the dream collection, not the memory
# collection, not even the auto-created empty placeholders that
# normally appear on first ChromaDB access.
#
# This wrapper:
#   1. Creates a fresh mktemp -d ChromaDB sandbox
#   2. Exports PERPBOT_CHROMADB_PATH so config.py uses the sandbox
#   3. Invokes the requested cycle runner with --dry-run forced on
#   4. Wipes the sandbox on exit (success OR crash via EXIT trap)
#
# After this wrapper finishes, /opt/perpbot/chromadb/ is exactly what
# it was before the test ran. The naming ceremony bird wakes up to an
# empty world like it should.
#
# Usage:
#   tests/dry_run.sh think  --bird-name testbird [--log-level DEBUG]
#   tests/dry_run.sh dream  --bird-name testbird [--free] [--log-level DEBUG]
#   tests/dry_run.sh digest --bird-name testbird [--log-level DEBUG]
#
# First arg = cycle name. Remaining args pass through to the Python
# module's argparse. --dry-run is added automatically — don't worry
# about it.

set -euo pipefail

# -----------------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------------

if [ $# -lt 1 ]; then
    echo "usage: $0 <think|dream|digest> [args...]" >&2
    echo "" >&2
    echo "examples:" >&2
    echo "  $0 think  --bird-name testbird --log-level DEBUG" >&2
    echo "  $0 dream  --bird-name testbird --log-level DEBUG" >&2
    echo "  $0 dream  --bird-name testbird --free --log-level DEBUG" >&2
    echo "  $0 digest --bird-name testbird --log-level DEBUG" >&2
    exit 1
fi

CYCLE="$1"
shift

case "$CYCLE" in
    think|dream|digest) ;;
    *)
        echo "error: unknown cycle '$CYCLE'" >&2
        echo "expected one of: think, dream, digest" >&2
        exit 1
        ;;
esac

# -----------------------------------------------------------------------------
# Sandbox setup
# -----------------------------------------------------------------------------

# mktemp -d in /tmp guarantees: unique name, fresh empty dir, world-readable
# permissions (so the perpbot service user can write into it). The XXXXXX
# template ensures uniqueness even on rapid repeated invocations.
TEMP_CHROMADB=$(mktemp -d -t perpbot-test-chromadb-XXXXXX)

# Cleanup runs on ANY exit: success, error, SIGINT (Ctrl-C), SIGTERM.
# rm -rf is fine — the path always points at /tmp/perpbot-test-chromadb-*
# which we just created, so there's no risk of clobbering anything real.
cleanup() {
    local exit_code=$?
    echo ""
    echo "[isolation] cleaning up sandbox: $TEMP_CHROMADB"
    rm -rf "$TEMP_CHROMADB"
    echo "[isolation] sandbox removed. Real ChromaDB at /opt/perpbot/chromadb/ untouched."
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

# Make the sandbox writable by the perpbot service user (we're going to
# invoke as them via sudo -u perpbot).
chmod 777 "$TEMP_CHROMADB"

# -----------------------------------------------------------------------------
# Banner
# -----------------------------------------------------------------------------

echo "============================================================"
echo "[isolation] perpPROMPT dry-run wrapper"
echo "[isolation] cycle:         $CYCLE"
echo "[isolation] sandbox path:  $TEMP_CHROMADB"
echo "[isolation] real path:     /opt/perpbot/chromadb/  (UNTOUCHED)"
echo "[isolation] args:          $*"
echo "[isolation] --dry-run is added automatically"
echo "============================================================"
echo ""

# -----------------------------------------------------------------------------
# Invocation
# -----------------------------------------------------------------------------

# Run as perpbot user (matches production execution context). Pass the
# env var explicitly via `env` since sudo by default scrubs the env.
# cd to /opt/perpbot so uv/python find their roots correctly.
cd /opt/perpbot
sudo -u perpbot env \
    PERPBOT_CHROMADB_PATH="$TEMP_CHROMADB" \
    /opt/perpbot/venv/bin/python -m "src.$CYCLE" --dry-run "$@"

# Exit code from the python invocation propagates via set -e + trap.
