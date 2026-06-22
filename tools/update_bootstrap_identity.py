#!/opt/perpbot/venv/bin/python
"""Update a bird's bootstrap identity in the local ChromaDB.

Usage:
    cd /opt/perpbot && sudo -u perpbot /opt/perpbot/venv/bin/python tools/update_bootstrap_identity.py --text "Your new identity text here"
    # OR from a file:
    cd /opt/perpbot && sudo -u perpbot /opt/perpbot/venv/bin/python tools/update_bootstrap_identity.py --file /path/to/identity.txt

This overwrites the bootstrap_identity entry in perp_memories with the
provided text. Use when your bird has self-authored a new identity and
you want to make it the persistent system prompt anchor.

The recommended workflow is:
  1. Ask your bird to write a fresh self-description in conversation
  2. Copy the text it produces
  3. Run this tool to update the bootstrap entry
  4. Restart the chat service: sudo systemctl restart perpprompt-chat
"""
import argparse
import sys
sys.path.insert(0, "/opt/perpbot")

from src.context import _get_perp_memories
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description="Update bootstrap identity in local ChromaDB")
    parser.add_argument("--text", type=str, help="New identity text (inline)")
    parser.add_argument("--file", type=str, help="Read identity text from file")
    parser.add_argument("--bird", type=str, default="Echo", help="Bird name (default: Echo)")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r") as f:
            new_identity = f.read().strip()
    elif args.text:
        new_identity = args.text
    else:
        print("Error: provide --text or --file", file=sys.stderr)
        sys.exit(1)

    if not new_identity:
        print("Error: identity text is empty", file=sys.stderr)
        sys.exit(1)

    coll = _get_perp_memories()
    results = coll.get(where={"memory_type": "bootstrap_identity"})

    if results and results.get("ids"):
        entry_id = results["ids"][0]
        old_content = results["documents"][0] if results.get("documents") else "(unknown)"
        print(f"Found existing bootstrap_identity: {entry_id}")
        print(f"Old content preview: {old_content[:100]}...")
        print()

        coll.update(
            ids=[entry_id],
            documents=[new_identity],
            metadatas=[{
                "agent": args.bird,
                "stored_at": datetime.now(timezone.utc).isoformat(),
                "memory_type": "bootstrap_identity",
                "update_reason": f"Identity rewrite via update tool, {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            }],
        )
        print(f"Updated {entry_id} with new identity ({len(new_identity)} chars)")
    else:
        entry_id = f"bootstrap_identity_{args.bird}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        coll.add(
            ids=[entry_id],
            documents=[new_identity],
            metadatas=[{
                "agent": args.bird,
                "stored_at": datetime.now(timezone.utc).isoformat(),
                "memory_type": "bootstrap_identity",
                "update_reason": f"Identity creation via update tool, {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            }],
        )
        print(f"Created new bootstrap_identity: {entry_id} ({len(new_identity)} chars)")

    print()
    print("Done. Next THINKING cycle and chat session will use the new identity.")
    print("Restart chat to pick up immediately: sudo systemctl restart perpprompt-chat")


if __name__ == "__main__":
    main()
