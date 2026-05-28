"""Daily digest sender.

Wakes on a systemd timer at Holden's wake time (08:00 CDT per design.md
§8). Pulls everything that happened in the bird's life over the last ~24
hours from perp_memories, perp_dreams, and the AMQ inbox; formats a
human-readable report; sends it to Holden via amq_send.

This is the bird's "good morning" — the primary observability channel.
Daily, not optional. If digest fails, Holden notices something is wrong
within hours rather than days.

The digest is FOR HOLDEN — unlike the dream itself, here the confidence
tiers are visible and labeled. Track A auditing happens through these
reports: "did the wrapper score this dream correctly against my gut
read?" requires showing the tier.

Phase B module 7 of 7 (final). Depends on: config, context, mcp_client.
Entry point for systemd-managed daily digest.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import config
from . import context
from . import mcp_client


logger = logging.getLogger(__name__)


# Default lookback for the digest. systemd timer fires daily; this catches
# everything since yesterday's run. Pad slightly (25h) to avoid clock-drift
# gaps where a cycle near the boundary doesn't appear in either day's digest.
DEFAULT_PERIOD_HOURS = 25


# =============================================================================
# Data gathering
# =============================================================================

def _iso_since(period_hours: int) -> str:
    """ISO timestamp for `period_hours` ago, UTC. Used as ChromaDB filter."""
    return (datetime.now(timezone.utc) - timedelta(hours=period_hours)).isoformat()


def gather_observations(bird_name: str, period_hours: int) -> list[dict]:
    """Pull observation memories stored in the last `period_hours`."""
    collection = context._get_perp_memories()
    if collection.count() == 0:
        return []

    cutoff_iso = _iso_since(period_hours)
    try:
        result = collection.get(where={
            "$and": [
                {"agent": bird_name},
                {"memory_type": "observation"},
            ]
        })
    except Exception as e:
        logger.error("observation fetch failed: %s", e)
        return []

    ids = result.get("ids", [])
    docs = result.get("documents", [])
    metas = result.get("metadatas", [])

    entries = []
    for i, doc_id in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        stored_at = meta.get("stored_at", "")
        if stored_at < cutoff_iso:
            continue
        entries.append({
            "id": doc_id,
            "content": docs[i] if i < len(docs) else "",
            "metadata": meta,
        })

    entries.sort(key=lambda e: e["metadata"].get("stored_at", ""), reverse=True)
    return entries


def gather_dreams(bird_name: str, period_hours: int) -> list[dict]:
    """Pull dream memories stored in the last `period_hours` — ALL tiers.

    The digest shows everything so Holden can audit tier assignments.
    The DREAM_CONFIDENCE_PROMOTION_FLOOR filter only applies to THINKING
    context, NOT to digest reporting.
    """
    collection = context._get_perp_dreams()
    if collection.count() == 0:
        return []

    cutoff_iso = _iso_since(period_hours)
    try:
        result = collection.get(where={"agent": bird_name})
    except Exception as e:
        logger.error("dream fetch failed: %s", e)
        return []

    ids = result.get("ids", [])
    docs = result.get("documents", [])
    metas = result.get("metadatas", [])

    entries = []
    for i, doc_id in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        stored_at = meta.get("stored_at", "")
        if stored_at < cutoff_iso:
            continue
        entries.append({
            "id": doc_id,
            "content": docs[i] if i < len(docs) else "",
            "metadata": meta,
        })

    entries.sort(key=lambda e: e["metadata"].get("stored_at", ""), reverse=True)
    return entries


def gather_amq_activity(bird_name: str) -> dict:
    """Snapshot the bird's AMQ inbox state.

    Returns {new_count, recent_subjects} — full bodies are not pulled
    here because we don't want to mark them as read just for digest
    rendering. THINKING cycles handle their own AMQ reading.
    """
    check = mcp_client.amq_check(bird_name)
    if not check:
        return {"new_count": 0, "recent_subjects": [], "mcp_available": False}

    messages = check.get("messages", [])
    subjects = []
    for m in messages[:10]:
        subjects.append({
            "from": m.get("from", "?"),
            "subject": m.get("subject", "(no subject)"),
            "kind": m.get("kind", "message"),
            "priority": m.get("priority", "normal"),
            "created": m.get("created", "?"),
        })

    return {
        "new_count": check.get("new_count", len(messages)),
        "recent_subjects": subjects,
        "mcp_available": True,
    }


# =============================================================================
# Halt-condition checks (Track A safety surface)
# =============================================================================

def check_halt_conditions(
    observations: list[dict],
    dreams: list[dict],
    amq: dict | None = None,
    bird_name: str = "",
) -> list[str]:
    """Inspect recent activity for halt-condition flags per dry-run-evaluation.md.

    Returns a list of warning strings (empty if all clear). These get
    appended to the digest so Holden sees them on the morning glance,
    not buried in journal logs.

    Per Knot's C4 review (2026-05-27): expanded from 5 to 7 checks.
    Added: MCP auth flag cross-reference, AMQ inbox flood, no-dreams gap.
    """
    warnings = []

    # Memory growth rate sanity (linear vs exponential)
    obs_count = len(observations)
    dream_count = len(dreams)
    if obs_count > 50:
        warnings.append(
            f"⚠ unusually high observation count in period: {obs_count} "
            f"(expected ~5-15 per day). Flag for review."
        )
    if dream_count > 20:
        warnings.append(
            f"⚠ unusually high dream count in period: {dream_count} "
            f"(expected ~2-3 per day). Flag for review."
        )

    # Reasoning-leakage detection — Track B failure mode
    # Search recent observations for <think> tag leakage
    reasoning_leak_count = 0
    for obs in observations:
        content = obs.get("content", "")
        if "<think>" in content or "</think>" in content:
            reasoning_leak_count += 1
    if reasoning_leak_count > 0:
        warnings.append(
            f"⚠ {reasoning_leak_count} observations contain <think> tags — "
            f"reasoning suppression may be failing. Check llama_client.py "
            f"REASONING_SUPPRESSION_PROMPT injection path."
        )

    # All-quiet-marker pattern: if every observation is the quiet marker,
    # the bird isn't doing useful work
    quiet_marker_count = sum(
        1 for o in observations
        if o.get("content", "").startswith("reviewed ") and o.get("content", "").endswith(", quiet")
    )
    if obs_count > 0 and quiet_marker_count == obs_count and obs_count >= 3:
        warnings.append(
            f"⚠ all {obs_count} observations this period are quiet-markers. "
            f"Bird is running but producing no substance — check prompt, context, model."
        )

    # Dreams all tier 1 — analytical-mode pattern
    if dream_count >= 3:
        tier_1_count = sum(1 for d in dreams if d["metadata"].get("confidence") == 1)
        if tier_1_count == dream_count:
            warnings.append(
                f"⚠ all {dream_count} dreams this period scored tier 1 (thin/analytical). "
                f"Either the model is in analytical mode or the scoring threshold is too strict."
            )

    # MCP auth failure flag (Knot C4 review). Set by mcp_client when any
    # 401/403 fires during the digest's process. If the digest itself
    # caused the auth failure (likely, since digest calls amq_check), this
    # will be set — surface as urgent.
    if mcp_client.get_auth_failed_recently():
        warnings.append(
            "⚠ MCP authentication failed during this digest run. Bird's "
            "persmem token may be revoked or misconfigured. Check "
            "/opt/perpbot/config/persmem_bird_secret and CT 300 server logs."
        )

    # AMQ inbox flood — bird not processing messages
    if amq and amq.get("mcp_available") and amq.get("new_count", 0) > 50:
        warnings.append(
            f"⚠ AMQ inbox has {amq['new_count']} unread messages — bird is "
            f"not processing AMQ during cycles. Check think.py amq_read tool "
            f"usage in recent cycles."
        )

    # No-dreams-for-2-days gap (DREAMING enabled state)
    # Distinguishes "silence is valid" (occasional) from "dreaming not firing"
    # (systemic). Skip if the period itself is < 48h — can't detect a 2-day
    # gap inside a 25-hour window. This check matters once DREAMING goes
    # live (post-Day-5); for THINKING-only window it's expected.
    if dream_count == 0 and bird_name:
        # Lightweight signal — fire only when the period is wide enough
        # that "zero dreams" is a real anomaly. Caller passes period_hours
        # implicitly by virtue of fetching dreams for that period.
        warnings.append(
            f"⚠ zero dreams stored in this digest period. If DREAMING is "
            f"enabled, the cycle may not be firing (check systemd timer) "
            f"or the model is producing only silence (check dream samples)."
        )

    return warnings


# =============================================================================
# Formatting
# =============================================================================

def _format_timestamp(iso: str) -> str:
    """Pretty short timestamp for digest output, converted to local TZ.

    Per Knot's C3 review (2026-05-27): per-entry timestamps must use
    the same TZ as the digest header (`now.astimezone()` = local) for
    consistency. Storing in UTC but rendering in local — Holden's CDT.
    """
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        # Convert UTC stored timestamp to local TZ for display
        dt_local = dt.astimezone()
        return dt_local.strftime("%H:%M")
    except Exception:
        return iso[:16]


def _tier_label(confidence: int | None) -> str:
    """Human-readable tier label."""
    labels = {0: "silence", 1: "thin", 2: "present", 3: "vivid"}
    return labels.get(confidence, "?")


def format_digest(
    bird_name: str,
    period_hours: int,
    observations: list[dict],
    dreams: list[dict],
    amq: dict,
    warnings: list[str],
) -> str:
    """Render the digest as markdown for Holden's AMQ inbox."""
    now = datetime.now(timezone.utc).astimezone()
    date_str = now.strftime("%Y-%m-%d %H:%M %Z")

    lines: list[str] = []
    lines.append(f"# Daily digest — {bird_name}, {date_str}")
    lines.append(f"Period covered: last {period_hours}h")
    lines.append("")

    # === Summary stats ===
    dream_by_tier = {0: 0, 1: 0, 2: 0, 3: 0}
    for d in dreams:
        tier = d["metadata"].get("confidence")
        if tier in dream_by_tier:
            dream_by_tier[tier] += 1

    lines.append("## Summary")
    lines.append(f"- Observations stored: {len(observations)}")
    lines.append(
        f"- Dreams stored: {len(dreams)} "
        f"(tier 3 vivid: {dream_by_tier[3]}, "
        f"tier 2 present: {dream_by_tier[2]}, "
        f"tier 1 thin: {dream_by_tier[1]})"
    )
    if amq["mcp_available"]:
        lines.append(f"- AMQ inbox: {amq['new_count']} unread")
    else:
        lines.append("- AMQ inbox: MCP unavailable (no production check)")
    lines.append("")

    # === Halt warnings ===
    if warnings:
        lines.append("## ⚠ Warnings")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # === Observations ===
    lines.append("## Observations")
    if not observations:
        lines.append("(none)")
    else:
        for obs in observations:
            ts = _format_timestamp(obs["metadata"].get("stored_at", ""))
            content = obs.get("content", "").strip()
            # Truncate long observations to first ~200 chars for the digest
            preview = content if len(content) <= 200 else content[:200] + "..."
            derived = obs["metadata"].get("derived_from", "")
            derived_note = f" [derived from: {derived}]" if derived else ""
            lines.append(f"- **{ts}**{derived_note} — {preview}")
    lines.append("")

    # === Dreams ===
    lines.append("## Dreams")
    if not dreams:
        lines.append("(none — could be silence cycles, could mean no DREAMING fired)")
    else:
        for d in dreams:
            ts = _format_timestamp(d["metadata"].get("stored_at", ""))
            tier = d["metadata"].get("confidence")
            label = _tier_label(tier)
            content = d.get("content", "").strip()
            preview = content if len(content) <= 300 else content[:300] + "..."

            # Diagnostic signals for the audit
            nouns = d["metadata"].get("noun_matches", "?")
            jux = d["metadata"].get("has_juxtaposition", False)
            hard = d["metadata"].get("has_hard_marker", False)
            soft = d["metadata"].get("soft_marker_count", 0)

            tier_diag = f"tier {tier} ({label}) — nouns={nouns}"
            if jux:
                tier_diag += ", juxtaposition"
            if hard:
                tier_diag += ", HARD marker"
            if soft > 0:
                tier_diag += f", soft markers={soft}"

            lines.append(f"- **{ts}** [{tier_diag}]")
            lines.append(f"  > {preview}")
    lines.append("")

    # === Inbox preview ===
    if amq["recent_subjects"]:
        lines.append("## Recent AMQ activity (unread)")
        for s in amq["recent_subjects"]:
            priority_mark = "🔴 " if s["priority"] == "urgent" else ""
            lines.append(
                f"- {priority_mark}from {s['from']} ({s['kind']}): {s['subject']}"
            )
        lines.append("")

    # === Footer ===
    lines.append("---")
    lines.append(
        f"Auto-generated by digest.py. "
        f"To audit a dream's score: check noun_matches and juxtaposition signals. "
        f"To halt the bird: `sudo systemctl stop perpprompt-thinking.timer "
        f"perpprompt-dreaming.timer`."
    )

    return "\n".join(lines)


# =============================================================================
# Top-level entry points
# =============================================================================

def build_digest(bird_name: str, period_hours: int = DEFAULT_PERIOD_HOURS) -> dict:
    """Gather data and format a digest. Returns dict with text + diagnostics."""
    logger.info("Building digest for %s (period=%dh)", bird_name, period_hours)

    observations = gather_observations(bird_name, period_hours)
    dreams = gather_dreams(bird_name, period_hours)
    amq = gather_amq_activity(bird_name)
    warnings = check_halt_conditions(observations, dreams, amq=amq, bird_name=bird_name)

    text = format_digest(bird_name, period_hours, observations, dreams, amq, warnings)

    return {
        "text": text,
        "observation_count": len(observations),
        "dream_count": len(dreams),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def send_digest(
    bird_name: str,
    recipient: str = "holden",
    period_hours: int = DEFAULT_PERIOD_HOURS,
    dry_run: bool = False,
) -> bool:
    """Build the digest and send via amq_send to the recipient."""
    digest = build_digest(bird_name, period_hours)

    today_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    subject = f"Daily digest — {bird_name}, {today_str}"
    if digest["warning_count"] > 0:
        subject = "⚠ " + subject
        priority = "urgent" if digest["warning_count"] >= 2 else "normal"
    else:
        priority = "normal"

    if dry_run:
        logger.info("[DRY-RUN] Would send digest to %s (subject=%s, priority=%s)",
                    recipient, subject, priority)
        logger.info("=== DIGEST TEXT START ===")
        for line in digest["text"].splitlines():
            logger.info(line)
        logger.info("=== DIGEST TEXT END ===")
        return True

    success = mcp_client.amq_send(
        from_agent=bird_name,
        to_agent=recipient,
        body=digest["text"],
        subject=subject,
        kind="status",
        priority=priority,
    )

    if success:
        logger.info(
            "Digest sent to %s (obs=%d, dreams=%d, warnings=%d)",
            recipient,
            digest["observation_count"],
            digest["dream_count"],
            digest["warning_count"],
        )
    else:
        logger.error("Digest send failed — MCP unavailable or auth issue")
    return success


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Send the bird's daily digest.")
    parser.add_argument("--bird-name", required=True, help="The bird's chosen name.")
    parser.add_argument(
        "--recipient",
        default="holden",
        help="AMQ recipient (default: holden).",
    )
    parser.add_argument(
        "--period-hours",
        type=int,
        default=DEFAULT_PERIOD_HOURS,
        help=f"Lookback window in hours (default {DEFAULT_PERIOD_HOURS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to stdout instead of sending.",
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

    success = send_digest(
        bird_name=args.bird_name,
        recipient=args.recipient,
        period_hours=args.period_hours,
        dry_run=args.dry_run,
    )
    sys.exit(0 if success else 2)


if __name__ == "__main__":
    main()
