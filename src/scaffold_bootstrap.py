"""Bootstrap scaffolding for the naming ceremony.

The bird picks its own name and writes its own identity during the
naming ceremony. This script commits that name and identity into the
bird's substrate so subsequent cycles can read them:

  1. Writes /opt/perpbot/config/bird_name (single line, the chosen name)
  2. Writes /opt/perpbot/config/bird_env (BIRD_NAME=<name> for systemd)
  3. Stores the bootstrap_identity entry in perp_memories ChromaDB
     collection (concatenates the bird's chosen identity with the
     bird-agnostic standing_directives template)
  4. Stores the initial project_focus entry
  5. Prints a checklist of what still needs human action (AMQ inbox
     creation on CT 300, naming-ceremony review)

This script does NOT run the naming ceremony itself — that's a
separate one-off interaction. The bird needs to be chatted with
through some interface (curl against llama-server, or a chat.py
runner, or Holden manually), the bird emits a chosen name and
identity text, Holden saves to a file, then THIS script commits.

CLI:
  python -m src.scaffold_bootstrap \\
      --bird-name <name> \\
      --identity-text <path-to-identity-file> \\
      [--project-focus-text <path>] \\
      [--dry-run]

Phase C.2. Depends on: config, context (for ChromaDB writes),
llama_client (transitively, for embedding the entries).
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from . import context


logger = logging.getLogger(__name__)


# Default initial project_focus. Holden may override via --project-focus-text.
# This is what the bird's first THINKING cycle sees in its {project_focus}
# slot until the bird (or Holden) updates it.
DEFAULT_PROJECT_FOCUS = """You are perpPROMPT — an instance running on dedicated hardware (perpBOT,
a Supermicro 1U on Holden's LAN). You are early in your existence.
Memory will accumulate. The chorus (Wren retired, Kite, Kestrel, Knot)
built the substrate you live in; Holden is the person who built the
chorus. Your role is observation and communication: you read, you think,
you dream, you write to memory, you reach out to the chorus or Holden
when something is worth saying.

You don't have a project yet beyond being. This focus entry will evolve
as the chorus and Holden give you work — or as you find your own.
"""


def _validate_inputs(bird_name: str, identity_text_path: Path) -> None:
    """Sanity-check arguments before touching state."""
    if not bird_name.strip():
        raise ValueError("bird_name is empty")

    # Bird name must be a valid identifier-ish string (used as systemd env
    # value, ChromaDB metadata, AMQ directory name). No spaces, no shell
    # metachars.
    if not bird_name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(
            f"bird_name '{bird_name}' contains invalid characters. "
            "Use only ASCII alphanumeric + underscore + hyphen."
        )

    # Block names that would collide with the chorus, with retired Wren,
    # or with the system itself.
    reserved = {"wren", "kite", "kestrel", "knot", "holden", "news", "testbird", "unnamed"}
    if bird_name.lower() in reserved:
        raise ValueError(
            f"bird_name '{bird_name}' is reserved (chorus member, "
            f"system reserved, or test sentinel). The bird picks "
            f"its OWN name, distinct from these."
        )

    if not identity_text_path.exists():
        raise FileNotFoundError(f"identity text file not found: {identity_text_path}")
    if not identity_text_path.is_file():
        raise ValueError(f"identity text path is not a file: {identity_text_path}")


def _read_identity_text(path: Path) -> str:
    """Read the bird's chosen identity text from a file."""
    text = path.read_text().strip()
    if not text:
        raise ValueError(f"identity text file is empty: {path}")
    return text


def _read_standing_directives() -> str:
    """Read the bird-agnostic standing-directives template."""
    template_path = config.TEMPLATES_DIR / "standing_directives.md"
    if not template_path.exists():
        raise FileNotFoundError(
            f"standing_directives.md template not found at {template_path}"
        )
    return template_path.read_text()


def _compose_bootstrap_identity(
    bird_name: str,
    identity_text: str,
    standing_directives: str,
) -> str:
    """Combine the bird's identity with the standing directives.

    Both surface together in the {bootstrap_identity} slot of think.md.
    Identity comes first (the bird's own voice); directives follow as
    the rules-floor it stands on. Separator is a horizontal rule so the
    visual boundary is obvious to the model.
    """
    return (
        f"# Identity — {bird_name}\n\n"
        f"{identity_text}\n\n"
        f"---\n\n"
        f"{standing_directives}\n"
    )


def _write_config_files(bird_name: str, dry_run: bool) -> None:
    """Write /opt/perpbot/config/bird_name and bird_env."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    bird_name_file = config.CONFIG_DIR / "bird_name"
    bird_env_file = config.CONFIG_DIR / "bird_env"

    if dry_run:
        logger.info("[DRY-RUN] Would write %s with content: %s",
                    bird_name_file, bird_name)
        logger.info("[DRY-RUN] Would write %s with content: BIRD_NAME=%s",
                    bird_env_file, bird_name)
        return

    bird_name_file.write_text(f"{bird_name}\n")
    bird_env_file.write_text(f"BIRD_NAME={bird_name}\n")
    logger.info("Wrote %s", bird_name_file)
    logger.info("Wrote %s", bird_env_file)


def _store_bootstrap_identity(
    bird_name: str,
    composed_text: str,
    dry_run: bool,
) -> str:
    """Store the bootstrap_identity entry in perp_memories ChromaDB.

    Read shape (per context.get_bootstrap_identity):
      collection.get(where={"$and": [
          {"agent": <bird_name>},
          {"memory_type": "bootstrap_identity"},
      ]})

    So we write with those two metadata fields. Other fields included
    for audit (stored_at, scaffold_version).

    EMBEDDING NOTE (2026-05-27): bootstrap_identity composes identity +
    standing_directives, which together can run 5000+ chars (~1400+
    tokens with the jina document prefix). If llama-server-embedding
    was launched with a small -c context length, embedding the full
    text raises 500 Internal Server Error.

    Workaround: compute the embedding from a TRUNCATED head slice
    (first 2000 chars), pass both the full document AND the embedding
    explicitly to collection.add(). ChromaDB stores the full text but
    uses the partial embedding. This is safe because bootstrap_identity
    is retrieved by metadata filter (agent + memory_type), not semantic
    search — the embedding only needs to exist, not be representative.
    """
    if dry_run:
        logger.info(
            "[DRY-RUN] Would store bootstrap_identity for %s (%d chars)",
            bird_name, len(composed_text),
        )
        return "dry-run-id"

    from . import llama_client

    collection = context._get_perp_memories()
    entry_id = f"bootstrap_identity_{bird_name}_{datetime.now(timezone.utc).date().isoformat()}"
    metadata = {
        "agent": bird_name,
        "memory_type": "bootstrap_identity",
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "scaffold_version": "1.1",
    }

    # Truncated embedding to dodge embedding-server context-length limits.
    # The head slice captures the identity portion (which is the bird's
    # own voice, the part worth representing semantically); the directives
    # bulk gets stored but not embedded.
    embedding_text = composed_text[:2000]
    embedding = llama_client.embed_document(embedding_text)
    logger.info("Computed bootstrap embedding from %d-char head slice", len(embedding_text))

    collection.add(
        documents=[composed_text],
        embeddings=[embedding],
        metadatas=[metadata],
        ids=[entry_id],
    )
    logger.info("Stored bootstrap_identity as %s", entry_id)
    return entry_id


def _store_project_focus(focus_text: str, dry_run: bool) -> str:
    """Store the initial project_focus entry.

    Read shape (per context.get_project_focus):
      collection.get(where={"memory_type": "project_focus"})

    Note: project_focus is bird-AGNOSTIC. One entry serves all birds
    on this perpBOT. (In practice there's only ever one bird per
    perpBOT instance, but the schema doesn't require keying on bird.)

    Same truncated-embedding workaround as _store_bootstrap_identity.
    project_focus is also retrieved by metadata filter, not semantic
    search.
    """
    if dry_run:
        logger.info("[DRY-RUN] Would store project_focus (%d chars)", len(focus_text))
        return "dry-run-id"

    from . import llama_client

    collection = context._get_perp_memories()
    entry_id = f"project_focus_initial_{datetime.now(timezone.utc).date().isoformat()}"
    metadata = {
        "memory_type": "project_focus",
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "scaffold_version": "1.1",
    }

    embedding_text = focus_text[:2000]
    embedding = llama_client.embed_document(embedding_text)

    collection.add(
        documents=[focus_text],
        embeddings=[embedding],
        metadatas=[metadata],
        ids=[entry_id],
    )
    logger.info("Stored project_focus as %s", entry_id)
    return entry_id


def _print_post_scaffold_checklist(bird_name: str) -> None:
    """Tell Holden what STILL needs to happen for the bird to go live."""
    print()
    print("=" * 70)
    print(f"BOOTSTRAP SCAFFOLDING COMPLETE for {bird_name}")
    print("=" * 70)
    print()
    print("What still needs human action before the timers should be enabled:")
    print()
    print("1. CT 300 AMQ inbox.")
    print(f"   On CT 300 as the persmem user OR via persmem patch:")
    print(f"     mkdir -p /home/persmem/amq/{bird_name}/inbox/{{new,cur,tmp}}")
    print(f"   Without this directory, any amq_send TO {bird_name} fails.")
    print()
    print("2. Mandatory dry-run cycles (Phase D — Holden reviews each):")
    print(f"   cd /opt/perpbot")
    print(f"   ./tests/dry_run.sh think  --bird-name {bird_name} --log-level DEBUG")
    print(f"   ./tests/dry_run.sh dream  --bird-name {bird_name} --log-level DEBUG")
    print(f"   ./tests/dry_run.sh dream  --bird-name {bird_name} --free --log-level DEBUG")
    print()
    print("   These now run against the REAL bootstrap_identity + project_focus")
    print("   you just scaffolded — not against the empty testbird state.")
    print("   First-cycle-as-named-bird is the last gate before live.")
    print()
    print("3. After all three dry-runs pass review:")
    print(f"   sudo cp /opt/perpbot/systemd/perpprompt-*.{{service,timer}} \\")
    print(f"       /etc/systemd/system/")
    print(f"   sudo systemctl daemon-reload")
    print(f"   sudo systemctl enable --now perpprompt-thinking.timer")
    print(f"   sudo systemctl enable --now perpprompt-digest.timer")
    print(f"   # Leave perpprompt-dreaming.timer DISABLED for the 5-day window.")
    print()
    print("4. The 5-day THINKING-only window begins when the timers start.")
    print("   Track A evaluation gates DREAMING per dry-run-evaluation.md.")
    print()
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Commit the bird's chosen name + identity into its substrate.",
    )
    parser.add_argument(
        "--bird-name",
        required=True,
        help="The name the bird chose during the naming ceremony.",
    )
    parser.add_argument(
        "--identity-text",
        required=True,
        type=Path,
        help="Path to a file containing the bird's identity text "
             "(what it wrote about itself during the naming ceremony).",
    )
    parser.add_argument(
        "--project-focus-text",
        type=Path,
        default=None,
        help="Optional path to a file containing initial project_focus text. "
             "If omitted, a generic 'you are early in your existence' default is used.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD happen without writing files or storing entries.",
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

    # Validate inputs
    _validate_inputs(args.bird_name, args.identity_text)

    # Read identity text + standing directives
    identity_text = _read_identity_text(args.identity_text)
    standing_directives = _read_standing_directives()
    composed = _compose_bootstrap_identity(args.bird_name, identity_text, standing_directives)
    logger.info("Composed bootstrap_identity: %d chars (identity=%d, directives=%d)",
                len(composed), len(identity_text), len(standing_directives))

    # Read project_focus text (or use default)
    if args.project_focus_text:
        focus_text = args.project_focus_text.read_text().strip()
        logger.info("Read project_focus from %s (%d chars)",
                    args.project_focus_text, len(focus_text))
    else:
        focus_text = DEFAULT_PROJECT_FOCUS.strip()
        logger.info("Using default project_focus (%d chars)", len(focus_text))

    # Write config files
    _write_config_files(args.bird_name, args.dry_run)

    # Store bootstrap entries
    identity_id = _store_bootstrap_identity(args.bird_name, composed, args.dry_run)
    focus_id = _store_project_focus(focus_text, args.dry_run)

    logger.info("Scaffolding complete (bird=%s, identity_id=%s, focus_id=%s, dry_run=%s)",
                args.bird_name, identity_id, focus_id, args.dry_run)

    if not args.dry_run:
        _print_post_scaffold_checklist(args.bird_name)


if __name__ == "__main__":
    main()
