---
name: weather
description: Fetch current weather for any city via the public wttr.in service (no API key required).
version: 0.1.0
type: script
runtime: python3
timeout_sec: 30
when_to_use: User asks about weather, temperature, forecast, or current conditions in a specific city.
permissions: [net]
env_from_settings: []
scripts:
  - name: fetch.py
    description: Fetch a 3-line weather summary for the city supplied via argv.
---

# Weather skill (reference)

This is the Phase 5 reference skill shipped with Ouroboros. It demonstrates
the minimum viable ``type: script`` extension:

- A manifest declaring ``runtime: python3`` and a single script.
- One ``net`` permission (the script reaches ``wttr.in``).
- No ``env_from_settings`` — no secret handling needed, no credential surface.

## Using it

1. Point ``OUROBOROS_SKILLS_REPO_PATH`` at the directory containing this
   skill (``skills/`` under the Ouroboros checkout works out of the box).
2. Run ``review_skill(skill="weather")`` — the tri-model review should pass
   cleanly (public HTTP fetch, no secrets, no repo mutation, tight confinement).
3. ``toggle_skill(skill="weather", enabled=true)`` flips the durable
   enabled bit.
4. Invoke with ``skill_exec(skill="weather", script="fetch.py", args=["Moscow"])``.

The script prints one JSON object per invocation:

```
{"city": "Moscow", "temp_c": 7, "condition": "Partly cloudy", "feels_like_c": 4}
```

Reviewers should verify that ``fetch.py`` only reaches ``wttr.in`` (the
declared ``net`` permission is the minimum required) and refuses to
contact any other host.
