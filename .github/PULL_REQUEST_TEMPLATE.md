<!--
Thank you for contributing to Ouroboros.

Open this PR against the `ouroboros` branch, not `main` or
`ouroboros-stable`. Keep review artifacts out of the git diff: attach or link
them in the Review evidence section instead.
-->

## Summary

<!-- What changes, and why is it needed? Link an issue or discussion when relevant. -->

## Scope

In scope:

-

Non-goals:

-

## Verification

<!-- List exact commands and outcomes. Do not write only "tests pass". -->

- [ ] Focused tests for the changed behavior pass.
- [ ] The default local test suite passes, or the reason it was not run is below.
- [ ] Lint/static checks relevant to this change pass.

Commands and results:

```text

```

## Visual evidence

<!-- Check one. Visible UI changes need evidence from a real rendered flow. -->

- [ ] Not applicable; this PR has no visible UI change.
- [ ] Before/after screenshots or other rendered-flow evidence are attached below.

Evidence:

## Governance and documentation

- [ ] I followed `CONTRIBUTING.md` and checked the relevant guidance in
      `BIBLE.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, and
      `docs/CHECKLISTS.md`.
- [ ] I updated tests and documentation where behavior or architecture changed.
- [ ] I did not include secrets, local settings, runtime state, logs, caches, or
      generated build/review artifacts in the commit.
- [ ] I did **not** bump `VERSION` or release-only version carriers; maintainers
      assign the collision-free release version during final integration.

## Agent assistance (optional)

<!-- Disclosure is useful context, not a quality signal. Remove this section if unused. -->

- Agent/tool and model:
- Work performed by the agent:
- Human verification performed:

## Review evidence

<!--
Attach or link the final triad/scope review output when available. Evidence must
correspond to the current PR diff; rerun it after code changes or a rebase. If it
was not run, say why. Missing evidence does not prevent opening a PR, but it can
make maintainer integration slower.
-->

- [ ] Triad/scope review evidence is attached or linked below.
- [ ] Review was not run; the reason is recorded below.

- Reviewed base SHA:
- Reviewed head/diff SHA:
- Review command/profile:
- Triad verdict:
- Scope verdict:
- Run artifacts/link:
- Known advisory findings or accepted tradeoffs:
- If not run, reason:

## Final checklist

- [ ] The PR base branch is `ouroboros`.
- [ ] The branch is based on a current `ouroboros` revision.
- [ ] The PR is one coherent change and is ready to be squash-integrated.
- [ ] The description explains any limitations, follow-up work, or compatibility impact.
