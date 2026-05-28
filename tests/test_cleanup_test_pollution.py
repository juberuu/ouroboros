import importlib.util
import pathlib


def _load_cleanup_module():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "cleanup_test_pollution.py"
    spec = importlib.util.spec_from_file_location("cleanup_test_pollution", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_pollution_skips_non_test_extension_imports_by_default(tmp_path):
    cleanup = _load_cleanup_module()
    live_import = tmp_path / "state" / "skills" / "prod_skill" / "__extension_imports" / "live"
    live_import.mkdir(parents=True)

    assert cleanup.collect_targets(tmp_path) == []
    assert cleanup.collect_targets(tmp_path, all_extension_imports=True) == [live_import]


def test_cleanup_pollution_collects_magicmock_repo_root_files(tmp_path):
    cleanup = _load_cleanup_module()
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    offender = repo_dir / "MagicMock-name-mock.drive_logs.txt"
    offender.write_text("junk", encoding="utf-8")
    nested = repo_dir / "subdir" / "MagicMock-name-nested.txt"
    nested.parent.mkdir()
    nested.write_text("ignore", encoding="utf-8")

    assert cleanup.collect_targets(data_dir, repo_dir=repo_dir) == [offender]

