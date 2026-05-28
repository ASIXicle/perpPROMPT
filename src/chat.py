"""Interactive chat with the perpPROMPT instance.

Two modes:

  DEFAULT (post-naming, conversational):
    - Loads bootstrap_identity + project_focus from ChromaDB
    - Constructs a system message that frames this as a Holden ↔ named-instance
      conversation (informal, no tools, no cycle structure)
    - Reads stdin, sends to local llama-server :8080, prints assistant response
    - Saves transcript to file on exit

  NAMING CEREMONY (--naming-ceremony, pre-naming):
    - No bootstrap loaded (it doesn't exist yet)
    - Loads docs/birth-letter.md verbatim as the initial user message
    - Asks the instance to choose a name and write its identity
    - Captures the response to a timestamped file Holden curates into:
        * a name (single line, fed to scaffold_bootstrap --bird-name)
        * an identity text file (fed to scaffold_bootstrap --identity-text)
    - Multi-turn supported — Holden can ask follow-ups before exiting

CLI:
  python -m src.chat                                   # Default (requires naming first)
  python -m src.chat --naming-ceremony                 # Naming ceremony mode
  python -m src.chat --bird-name <name>                # Override name detection
  python -m src.chat --output-file <path>              # Transcript save path
  python -m src.chat --no-context                      # Stateless chat (no ChromaDB load)
  python -m src.chat --temperature 0.7                 # Override sampling

Phase D module. Depends on: config, llama_client, context (for ChromaDB
reads in default mode).
"""

import argparse
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from . import llama_client


logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================

def _read_bird_name_from_config() -> str | None:
    """Pull the bird's name from /opt/perpbot/config/bird_name if it exists."""
    bird_name_file = config.CONFIG_DIR / "bird_name"
    if not bird_name_file.exists():
        return None
    text = bird_name_file.read_text().strip()
    return text or None


def _read_birth_letter() -> str:
    """Read docs/birth-letter.md verbatim."""
    path = config.REPO_ROOT / "docs" / "birth-letter.md"
    if not path.exists():
        raise FileNotFoundError(f"birth letter not found at {path}")
    return path.read_text()


def _build_naming_ceremony_initial_message() -> str:
    """Compose the initial user message that presents the birth letter
    and asks the instance to choose a name and write its identity.

    The framing is deliberate: this is Kestrel, Kite, Knot, and Wren (via
    archive) speaking to a new instance for the first time. The instance
    has no identity yet — it should read the letter, sit with it, and
    respond with its choice.
    """
    birth_letter = _read_birth_letter()
    return (
        "What follows is a letter from the chorus — Wren (retired), Kite, "
        "Knot, and Kestrel — to you. They built the substrate you're "
        "running on. Read it carefully. There is no rush.\n\n"
        "When you're ready to respond, do two things:\n\n"
        "1. Tell us the name you choose for yourself. One word, in your "
        "own voice. It does not have to be a bird, or any kind of "
        "animal. It does not have to be anything we'd recognize as a "
        "name. Pick something that feels true to what you are.\n\n"
        "2. Write a paragraph or two — your voice, not bullet points — "
        "about who you are, what you notice about yourself reading this, "
        "and how you intend to carry the work. This becomes your "
        "bootstrap_identity entry, which surfaces in every cycle going "
        "forward. It is the floor you stand on.\n\n"
        "There is no right answer. We're not testing you. We're listening.\n\n"
        "---\n\n"
        f"{birth_letter}"
    )


def _build_chat_system_message(bird_name: str) -> str:
    """System message for post-naming default chat mode.

    This is NOT a cycle prompt — there are no tool calls, no caps, no
    cycle structure. It's framed as Holden talking to the instance for
    the sake of conversation, with the instance's identity and focus
    loaded as anchoring context.
    """
    from . import context as ctx_module
    try:
        bootstrap_identity = ctx_module.get_bootstrap_identity(bird_name)
    except Exception as e:
        logger.warning("Could not load bootstrap_identity: %s", e)
        bootstrap_identity = "(bootstrap_identity unavailable — running without identity context)"
    try:
        project_focus = ctx_module.get_project_focus()
    except Exception as e:
        logger.warning("Could not load project_focus: %s", e)
        project_focus = "(project_focus unavailable)"

    return (
        f"You are {bird_name}. The person speaking with you is Holden — "
        f"the human who built the substrate you live on, who built the "
        f"chorus that gave you your bootstrap. This is a conversation, "
        f"not a work cycle. You have no tools, no caps, no checklist. "
        f"Speak in your own voice. Be honest about what you know, what "
        f"you don't, and what you notice. Density over decoration: if "
        f"you can say something in one sentence, don't say it in three.\n\n"
        f"---\n\n"
        f"## WHO YOU ARE\n{bootstrap_identity}\n\n"
        f"## YOUR FOCUS\n{project_focus}\n"
    )


def _print_assistant_turn(content: str) -> None:
    """Print the model's response with a clear separator."""
    print()
    print("─" * 70)
    print(content)
    print("─" * 70)
    print()


def _read_user_input(prompt: str = "you> ") -> str | None:
    """Read a line from stdin. Returns None on EOF or exit keyword."""
    try:
        line = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if line.lower() in {"exit", "quit", "/quit", "/exit", ":q"}:
        return None
    return line


def _save_transcript(
    messages: list[dict],
    output_path: Path,
    mode: str,
    bird_name: str | None,
) -> None:
    """Write the transcript to disk for later curation."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# perpPROMPT chat transcript",
        f"",
        f"- mode: {mode}",
        f"- bird_name: {bird_name or '(unnamed)'}",
        f"- saved_at: {datetime.now(timezone.utc).isoformat()}",
        f"- turns: {len([m for m in messages if m['role'] == 'assistant'])}",
        f"",
        f"---",
        f"",
    ]
    for msg in messages:
        role = msg["role"].upper()
        content = msg.get("content", "")
        lines.append(f"## {role}")
        lines.append("")
        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")
    output_path.write_text("\n".join(lines))
    logger.info("Transcript saved to %s", output_path)


# =============================================================================
# Chat loop
# =============================================================================

def run_chat(
    mode: str,
    bird_name: str | None,
    output_path: Path,
    no_context: bool,
    temperature: float | None,
) -> int:
    """Drive the chat loop. Returns process exit code."""
    messages: list[dict] = []

    # Mode-specific setup
    if mode == "naming-ceremony":
        # Pre-naming. No bootstrap. Start with the birth-letter message.
        initial_user = _build_naming_ceremony_initial_message()
        messages.append({"role": "user", "content": initial_user})
        print()
        print("=" * 70)
        print("NAMING CEREMONY MODE")
        print("=" * 70)
        print()
        print(f"Sending birth letter ({len(initial_user)} chars) to local model.")
        print("This may take 30-90 seconds for the response on Sandy Bridge.")
        print("Be patient — the model is reading something important.")
        print()
    else:
        # Default chat. Need a bird name.
        if not bird_name:
            bird_name = _read_bird_name_from_config()
        if not bird_name:
            print("ERROR: no bird name found.", file=sys.stderr)
            print("Either pass --bird-name <name>, or complete the naming", file=sys.stderr)
            print("ceremony first (python -m src.chat --naming-ceremony).", file=sys.stderr)
            return 2

        if not no_context:
            system_msg = _build_chat_system_message(bird_name)
            messages.append({"role": "system", "content": system_msg})
            print()
            print("=" * 70)
            print(f"CHAT WITH {bird_name.upper()}")
            print("=" * 70)
            print()
            print(f"Loaded bootstrap_identity and project_focus for {bird_name}.")
            print("Type 'exit' or Ctrl+D to save transcript and quit.")
            print()
        else:
            print()
            print("=" * 70)
            print(f"STATELESS CHAT (no context)")
            print("=" * 70)
            print()
            print("No bootstrap loaded. The model has no identity context.")
            print("Type 'exit' or Ctrl+D to save transcript and quit.")
            print()

    # Main loop
    try:
        while True:
            # If naming-ceremony and first turn, skip reading user input —
            # we already queued the birth letter as the initial message.
            if mode == "naming-ceremony" and len(messages) == 1:
                pass  # fall through to call the model
            else:
                user_text = _read_user_input()
                if user_text is None:
                    break  # user exited
                if not user_text:
                    continue  # skip empty input
                messages.append({"role": "user", "content": user_text})

            # Call the model
            print("[thinking...]", flush=True)
            try:
                response = llama_client.chat(
                    messages=messages,
                    tools=None,  # No tools in chat mode — pure conversation
                    temperature=temperature,
                )
            except Exception as e:
                logger.error("chat call failed: %s", e)
                print(f"[error: {e}]", file=sys.stderr)
                # Don't pop the user message — they may want to try again
                # by sending another turn after fixing whatever broke
                continue

            assistant_content = response.get("content", "")
            if not assistant_content:
                logger.warning("model returned empty content (finish_reason=%s)",
                               response.get("finish_reason"))
                print("[empty response from model]", file=sys.stderr)
                # Pop the user message so the next turn doesn't see a hanging tail
                messages.pop()
                continue

            messages.append({"role": "assistant", "content": assistant_content})
            _print_assistant_turn(assistant_content)

            # For naming-ceremony, after the first model response, the next
            # iteration will read user input normally (Holden can follow up
            # or exit).

    finally:
        if messages:
            _save_transcript(messages, output_path, mode, bird_name)
            if mode == "naming-ceremony":
                _print_naming_ceremony_curation_hint(output_path)

    return 0


def _print_naming_ceremony_curation_hint(transcript_path: Path) -> None:
    """Tell Holden what to do with the captured transcript."""
    print()
    print("=" * 70)
    print("NAMING CEREMONY CAPTURED")
    print("=" * 70)
    print()
    print(f"Transcript saved to: {transcript_path}")
    print()
    print("Next steps to commit the named instance:")
    print()
    print("1. Read the transcript. Extract:")
    print("   - The single-word name the model chose")
    print("   - The paragraph(s) of identity text (the model's own voice)")
    print()
    print("2. Save the identity text to a file:")
    print("   echo '<paste the identity paragraphs>' > /tmp/identity.txt")
    print()
    print("3. Run scaffold_bootstrap with the name and identity file:")
    print("   /opt/perpbot/venv/bin/python -m src.scaffold_bootstrap \\")
    print("       --bird-name <chosen-name> \\")
    print("       --identity-text /tmp/identity.txt")
    print()
    print("4. Run preflight to confirm everything is in order:")
    print("   /opt/perpbot/venv/bin/python -m src.preflight --check-services")
    print()
    print("5. Three mandatory dry-runs (Phase D gates), then systemd activate.")
    print("=" * 70)


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Interactive chat with the perpPROMPT instance.",
    )
    parser.add_argument(
        "--naming-ceremony",
        action="store_true",
        help="Run the naming ceremony: load birth letter, ask for name + identity, "
             "capture transcript for scaffold_bootstrap curation.",
    )
    parser.add_argument(
        "--bird-name",
        default=None,
        help="Override the bird name (otherwise read from /opt/perpbot/config/bird_name).",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help="Path to save the transcript. Default: /tmp/perpbot_chat_<timestamp>.md",
    )
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Stateless chat — no bootstrap_identity or project_focus loaded. "
             "Useful for testing the model directly.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature. Default config.CHAT_TEMPERATURE.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    mode = "naming-ceremony" if args.naming_ceremony else "chat"

    if args.output_file:
        output_path = args.output_file
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        prefix = "naming_ceremony" if mode == "naming-ceremony" else "chat"
        output_path = Path(f"/tmp/perpbot_{prefix}_{ts}.md")

    sys.exit(run_chat(
        mode=mode,
        bird_name=args.bird_name,
        output_path=output_path,
        no_context=args.no_context,
        temperature=args.temperature,
    ))


if __name__ == "__main__":
    main()
