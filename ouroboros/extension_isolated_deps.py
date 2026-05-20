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


def _extend_path_candidates(candidates: List[object], value: object) -> None:
    if value is None:
        return
    if isinstance(value, (str, bytes, pathlib.Path)):
        candidates.append(value)
        return
    try:
        candidates.extend(list(value))  # type: ignore[arg-type]
        return
    except Exception:
        pass
    # importlib namespace paths recalculate from parent packages during
    # iteration. If a third-party object is temporarily inconsistent, the
    # cached path list is still enough for isolated-deps cleanup.
    cached = getattr(value, "_path", None)
    if cached is None:
        return
    try:
        candidates.extend(list(cached))
    except Exception:
        return


def _module_paths(module: ModuleType) -> List[pathlib.Path]:
    candidates: List[object] = []
    module_file = getattr(module, "__file__", None)
    if module_file:
        candidates.append(module_file)
    module_path = getattr(module, "__path__", None)
    _extend_path_candidates(candidates, module_path)
    spec = getattr(module, "__spec__", None)
    locations = getattr(spec, "submodule_search_locations", None)
    _extend_path_candidates(candidates, locations)
    out: List[pathlib.Path] = []
    for value in candidates:
        try:
            out.append(pathlib.Path(value).resolve())
        except Exception:
            continue
    return out


def _path_is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _module_names_under_site_dir_best_effort(site_path: pathlib.Path) -> List[str]:
    to_drop: set[str] = set()
    package_prefixes: set[str] = set()
    modules = list(sys.modules.items())
    for name, module in modules:
        if not name or module is None:
            continue
        paths = _module_paths(module)
        if not any(_path_is_under(path, site_path) for path in paths):
            continue
        to_drop.add(name)
        module_path_attr = getattr(module, "__path__", None)
        module_file = getattr(module, "__file__", None)
        if module_path_attr is not None and module_file:
            try:
                if _path_is_under(pathlib.Path(module_file).resolve(), site_path):
                    package_prefixes.add(f"{name}.")
            except Exception:
                pass
    if package_prefixes:
        for name, module in modules:
            if not name or module is None:
                continue
            if any(name.startswith(prefix) for prefix in package_prefixes):
                to_drop.add(name)
    return sorted(to_drop, key=lambda value: value.count("."), reverse=True)


def _module_names_under_site_dir(site_path: pathlib.Path) -> List[str]:
    return _module_names_under_site_dir_best_effort(site_path)


def _drop_modules_under_site_dir(site_path: pathlib.Path) -> BaseException | None:
    cleanup_error = None
    try:
        module_names = _module_names_under_site_dir(site_path)
    except BaseException as exc:
        cleanup_error = exc
        module_names = _module_names_under_site_dir_best_effort(site_path)
    for name in module_names:
        try:
            sys.modules.pop(name, None)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
    return cleanup_error


def _drop_importer_cache_for_site_dir(site_path: pathlib.Path, site_str: str) -> None:
    for raw in list(sys.path_importer_cache.keys()):
        raw_str = str(raw or "")
        if not raw_str:
            continue
        if raw_str == site_str:
            sys.path_importer_cache.pop(raw, None)
            continue
        try:
            pathlib.Path(raw_str).resolve().relative_to(site_path)
        except Exception:
            continue
        sys.path_importer_cache.pop(raw, None)


def release_isolated_site_dirs(site_dirs: Sequence[str]) -> None:
    first_error = None
    for raw in site_dirs:
        site_str = str(raw or "")
        if not site_str:
            continue
        with _lock:
            count = _injected_site_dir_refs.get(site_str, 0)
            if count > 1:
                _injected_site_dir_refs[site_str] = count - 1
                continue
            site_path = pathlib.Path(site_str).resolve()
            cleanup_error = _drop_modules_under_site_dir(site_path)
            try:
                while site_str in sys.path:
                    sys.path.remove(site_str)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            try:
                _drop_importer_cache_for_site_dir(site_path, site_str)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            try:
                importlib.invalidate_caches()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            _injected_site_dir_refs.pop(site_str, None)
            first_error = first_error or cleanup_error
    if first_error is not None:
        raise first_error


@contextmanager
def isolated_site_dirs_scope(skill_dir: pathlib.Path, *, enabled: bool) -> Iterator[None]:
    """Serialize extension import work and expose this skill's deps only in-scope."""

    _execution_lock.acquire()
    site_dirs = inject_isolated_site_dirs(skill_dir) if enabled else []
    try:
        yield
    finally:
        try:
            release_isolated_site_dirs(site_dirs)
        finally:
            _execution_lock.release()


@asynccontextmanager
async def async_isolated_site_dirs_scope(skill_dir: pathlib.Path, *, enabled: bool) -> Iterator[None]:
    await asyncio.to_thread(_execution_lock.acquire)
    site_dirs = inject_isolated_site_dirs(skill_dir) if enabled else []
    try:
        yield
    finally:
        try:
            release_isolated_site_dirs(site_dirs)
        finally:
            _execution_lock.release()
