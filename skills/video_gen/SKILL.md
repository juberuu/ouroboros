---
name: video_gen
description: Generate a short video clip via OpenRouter's video generation API using Seedance 2.0 (bytedance/seedance-2.0). Output is saved to the system temp directory.
version: 0.5.0
type: script
runtime: python3
timeout_sec: 300
when_to_use: User wants to generate a video from a text prompt. Supports optional duration, aspect ratio, and resolution parameters.
permissions: [net, fs]
env_from_settings: [VIDEO_GEN_KEY]
scripts:
  - name: generate.py
    description: Generate a video from a text prompt. Reads VIDEO_GEN_KEY from env. Output written to system temp dir. Usage: generate.py <prompt> [--model MODEL] [--duration SEC] [--aspect RATIO] [--resolution RES] [--out FILENAME]
---

# Video Generation skill

Generates short video clips via the [OpenRouter Video Generation API](https://openrouter.ai/docs/api/api-reference/video-generation).

Default model: `bytedance/seedance-2.0`

## Setup

1. Add `VIDEO_GEN_KEY` in Ouroboros Settings with an [OpenRouter API key](https://openrouter.ai/keys).
2. Run `review_skill(skill="video_gen")`.
3. `toggle_skill(skill="video_gen", enabled=true)`

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
{"status": "completed", "output_path": "/tmp/video_1234567890.mp4", "job_id": "...", "cost_usd": 0.5}
```

The output file is written to the **skill state directory**
(`~/Ouroboros/data/state/skills/video_gen/`) — never inside the skill source
directory, to preserve the content hash. Falls back to system temp dir if the
state dir is unavailable.

## Optional arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `bytedance/seedance-2.0` | OpenRouter model ID |
| `--duration` | (model default) | Video duration in seconds |
| `--aspect` | (model default) | Aspect ratio, e.g. `16:9`, `9:16`, `1:1` |
| `--resolution` | (model default) | Resolution, e.g. `720p`, `1080p` |
| `--out` | `video_<timestamp>.mp4` | Output filename (plain name, no slashes; written to temp dir) |

## Security model

- `VIDEO_GEN_KEY` is read from env via `env_from_settings`; no secrets appear in argv or stdout.
- Output goes to the system temp directory — the skill directory is never written to.
- Cross-host HTTP redirects are blocked by a custom `urllib` redirect handler, preventing
  Bearer token leakage to CDN or other third-party hosts.
- Only reaches `openrouter.ai` — no other network contact.

## Notes

- Seedance 2.0 generation typically takes 30–120 seconds. Polling timeout: 200 s (total budget fits within the 300 s skill ceiling: submit 30 s + poll 200 s + download 60 s = 290 s).
