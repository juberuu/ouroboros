---
name: video_gen
description: Generate a short video clip via OpenRouter's video generation API using Seedance 2.0 (bytedance/seedance-2.0). Output is saved to the skill state directory.
version: 0.7.0
type: script
runtime: python3
timeout_sec: 300
when_to_use: User wants to generate a video from a text prompt. Supports optional duration, aspect ratio, and resolution parameters.
permissions: [net, fs]
env_from_settings: [OPENROUTER_API_KEY]
scripts:
  - name: generate.py
    description: "Generate a video from a text prompt. Reads OPENROUTER_API_KEY from env (granted via the Skills tab). Output written to skill state directory (OUROBOROS_SKILL_STATE_DIR). Usage: generate.py <prompt> [--model MODEL] [--duration SEC] [--aspect RATIO] [--resolution RES] [--out FILENAME]"
---

# Video Generation skill

Generates short video clips via the [OpenRouter Video Generation API](https://openrouter.ai/docs/api/api-reference/video-generation).

Default model: `bytedance/seedance-2.0`

## Setup

1. Add your [OpenRouter API key](https://openrouter.ai/keys) under `OPENROUTER_API_KEY` in Ouroboros Settings.
2. Run `review_skill(skill="video_gen")` and wait for a PASS verdict.
3. On the Skills tab, click **Grant access** to authorise this skill to read `OPENROUTER_API_KEY` from settings.
4. `toggle_skill(skill="video_gen", enabled=true)` (or use the Enable button on the Skills tab).

## Usage

```
skill_exec(
  skill="video_gen",
  script="generate.py",
  args=["A cat riding a surfboard at sunset, cinematic 4k"]
)
```

Output on success:
```json
{"status": "completed", "output_path": "/Users/you/Ouroboros/data/state/skills/video_gen/video_1234567890.mp4", "job_id": "...", "cost_usd": 0.5}
```

The output file is written to the **skill state directory**
(`~/Ouroboros/data/state/skills/video_gen/`), injected by skill_exec as
`OUROBOROS_SKILL_STATE_DIR`. The script raises an error if this variable
is not set (it must be run via `skill_exec`, not directly).

## Optional arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `bytedance/seedance-2.0` | OpenRouter model ID |
| `--duration` | (model default) | Video duration in seconds |
| `--aspect` | (model default) | Aspect ratio, e.g. `16:9`, `9:16`, `1:1` |
| `--resolution` | (model default) | Resolution, e.g. `720p`, `1080p` |
| `--out` | `video_<timestamp>.mp4` | Output filename (plain name, no slashes; written to skill state dir) |

## Security model

- `OPENROUTER_API_KEY` is a forbidden / "core" settings key; it only reaches the script's environment after the owner approves a per-skill grant via the Skills tab. The grant is bound to the current skill content hash, so any edit to this skill invalidates the grant and re-prompts the owner.
- The grant is forwarded into the subprocess by `_scrub_env`; no secrets appear in argv or stdout.
- Output is confined to the skill state directory (`OUROBOROS_SKILL_STATE_DIR`).
- Cross-host HTTP redirects are blocked by a custom `urllib` redirect handler.
- Only reaches `openrouter.ai` — no other network contact.

## Notes

- Seedance 2.0 generation typically takes 30–120 seconds. Polling timeout: 200 s (total: submit 30 s + poll 200 s + download 60 s = 290 s < 300 s ceiling).
