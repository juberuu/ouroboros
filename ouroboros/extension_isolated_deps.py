"""In-process extension integration for per-skill isolated Python deps."""

from __future__ import annotations

import asyncio
import pathlib
import importlib
import sys
import threading
from contextlib import asynccontextmanager, contextmanager
from types import ModuleType
from typing import Iterator, List, Sequence

from ouroboros.skill_loader import _SKILL_DIR_CACHE_NAMES


_lock = threading.RLock()
_execution_lock = threading.Lock()
_injected_site_dir_refs: dict[str, int] = {}


def is_skill_cache_path(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part in _SKILL_DIR_CACHE_NAMES for part in rel_parts)


def _isolated_python_site_dirs(skill_dir: pathlib.Path) -> List[pathlib.Path]:
    env_root = pathlib.Path(skill_dir) / ".ouroboros_env" / "python"
    candidates = [
        *env_root.glob("lib/python*/site-packages"),
        env_root / "Lib" / "site-packages",
    ]
    out: List[pathlib.Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
            resolved.relative_to(pathlib.Path(skill_dir).resolve())
        except Exception:
            continue
        if resolved.is_dir() and resolved not in out:
            out.append(resolved)
    return out


def inject_isolated_site_dirs(skill_dir: pathlib.Path) -> List[str]:
    """Temporarily expose reviewed isolated Python deps to an extension."""

    injected: List[str] = []
    for site_dir in _isolated_python_site_dirs(pathlib.Path(skill_dir)):
        site_str = str(site_dir)
        with _lock:
            count = _injected_site_dir_refs.get(site_str)
            if count is not None:
                _injected_site_dir_refs[site_str] = count + 1
                injected.append(site_str)
                continue
            if site_str in sys.path:
                continue
            sys.path.insert(0, site_str)
            importlib.invalidate_caches()
            _injected_site_dir_refs[site_str] = 1
            injected.append(site_str)
    return injected


def _module_paths(module: ModuleType) -> List[pathlib.Path]:
    candidates = []
    module_file = getattr(module, "__file__", None)
    if module_file:
        candidates.append(module_file)
    module_path = getattr(module, "__path__", None)
    if module_path:
        candidates.extend(list(module_path))
    spec = getattr(module, "__spec__", None)
    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        candidates.extend(list(locations))
    out: List[pathlib.Path] = []
    for value in candidates:
        try:
            out.append(pathlib.Path(value).resolve())
        except Exception:
            continue
    return out


def release_isolated_site_dirs(site_dirs: Sequence[str]) -> None:
    for raw in site_dirs:
        site_str = str(raw or "")
        if not site_str:
            continue
        with _lock:
            count = _injected_site_dir_refs.get(site_str, 0)
            if count > 1:
                _injected_site_dir_refs[site_str] = count - 1
                continue
            _injected_site_dir_refs.pop(site_str, None)
            site_path = pathlib.Path(site_str).resolve()
            for name, module in list(sys.modules.items()):
                for module_path in _module_paths(module):
                    try:
                        module_path.relative_to(site_path)
                    except Exception:
                        continue
                    sys.modules.pop(name, None)
                    break
            while site_str in sys.path:
                sys.path.remove(site_str)
            sys.path_importer_cache.pop(site_str, None)


@contextmanager
def isolated_site_dirs_scope(skill_dir: pathlib.Path, *, enabled: bool) -> Iterator[None]:
    """Serialize extension import work and expose this skill's deps only in-scope."""

    _execution_lock.acquire()
    site_dirs = inject_isolated_site_dirs(skill_dir) if enabled else []
    try:
        yield
    finally:
        release_isolated_site_dirs(site_dirs)
        _execution_lock.release()


@asynccontextmanager
async def async_isolated_site_dirs_scope(skill_dir: pathlib.Path, *, enabled: bool) -> Iterator[None]:
    await asyncio.to_thread(_execution_lock.acquire)
    site_dirs = inject_isolated_site_dirs(skill_dir) if enabled else []
    try:
        yield
    finally:
        release_isolated_site_dirs(site_dirs)
        _execution_lock.release()
