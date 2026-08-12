"""
Curate the target list for the ``auto/utility-free`` OmniRoute combo.

Agent Zero's **utility model** is the "small, fast, cheap" slot. It runs on
(nearly) every turn and does the background work: history/topic
summarization, chat renaming, behaviour-ruleset merging, memory
memorize/recall/consolidation, document-query rewriting, email dispatch,
infection-safety auditing, and optional chat compaction. Five of those jobs
need **JSON-as-text** output (parsed leniently by ``helpers/dirty_json.py`` —
no native ``json_mode`` API is used), the rest are plain text. Context ranges
from tiny single-turn prompts up to ~50k-char history feeds, and the per-call
cascade timeout is 30 s.

So a good utility free model is: **free + reliable instruction-following
(emits parseable JSON-as-text) + a usable context window (>=32k, ideally
128k+) + fast inference**. We keep the *best* models in the chain (the user
asked us not to exclude strong models — they make fine last-resort fallbacks),
and only drop the "rubbish": models that are not text-chat models at all, or
that can't reliably follow a format, or that are too flaky for a slot that
fires this often.

This module is pure (no I/O) so it is unit-tested directly by
``tests/smoke.py``. The actual gateway ``POST /api/combos`` call lives in
``helpers/omniroute_client.py``; the ``api/combos.py`` endpoint glues the two
together: live free models -> ``curate_utility_targets`` -> gateway combo.

Import path:
    from usr.plugins.omniroute.helpers.utility_combo import curate_utility_targets
"""

from __future__ import annotations

import re
from typing import List, Tuple

# The combo id. The OmniRoute gateway bans colons in combo *names* and derives
# the combo's model id in /v1/models from the slugified name (slashes
# preserved), so the id is also the name and must be colon-free. This mirrors
# the gateway's built-in ``auto/coding:free`` / ``auto/reasoning:free``
# category:tier convention with ``-`` standing in for the banned ``:``. In
# Agent Zero's model picker it appears as ``omniroute/auto/utility-free``.
# The plugin's tier classifier already badges ids ending in ``-free`` as
# "free" (see ``omniroute_client._TIER_FREE_PATTERNS`` — the ``-free`` pattern
# matches ``auto/utility-free``), so no tier-code change is needed.
COMBO_ID = "auto/utility-free"
UTILITY_COMBO_STRATEGY = "priority"
MAX_TARGETS = 12
# Tail slots reserved for strong-but-slow reasoners (tier 3) so the "best"
# models are always kept as last-resort fallbacks even when fast/mid models
# alone would fill the cap (user: "don't exclude the best models").
_RESERVE_SLOW = 2


# --------------------------------------------------------------- exclusions
#
# Drop these from the utility pool. A utility model must follow text/JSON
# instructions reliably and serve a slot that fires on most turns, so we
# exclude non-text modalities, embeddings/rerank/moderation, tiny toy models
# that ignore format, and flaky low-rate no-auth providers (5 req/min is
# unusable for a slot that fires this often). Deprecated providers too.
#
# Patterns are matched as substrings (case-insensitive) against the bare
# gateway model id (no ``omniroute/`` prefix), e.g. ``groq/llama-4-scout``.

_EXCLUDE_PATTERNS: Tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # --- image generation ---
        r"flux", r"stable-diffusion", r"sdxl", r"dall-e", r"imagen",
        r"seedream", r"seeddream", r"kling", r"runway", r"luma", r"sora",
        r"hunyuan-image", r"ideogram", r"playground", r"recraft",
        r"nano-banana", r"gpt-image",
        # --- video ---
        r"veo", r"video",
        # --- audio ---
        r"whisper", r"tts", r"bark", r"playai", r"stable-audio",
        r"musicgen", r"elevenlabs",
        # --- embeddings / rerank / moderation / clip ---
        # (anchored to avoid false-positives on chat model ids: "e5"/"gte"
        # as bare substrings could clip a chat model, so require the dash)
        r"embed", r"bge", r"e5-", r"nomic-embed", r"mxbai", r"gte-",
        r"jina-embed", r"arctic-embed", r"rerank", r"jina-rerank",
        r"moderation", r"llama-guard", r"clip",
        # --- tiny toy models that can't follow JSON reliably ---
        r"0\.5b", r"\b1b\b", r"1\.5b", r"135m", r"360m", r"smollm", r"tiny",
        # --- flaky low-rate no-auth providers (utility fires too often) ---
        r"g4f", r"dgrid/",
        # --- deprecated ---
        r"galadriel", r"predibase",
        # --- gateway meta-routes (auto/* combos). A combo must never target
        # another combo (recursive routing) — and once creation works, our own
        # auto/utility-free plus the built-in auto/coding:free / auto/best-free
        # etc. all show up in /v1/models and are classified "free" (they end in
        # -free or :free), so without this guard they'd be curated into the
        # utility target list and the combo would route to itself. The auto/
        # prefix is reserved for the gateway's meta-combos; real provider ids
        # are namespaced as groq/, google/, openrouter/, etc. ---
        r"^auto/",
    )
)


def _is_excluded(model_id: str) -> bool:
    """True if the id matches a non-utility / rubbish pattern."""
    low = model_id.lower()
    return any(p.search(low) for p in _EXCLUDE_PATTERNS)


# --------------------------------------------------------------- ordering
#
# A ``priority`` combo tries ``targets[0]`` first and only slides down on
# failure. For a fast utility slot we want the fast free models up front and
# the strong-but-slow reasoners as last-resort fallbacks (kept in the chain
# per the user's "don't exclude the best" rule, just ordered last).
#
# Each tier is a list of substring patterns; the first tier that matches wins.
# ``_order_tier`` returns 0..3 (lower = tried earlier); unmatched text-chat
# models default to tier 2 (solid mid) so unknown-but-plausible chat models
# land in a sensible spot rather than being dropped or pinned first.

_ORDER_TIERS: Tuple[Tuple[re.Pattern, ...], ...] = (
    # tier 0 — fast inference hosts (latency-optimized providers)
    tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"^groq/", r"^cerebras/", r"^nvidia/", r"^pol/openai-fast",
            r"^pol/qwen-coder", r"^siliconflow/", r"^llm7/", r"^internlm/",
        )
    ),
    # tier 1 — fast chat families
    tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"gemini.*flash", r"gpt-oss", r"gpt-4o-mini", r"o4-mini",
            r"llama-4-scout", r"llama-3\.1-8b", r"qwen2\.5", r"qwen3",
            r"qwen-coder", r"gemma",
        )
    ),
    # tier 2 — solid mid (default for unknown-but-plausible chat models)
    tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"deepseek-chat", r"deepseek-v3", r"mistral", r"mixtral",
            r"devstral", r"codestral", r"glm-4", r"kimi-k2", r"command-r",
            r"phi-4", r"nemotron", r"hermes",
        )
    ),
    # tier 3 — best but slow (last resort)
    tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"deepseek-r1", r"\bo1\b", r"\bo3\b", r"llama-.*70b",
            r"llama-4-maverick", r"qwen3-max",
        )
    ),
)


def _order_tier(model_id: str) -> int:
    low = model_id.lower()
    for tier, patterns in enumerate(_ORDER_TIERS):
        if any(p.search(low) for p in patterns):
            return tier
    return 2  # unknown-but-plausible chat model -> solid mid


def curate_utility_targets(free_model_ids: List[str]) -> List[str]:
    """Curate an ordered target list for the ``auto/utility-free`` combo.

    Args:
        free_model_ids: bare gateway model ids (no ``omniroute/`` prefix)
            already filtered to ``classify_tier == "free"`` by the caller.

    Returns:
        Ordered list of model ids suited for the utility slot, capped at
        ``MAX_TARGETS``. Fast free models come first; strong-but-slow
        reasoners come last (kept as last-resort fallbacks, not excluded —
        the user asked us not to drop the best models, so we reserve
        ``_RESERVE_SLOW`` tail slots for tier-3 reasoners even when the
        fast/mid pool alone would fill the cap).
        Non-text / embedding / toy / flaky / deprecated ids are dropped.

    Within each tier the original gateway order is preserved, so re-runs
    with the same enabled providers produce a stable combo.
    """
    by_tier: List[List[str]] = [[], [], [], []]
    for mid in free_model_ids:
        if not mid or not isinstance(mid, str):
            continue
        if _is_excluded(mid):
            continue
        by_tier[_order_tier(mid)].append(mid)

    # Reserve tail slots for the strong-but-slow reasoners (tier 3) so the
    # "best" models are always represented as last-resort fallbacks, even
    # when the fast/mid pool alone would fill MAX_TARGETS.
    slow = by_tier[3][:_RESERVE_SLOW]
    fast_budget = MAX_TARGETS - len(slow)

    ordered: List[str] = []
    for tier in (0, 1, 2):  # fast -> mid
        for mid in by_tier[tier]:
            if len(ordered) >= fast_budget:
                break
            ordered.append(mid)
        if len(ordered) >= fast_budget:
            break
    ordered.extend(slow)
    return ordered