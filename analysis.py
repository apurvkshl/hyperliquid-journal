"""
LLM analysis step via the Claude CLI (claude -p).

No API key required — uses the logged-in Claude Code session.
Invokes: claude -p --system-prompt "..." --output-format text --tools ""
with the user prompt on stdin.
"""

import json
import logging
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

MODEL = "sonnet"  # alias resolved by claude CLI to latest sonnet

SYSTEM_PROMPT = (
    "You are an experienced proprietary trader and trading coach. "
    "You have spent years reviewing trader blotters and you are direct, "
    "specific, and evidence-driven. You never give generic advice. "
    "Every observation you make is tied to specific trades or numbers from "
    "the data. You are honest about what you cannot conclude from limited data."
)


def build_user_prompt(short_address: str, stats_dict: dict, trade_table: str) -> str:
    low_conf = stats_dict.get("low_confidence", False)
    conf_note = (
        "\n\n**DATA WARNING: fewer than 20 round-trip trades. "
        "Treat ALL patterns as preliminary hypotheses, not conclusions.**\n"
        if low_conf else ""
    )

    return f"""You are reviewing the trading history of wallet {short_address} on Hyperliquid perpetuals.
{conf_note}
## Ground-truth statistics (Layer 1 — computed from raw fills)

{json.dumps(stats_dict, indent=2)}

## Trade sequence (ordered, most recent first, limited to target assets only)

{trade_table}

## What I need from you

Write a coaching report with these sections. Be specific and evidence-based. If you see less than 20 trades, say "LOW DATA — treat all patterns as preliminary hypotheses."

### 1. The Verdict (1 paragraph)
What kind of trader is this, and what is the single most expensive thing they are doing?

### 2. Why You're Losing
List the recurring, concrete reasons their losing trades lose. For each reason, cite 2–3 specific trades as evidence (date, asset, amount). If you cannot find a recurring reason from the data, say so.

### 3. Your Biases
Name each psychological bias you can actually evidence from this data. For each: name it, describe what it looks like in their trades, and cite the specific instances.

Biases to test (confirm or refute each; do NOT include ones you cannot evidence):
- Revenge trading after losses (re-entering quickly after a loss at larger size)
- FOMO chasing (entering after a large move has already happened)
- Disposition effect (cutting winners early, holding losers)
- Funding bleed (holding the crowded side through expensive negative funding)
- Overtrading / no-edge churn (high trade count, near-zero expectancy on many assets)
- Over-leverage / avoidable liquidations
- Session/time effects (dramatically worse at certain hours)
- Over-concentration / ignoring their actual edge asset

### 4. What's Working
The setups, assets, or contexts where expectancy is clearly positive. Tell them to do more of this. Cite the numbers.

### 5. One Thing to Change Next
A single, concrete, data-driven suggestion. Make it actionable tomorrow, not philosophical.

Ground every claim in the data. No filler. Be direct."""


def run_analysis(short_address: str, stats_dict: dict, trade_table: str) -> str:
    """
    Call the Claude CLI in non-interactive mode to generate a coaching report.
    Returns the full response text.
    Raises RuntimeError if the claude CLI is not found or exits non-zero.
    """
    if not shutil.which("claude"):
        raise RuntimeError(
            "claude CLI not found in PATH. "
            "Install Claude Code and ensure `claude` is on your PATH."
        )

    user_prompt = build_user_prompt(short_address, stats_dict, trade_table)

    logger.info("Calling Claude CLI for wallet %s...", short_address)

    # Write prompt to a temp file to avoid shell arg-length limits
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(user_prompt)
        prompt_path = f.name

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--setting-sources", "project,local",  # skip user settings → no hooks firing
                "--system-prompt", SYSTEM_PROMPT,
                "--output-format", "text",
                "--tools", "",          # disable all tools — pure text generation
                "--model", MODEL,
            ],
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        import os
        os.unlink(prompt_path)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"claude CLI exited with code {result.returncode}: {stderr[:400]}"
        )

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("claude CLI returned empty output")

    return output
