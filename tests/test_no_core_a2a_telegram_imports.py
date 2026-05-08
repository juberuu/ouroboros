import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE_PATHS = [
    ROOT / "ouroboros",
    ROOT / "supervisor",
    ROOT / "server.py",
]
FORBIDDEN_PATTERNS = [
    re.compile(r"^\s*from\s+ouroboros\.a2a_(executor|server|task_store)\s+import", re.MULTILINE),
    re.compile(r"^\s*import\s+ouroboros\.a2a_(executor|server|task_store)", re.MULTILINE),
    re.compile(r"^\s*from\s+ouroboros\.tools\.a2a\s+import", re.MULTILINE),
    re.compile(r"_send_telegram|_telegram_poll_loop", re.MULTILINE),
]


def _iter_python_files(path: pathlib.Path):
    if path.is_file():
        yield path
        return
    for child in path.rglob("*.py"):
        if child.name.startswith("test_"):
            continue
        yield child


def test_no_core_imports_for_extracted_a2a_or_telegram_surfaces() -> None:
    offenders = []
    for root in CORE_PATHS:
        for path in _iter_python_files(root):
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(text):
                    offenders.append(str(path.relative_to(ROOT)))
                    break
    assert offenders == []
