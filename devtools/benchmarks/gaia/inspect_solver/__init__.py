"""Inspect-evals solver wrapper for GAIA."""

# SSOT for the GAIA answer-format instruction (the official benchmark expects a
# format/prefix prompt). Shared by every solver so the three harnesses stay in
# parity. GAIA's quasi-exact-match scorer normalizes case/punctuation/articles but
# NOT scale or wording, so a clear format instruction is the honest, methodology-
# sanctioned way to align the agent's own answer shape. Adapter/prompt only — never
# imported into runtime core (core normalization would hurt ordinary users).
GAIA_FORMAT_INSTRUCTION = (
    "\n\nWork through the task, then end your response with a single line, "
    "exactly: FINAL ANSWER: <your answer>\nThe answer must be a number or as few "
    "words as possible, with no units unless asked."
)
