"""Tests for local_model.py — preflight, install, and progress features."""
from __future__ import annotations

import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, call, patch


class TestCheckRuntime(unittest.TestCase):
    """check_runtime() sets _runtime_status and returns bool."""

    def _make_mgr(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager()

    def test_check_runtime_ok(self):
        mgr = self._make_mgr()
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch("subprocess.run", return_value=fake_result):
            ok = mgr.check_runtime()
        self.assertTrue(ok)
        self.assertEqual(mgr._runtime_status, "ok")

    def test_check_runtime_missing(self):
        mgr = self._make_mgr()
        fake_result = MagicMock()
        fake_result.returncode = 1
        with patch("subprocess.run", return_value=fake_result):
            ok = mgr.check_runtime()
        self.assertFalse(ok)
        self.assertEqual(mgr._runtime_status, "missing")

    def test_check_runtime_subprocess_exception(self):
        mgr = self._make_mgr()
        with patch("subprocess.run", side_effect=FileNotFoundError("no python")):
            ok = mgr.check_runtime()
        self.assertFalse(ok)
        self.assertEqual(mgr._runtime_status, "missing")

    def test_check_runtime_timeout(self):
        mgr = self._make_mgr()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            ok = mgr.check_runtime()
        self.assertFalse(ok)
        self.assertEqual(mgr._runtime_status, "missing")


class TestInstallRuntime(unittest.TestCase):
    """install_runtime() manages _runtime_status lifecycle and _install_proc."""

    def _make_mgr(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager()

    def test_install_sets_installing_status(self):
        mgr = self._make_mgr()
        events = []

        def fake_run_install():
            events.append(mgr._runtime_status)

        mgr._run_install = fake_run_install
        # Patch threading so _run_install is called synchronously in test
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: fake_run_install()
            mgr.install_runtime()

        self.assertIn("installing", events)

    def test_install_already_installing_noop(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"
        started = []
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: started.append(1)
            mgr.install_runtime()
        # Should not have started another thread
        self.assertEqual(len(started), 0)

    def test_run_install_success(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        # check_runtime returns True after successful install
        with patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(mgr, "check_runtime", return_value=True):
            mgr._run_install()

        self.assertEqual(mgr._runtime_status, "install_ok")

    def test_run_install_pip_failure(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stdout = None
        fake_proc.wait.return_value = 1

        with patch("subprocess.Popen", return_value=fake_proc):
            mgr._run_install()

        self.assertEqual(mgr._runtime_status, "install_error")

    def test_run_install_import_still_fails_after_pip(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(mgr, "check_runtime", return_value=False):
            mgr._run_install()

        self.assertEqual(mgr._runtime_status, "install_error")
        self.assertIn("still fails", mgr._runtime_install_log)

    def test_run_install_clears_install_proc(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(mgr, "check_runtime", return_value=True):
            mgr._run_install()

        self.assertIsNone(mgr._install_proc)

    def test_stop_server_terminates_install_proc(self):
        from ouroboros.local_model import LocalModelManager

        mgr = LocalModelManager()
        fake_install = MagicMock()
        fake_install.pid = 12345
        mgr._install_proc = fake_install

        with patch("ouroboros.local_model.terminate_process_tree") as mock_term, \
             patch("ouroboros.local_model.kill_process_tree"):
            mgr.stop_server()

        mock_term.assert_called_once_with(fake_install)
        self.assertIsNone(mgr._install_proc)


class TestStatusDict(unittest.TestCase):
    """status_dict() exposes runtime_status and download_progress."""

    def test_status_dict_has_runtime_status(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        d = mgr.status_dict()
        self.assertIn("runtime_status", d)
        self.assertIn("download_progress", d)

    def test_runtime_status_propagated(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._runtime_status = "install_ok"
        d = mgr.status_dict()
        self.assertEqual(d["runtime_status"], "install_ok")

    def test_runtime_install_log_truncated_in_status(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._runtime_install_log = "x" * 2000
        d = mgr.status_dict()
        self.assertLessEqual(len(d["runtime_install_log"]), 500)


class TestDownloadProgressCallback(unittest.TestCase):
    """download_model() updates _download_progress via tqdm_class callback."""

    def test_progress_updates_on_download(self):
        from ouroboros.local_model import LocalModelManager

        mgr = LocalModelManager()
        progress_values = []

        # Simulate hf_hub_download calling tqdm update
        def fake_hf_hub_download(repo_id, filename, resume_download=True,
                                 tqdm_class=None, subfolder=None):
            # Simulate progress updates
            bar = tqdm_class(total=100)
            bar.update(50)
            bar.update(50)
            bar.close()
            return "/fake/path/model.gguf"

        tqdm_mod = types.ModuleType("tqdm")
        tqdm_auto_mod = types.ModuleType("tqdm.auto")

        class FakeTqdm:
            def __init__(self, total=None, **kw):
                self.n = 0
                self.total = total

            def update(self, n=1):
                self.n += n
                progress_values.append(self.n / self.total if self.total else 0)

            def close(self):
                pass

        tqdm_auto_mod.tqdm = FakeTqdm
        tqdm_mod.auto = tqdm_auto_mod

        import sys
        sys.modules.setdefault("tqdm", tqdm_mod)
        sys.modules.setdefault("tqdm.auto", tqdm_auto_mod)

        with patch("ouroboros.local_model.LocalModelManager.check_runtime", return_value=True), \
             patch("huggingface_hub.hf_hub_download", side_effect=fake_hf_hub_download, create=True):
            try:
                from ouroboros import local_model as lm
                # Patch within the module
                orig_hf = None
                try:
                    import huggingface_hub
                    orig_hf = huggingface_hub.hf_hub_download
                    huggingface_hub.hf_hub_download = fake_hf_hub_download
                except ImportError:
                    pass
                try:
                    path = mgr.download_model("some/repo", "model.gguf")
                    self.assertEqual(path, "/fake/path/model.gguf")
                    # _download_progress should have been updated to 1.0 (final)
                    self.assertEqual(mgr._download_progress, 1.0)
                finally:
                    if orig_hf is not None:
                        huggingface_hub.hf_hub_download = orig_hf
            except ImportError:
                self.skipTest("huggingface_hub not available")


class TestApiLocalModelStartPreflight(unittest.TestCase):
    """api_local_model_start returns 412 when runtime is missing."""

    def test_returns_412_when_runtime_missing(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_start

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = False
        mock_mgr._runtime_status = "missing"

        async def fake_json():
            return {"source": "some/repo", "filename": "m.gguf"}

        mock_request = MagicMock()
        mock_request.json = fake_json

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            resp = asyncio.get_event_loop().run_until_complete(
                api_local_model_start(mock_request)
            )

        self.assertEqual(resp.status_code, 412)
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["error"], "runtime_missing")
        self.assertIn("hint", body)
        # Download must NOT have been called
        mock_mgr.download_model.assert_not_called()

    def test_proceeds_when_runtime_ok(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_start

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = True

        async def fake_json():
            return {"source": "some/repo", "filename": "m.gguf"}

        mock_request = MagicMock()
        mock_request.json = fake_json

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_mgr.download_model.return_value = "/path/model.gguf"

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr), \
             patch("asyncio.to_thread", side_effect=fake_to_thread):
            resp = asyncio.get_event_loop().run_until_complete(
                api_local_model_start(mock_request)
            )

        self.assertNotEqual(resp.status_code, 412)
        mock_mgr.download_model.assert_called_once()


class TestAutoStartPreflight(unittest.TestCase):
    """auto_start_local_model skips download when runtime missing."""

    def test_skips_download_when_runtime_missing(self):
        from ouroboros.local_model_autostart import auto_start_local_model

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = False

        settings = {
            "LOCAL_MODEL_SOURCE": "some/repo",
            "LOCAL_MODEL_FILENAME": "model.gguf",
            "LOCAL_MODEL_PORT": 8766,
            "LOCAL_MODEL_N_GPU_LAYERS": 0,
            "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
            "LOCAL_MODEL_CHAT_FORMAT": "",
        }

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            auto_start_local_model(settings)

        mock_mgr.download_model.assert_not_called()
        mock_mgr.start_server.assert_not_called()
        # Status should be set to error with a meaningful message
        self.assertEqual(mock_mgr._status, "error")
        self.assertIn("llama-cpp-python", mock_mgr._error)

    def test_proceeds_when_runtime_ok(self):
        from ouroboros.local_model_autostart import auto_start_local_model

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = True
        mock_mgr.download_model.return_value = "/path/model.gguf"

        settings = {
            "LOCAL_MODEL_SOURCE": "some/repo",
            "LOCAL_MODEL_FILENAME": "model.gguf",
            "LOCAL_MODEL_PORT": 8766,
            "LOCAL_MODEL_N_GPU_LAYERS": 0,
            "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
            "LOCAL_MODEL_CHAT_FORMAT": "",
        }

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            auto_start_local_model(settings)

        mock_mgr.download_model.assert_called_once()
        mock_mgr.start_server.assert_called_once()


class TestInstallCancellationWindow(unittest.TestCase):
    """_run_install respects _install_cancelled flag set before Popen."""

    def test_cancels_before_popen_when_flag_set(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._install_cancelled.set()  # simulate stop_server() before thread starts

        mgr._run_install()

        # No subprocess should have been spawned and status reset to missing
        self.assertIsNone(mgr._install_proc)
        self.assertEqual(mgr._runtime_status, "missing")

    def test_clear_flag_on_new_install_attempt(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._install_cancelled.set()  # set from a previous stop

        with patch("subprocess.Popen") as mock_popen:
            # Calling install_runtime should clear the flag before starting
            # the thread (we check the flag was cleared, not that Popen ran)
            mgr.install_runtime()
            self.assertFalse(mgr._install_cancelled.is_set())


class TestStatusApiAutoProbe(unittest.TestCase):
    """api_local_model_status probes runtime on first poll when status unknown."""

    def test_status_probes_runtime_when_unknown(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_status

        mock_mgr = MagicMock()
        mock_mgr._runtime_status = "unknown"
        mock_mgr.get_status.return_value = "offline"
        mock_mgr.status_dict.return_value = {"status": "offline", "runtime_status": "missing"}

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_request = MagicMock()
        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr), \
             patch("asyncio.to_thread", side_effect=fake_to_thread):
            asyncio.get_event_loop().run_until_complete(api_local_model_status(mock_request))

        mock_mgr.check_runtime.assert_called_once()

    def test_status_skips_probe_when_already_known(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_status

        mock_mgr = MagicMock()
        mock_mgr._runtime_status = "ok"  # already probed
        mock_mgr.get_status.return_value = "offline"
        mock_mgr.status_dict.return_value = {"status": "offline", "runtime_status": "ok"}

        mock_request = MagicMock()
        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            import asyncio
            asyncio.get_event_loop().run_until_complete(api_local_model_status(mock_request))

        mock_mgr.check_runtime.assert_not_called()


class TestGetInstallCommand(unittest.TestCase):
    """_get_install_command returns a list starting with sys.executable."""

    def test_command_uses_sys_executable(self):
        from ouroboros.local_model import _get_install_command
        cmd = _get_install_command()
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], sys.executable)
        self.assertIn("llama-cpp-python[server]", cmd)

    def test_env_has_cmake_args_on_macos(self):
        from ouroboros.local_model import _get_install_env
        with patch("ouroboros.local_model.IS_MACOS", True), \
             patch("ouroboros.local_model.IS_WINDOWS", False):
            env = _get_install_env()
        self.assertIn("CMAKE_ARGS", env)
        self.assertIn("METAL", env["CMAKE_ARGS"].upper())

    def test_env_has_no_cmake_args_on_linux(self):
        from ouroboros.local_model import _get_install_env
        with patch("ouroboros.local_model.IS_MACOS", False), \
             patch("ouroboros.local_model.IS_WINDOWS", False):
            env = _get_install_env()
        self.assertNotIn("CMAKE_ARGS", env)


class TestNormalizeHfFilename(unittest.TestCase):
    """Tests for LocalModelManager._normalize_hf_filename."""

    def _mgr(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager

    def test_simple_filename(self):
        cls = self._mgr()
        subfolder, basename = cls._normalize_hf_filename("model.gguf")
        self.assertIsNone(subfolder)
        self.assertEqual(basename, "model.gguf")

    def test_subfolder_path(self):
        cls = self._mgr()
        subfolder, basename = cls._normalize_hf_filename("UD-Q5_K_XL/model.gguf")
        self.assertEqual(subfolder, "UD-Q5_K_XL")
        self.assertEqual(basename, "model.gguf")

    def test_subfolder_plus_split(self):
        cls = self._mgr()
        subfolder, basename = cls._normalize_hf_filename(
            "UD-Q5_K_XL/Qwen3.5-00001-of-00003.gguf"
        )
        self.assertEqual(subfolder, "UD-Q5_K_XL")
        self.assertEqual(basename, "Qwen3.5-00001-of-00003.gguf")

    def test_strips_whitespace(self):
        cls = self._mgr()
        subfolder, basename = cls._normalize_hf_filename("  subdir/model.gguf  ")
        self.assertEqual(subfolder, "subdir")
        self.assertEqual(basename, "model.gguf")


class TestDetectShardInfo(unittest.TestCase):
    """Tests for LocalModelManager._detect_shard_info."""

    def _cls(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager

    def test_detects_split_gguf(self):
        result = self._cls()._detect_shard_info("model-00001-of-00003.gguf")
        self.assertIsNotNone(result)
        prefix, shard_num, total_shards, shard_w, total_w, suffix = result
        self.assertEqual(prefix, "model")
        self.assertEqual(shard_num, 1)
        self.assertEqual(total_shards, 3)
        self.assertEqual(shard_w, 5)
        self.assertEqual(total_w, 5)
        self.assertEqual(suffix, ".gguf")

    def test_detects_non_first_shard(self):
        result = self._cls()._detect_shard_info("model-00003-of-00003.gguf")
        self.assertIsNotNone(result)
        _, shard_num, total_shards, _sw, _tw, _ = result
        self.assertEqual(shard_num, 3)
        self.assertEqual(total_shards, 3)

    def test_degenerate_single_shard(self):
        result = self._cls()._detect_shard_info("model-00001-of-00001.gguf")
        self.assertIsNotNone(result)
        _, shard_num, total_shards, _sw, _tw, _ = result
        self.assertEqual(shard_num, 1)
        self.assertEqual(total_shards, 1)

    def test_non_split_filename_returns_none(self):
        self.assertIsNone(self._cls()._detect_shard_info("model-Q4_K_M.gguf"))
        self.assertIsNone(self._cls()._detect_shard_info("model.gguf"))

    def test_case_insensitive(self):
        result = self._cls()._detect_shard_info("model-00001-of-00002.GGUF")
        self.assertIsNotNone(result)

    def test_detect_preserves_non_standard_width(self):
        """2-digit width must be preserved, not normalized to 5."""
        result = self._cls()._detect_shard_info("model-01-of-03.gguf")
        self.assertIsNotNone(result)
        prefix, shard_num, total, shard_w, total_w, suffix = result
        self.assertEqual(shard_num, 1)
        self.assertEqual(total, 3)
        self.assertEqual(shard_w, 2, "original shard digit width must be preserved")
        self.assertEqual(total_w, 2, "original total digit width must be preserved")


class TestAllShardBasenames(unittest.TestCase):
    """Tests for LocalModelManager._all_shard_basenames."""

    def _cls(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager

    def test_generates_all_shards(self):
        shards = list(self._cls()._all_shard_basenames("model", 3, ".gguf", 5, 5))
        self.assertEqual(len(shards), 3)
        self.assertEqual(shards[0], "model-00001-of-00003.gguf")
        self.assertEqual(shards[1], "model-00002-of-00003.gguf")
        self.assertEqual(shards[2], "model-00003-of-00003.gguf")

    def test_single_shard(self):
        shards = list(self._cls()._all_shard_basenames("model", 1, ".gguf", 5, 5))
        self.assertEqual(shards, ["model-00001-of-00001.gguf"])

    def test_non_standard_width_preserved(self):
        """2-digit width shards reconstruct with the same width."""
        shards = list(self._cls()._all_shard_basenames("model", 3, ".gguf", 2, 2))
        self.assertEqual(shards, [
            "model-01-of-03.gguf",
            "model-02-of-03.gguf",
            "model-03-of-03.gguf",
        ])

    def test_defaults_to_5_width(self):
        """Default shard/total width is 5 (backward compat)."""
        shards = list(self._cls()._all_shard_basenames("m", 2, ".gguf"))
        self.assertEqual(shards[0], "m-00001-of-00002.gguf")


class TestDownloadModelSplitGGUF(unittest.TestCase):
    """Tests for LocalModelManager.download_model with split and subfolder GGUFs."""

    def _make_mgr(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager()

    def test_non_first_shard_raises_value_error(self):
        mgr = self._make_mgr()
        mock_hf = MagicMock()
        with patch.dict("sys.modules", {"huggingface_hub": mock_hf,
                                        "tqdm": MagicMock(), "tqdm.auto": MagicMock()}):
            mock_hf.hf_hub_download = MagicMock(return_value="/tmp/model.gguf")
            # Simulate "from huggingface_hub import hf_hub_download" in the method
            import builtins
            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "huggingface_hub":
                    return mock_hf
                if name == "tqdm.auto":
                    m = MagicMock()
                    m.tqdm = MagicMock
                    return m
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                with self.assertRaises(ValueError) as ctx:
                    mgr.download_model("owner/repo", "UD-Q5/model-00002-of-00003.gguf")
        self.assertIn("first shard", str(ctx.exception))
        self.assertIn("00001", str(ctx.exception))

    def test_normalize_helpers_combined(self):
        """Verify that subfolder + split shard normalizes correctly together."""
        from ouroboros.local_model import LocalModelManager
        subfolder, basename = LocalModelManager._normalize_hf_filename(
            "UD-Q5_K_XL/Qwen3.5-122B-00001-of-00003.gguf"
        )
        self.assertEqual(subfolder, "UD-Q5_K_XL")
        shard_info = LocalModelManager._detect_shard_info(basename)
        self.assertIsNotNone(shard_info)
        prefix, shard_num, total_shards, shard_w, total_w, suffix = shard_info
        self.assertEqual(shard_num, 1)
        self.assertEqual(total_shards, 3)
        # Generate all shard basenames and verify they all share the subfolder
        shards = list(LocalModelManager._all_shard_basenames(prefix, total_shards, suffix, shard_w, total_w))
        self.assertEqual(len(shards), 3)
        self.assertTrue(shards[0].endswith("-00001-of-00003.gguf"))
        self.assertTrue(shards[2].endswith("-00003-of-00003.gguf"))

    def test_download_split_gguf_downloads_all_shards(self):
        """All shards are downloaded; first shard path is returned."""
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        downloaded_files = []
        expected_paths = [
            "/cache/model-00001-of-00002.gguf",
            "/cache/model-00002-of-00002.gguf",
        ]

        import builtins
        real_import = builtins.__import__

        def fake_hf_hub_download(repo_id, filename, subfolder=None, resume_download=True,
                                 tqdm_class=None):
            idx = len(downloaded_files)
            downloaded_files.append((repo_id, filename, subfolder))
            return expected_paths[idx]

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.hf_hub_download = fake_hf_hub_download
                return mod
            if name == "tqdm.auto":
                m = MagicMock()
                m.tqdm = MagicMock
                return m
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = mgr.download_model("owner/repo", "model-00001-of-00002.gguf")

        self.assertEqual(result, expected_paths[0])
        self.assertEqual(len(downloaded_files), 2)
        self.assertEqual(downloaded_files[0][1], "model-00001-of-00002.gguf")
        self.assertEqual(downloaded_files[1][1], "model-00002-of-00002.gguf")

    def test_download_split_gguf_global_progress_fractions(self):
        """Progress callback receives correct global fractions across shards."""
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        progress_values = []
        call_count = [0]

        import builtins
        real_import = builtins.__import__

        class FakeTqdm:
            def __init__(self, *a, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        captured_tqdm_classes = []

        def fake_hf_hub_download(repo_id, filename, subfolder=None, resume_download=True,
                                 tqdm_class=None):
            captured_tqdm_classes.append(tqdm_class)
            call_count[0] += 1
            return f"/cache/{filename}"

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.hf_hub_download = fake_hf_hub_download
                return mod
            if name == "tqdm.auto":
                m = MagicMock()
                m.tqdm = FakeTqdm
                return m
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            mgr.download_model(
                "owner/repo",
                "model-00001-of-00002.gguf",
                progress_cb=progress_values.append,
            )

        # Two shards should have been downloaded
        self.assertEqual(call_count[0], 2)
        # Each tqdm_class should have been created (not None)
        self.assertEqual(len(captured_tqdm_classes), 2)
        # Final progress should be 1.0
        self.assertEqual(mgr._download_progress, 1.0)
        self.assertEqual(progress_values[-1], 1.0)

    def test_download_subfolder_single_file(self):
        """Subfolder path without split: hf_hub_download called with subfolder kwarg."""
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        call_args_list = []

        import builtins
        real_import = builtins.__import__

        def fake_hf_hub_download(repo_id, filename, subfolder=None, resume_download=True,
                                 tqdm_class=None):
            call_args_list.append({"repo_id": repo_id, "filename": filename, "subfolder": subfolder})
            return "/cache/model.gguf"

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.hf_hub_download = fake_hf_hub_download
                return mod
            if name == "tqdm.auto":
                m = MagicMock()
                m.tqdm = MagicMock
                return m
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = mgr.download_model("owner/repo", "UD-Q5/model.gguf")

        self.assertEqual(result, "/cache/model.gguf")
        self.assertEqual(len(call_args_list), 1)
        self.assertEqual(call_args_list[0]["subfolder"], "UD-Q5")
        self.assertEqual(call_args_list[0]["filename"], "model.gguf")


class TestResolveHfPath(unittest.TestCase):
    """Tests for LocalModelManager._resolve_hf_path auto-subfolder resolution."""

    def setUp(self):
        from ouroboros.local_model import LocalModelManager
        self.mgr = LocalModelManager()
        self.resolve = LocalModelManager._resolve_hf_path

    def _patch_list_repo_files(self, return_value=None, side_effect=None):
        """Patch the list_repo_files import inside _resolve_hf_path."""
        import ouroboros.local_model as lm_module
        mock_fn = MagicMock(return_value=return_value, side_effect=side_effect)

        def fake_import(name, fromlist=(), *args, **kwargs):
            if name == "huggingface_hub" and "list_repo_files" in (fromlist or ()):
                class _FakeHF:
                    list_repo_files = staticmethod(mock_fn)
                return _FakeHF()
            return real_import(name, fromlist, *args, **kwargs)

        import builtins
        real_import = builtins.__import__
        return patch("builtins.__import__", side_effect=fake_import), mock_fn

    def test_passthrough_when_slash_present(self):
        """If filename already has a slash, return it unchanged without any API call."""
        # No need to mock — the method returns immediately when '/' is in filename
        result = self.resolve("owner/repo", "UD-Q5_K_XL/model-00001-of-00003.gguf")
        self.assertEqual(result, "UD-Q5_K_XL/model-00001-of-00003.gguf")

    def test_auto_resolves_subfolder_from_hf(self):
        """When filename has no slash, list_repo_files is queried and subfolder is added."""
        import builtins
        real_import = builtins.__import__
        mock_list = MagicMock(return_value=[
            "UD-Q5_K_XL/model-00001-of-00003.gguf",
            "UD-Q5_K_XL/model-00002-of-00003.gguf",
            "UD-Q5_K_XL/model-00003-of-00003.gguf",
            "README.md",
        ])

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.list_repo_files = mock_list
                return mod
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = self.resolve("owner/repo", "model-00001-of-00003.gguf")
        self.assertEqual(result, "UD-Q5_K_XL/model-00001-of-00003.gguf")

    def test_returns_original_when_not_found(self):
        """If no match in repo, return original filename (fail-open)."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.list_repo_files = MagicMock(return_value=["README.md"])
                return mod
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = self.resolve("owner/repo", "unknown-model.gguf")
        self.assertEqual(result, "unknown-model.gguf")

    def test_returns_original_on_network_error(self):
        """If list_repo_files raises, return original filename (fail-open)."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.list_repo_files = MagicMock(side_effect=Exception("network error"))
                return mod
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = self.resolve("owner/repo", "model.gguf")
        self.assertEqual(result, "model.gguf")

    def test_raises_on_ambiguous_match(self):
        """When multiple paths match the bare filename, raise ValueError with candidates."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.list_repo_files = MagicMock(return_value=[
                    "Q4_K_M/model.gguf",
                    "Q5_K_XL/model.gguf",
                ])
                return mod
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(ValueError) as ctx:
                self.resolve("owner/repo", "model.gguf")
        self.assertIn("Ambiguous filename", str(ctx.exception))
        self.assertIn("Q4_K_M/model.gguf", str(ctx.exception))
        self.assertIn("Q5_K_XL/model.gguf", str(ctx.exception))

    def test_flat_file_at_root_not_doubled(self):
        """A file that lives at root (no subfolder) is returned as-is."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "huggingface_hub":
                mod = MagicMock()
                mod.list_repo_files = MagicMock(return_value=["model.gguf", "README.md"])
                return mod
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = self.resolve("owner/repo", "model.gguf")
        self.assertEqual(result, "model.gguf")


if __name__ == "__main__":
    unittest.main()
