"""Ouroboros marketplace surface (v4.50).

Subpackages:

- :mod:`ouroboros.marketplace.clawhub` — read-only HTTP client to the
  ClawHub registry (``https://clawhub.ai/api/v1``).
- :mod:`ouroboros.marketplace.fetcher` — staging download + verify.
- :mod:`ouroboros.marketplace.adapter` — translate OpenClaw frontmatter
  into the Ouroboros ``SKILL.md`` shape.
- :mod:`ouroboros.marketplace.provenance` — durable provenance records
  under ``data/state/skills/<name>/clawhub.json``.
- :mod:`ouroboros.marketplace.install` — orchestration pipeline that
  ties fetch + adapter + skill_review together.

Plugins (Node/TypeScript packages with ``openclaw.plugin.json``) are
intentionally NOT supported. The marketplace UI filters them out at
search time and the install pipeline refuses them with a clear error.
"""
