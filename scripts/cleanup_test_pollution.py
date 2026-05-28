#!/usr/bin/env python3
"""Remove known test-pollution artifacts from the local Ouroboros data dir.

Dry-run by default. Use --apply after inspecting the planned removals.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil


TEST_STATE_NAMES = {
    "badui", "cleanup_ext", "crashy", "d1", "delayed_ghost_ext", "env_async_neighbor",
    "env_async_owner", "env_import", "env_namespace", "env_overlap_neighbor",
    "env_overlap_owner", "env_owner", "env_pth", "env_symlink", "env_untracked",
    "ext1", "ext2", "ghost_ext", "hello", "pollui", "settings_ext", "settings_unload_race",
    "staleish", "tree_ext", "uiwait", "ws1",
}


def _default_data_dir() -> pathlib.Path:
    return pathlib.Path.home() / "Ouroboros" / "data"


def collect_targets(
    data_dir: pathlib.Path,
    *,
    all_extension_imports: bool = False,
    repo_dir: pathlib.Path | None = None,
) -> list[pathlib.Path]:
    state_skills = data_dir / "state" / "skills"
    targets: list[pathlib.Path] = []
    if state_skills.exists():
        for skill_dir in state_skills.iterdir():
            if not skill_dir.is_dir():
                continue
            imports = skill_dir / "__extension_imports"
            if imports.exists() and imports.is_dir() and (all_extension_imports or skill_dir.name in TEST_STATE_NAMES):
                for child in imports.iterdir():
                    if child.is_dir():
                        targets.append(child)
            if skill_dir.name in TEST_STATE_NAMES:
                remaining = [p.name for p in skill_dir.iterdir() if p.name != "__extension_imports"]
                safe_only = all(name in {"enabled.json", "review.json", "grants.json", "deps.json"} for name in remaining)
                if safe_only:
                    targets.append(skill_dir)
    if repo_dir and repo_dir.exists():
        targets.extend(
            path for path in repo_dir.iterdir()
            if path.is_file() and "MagicMock" in path.name
        )
    return sorted(set(targets), key=lambda p: str(p))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=pathlib.Path, default=_default_data_dir())
    parser.add_argument("--repo-dir", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument(
        "--all-extension-imports",
        action="store_true",
        help=(
            "also remove stale __extension_imports for non-test skill names; "
            "use only while Ouroboros is stopped"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="show planned removals without deleting")
    parser.add_argument("--apply", action="store_true", help="actually delete planned targets")
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run and --apply are mutually exclusive")

    targets = collect_targets(
        args.data_dir.expanduser(),
        all_extension_imports=bool(args.all_extension_imports),
        repo_dir=args.repo_dir.expanduser(),
    )
    action = "DELETE" if args.apply else "DRY-RUN"
    if not targets:
        print(f"{action}: no known test-pollution targets found")
        return 0
    for path in targets:
        print(f"{action}: {path}")
        if args.apply:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
