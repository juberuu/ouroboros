"""
Tests for v6 message routing: single-consumer delivery,
per-task mailbox, and forward_to_worker tool.

Run: pytest tests/test_message_routing.py -v
"""

import json
import pathlib
import sys
import os
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestOwnerInjectPerTask(unittest.TestCase):
    """Test per-task mailbox in owner_inject.py."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.drive_root = pathlib.Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_write_creates_per_task_file(self):
        from ouroboros.owner_inject import write_owner_message, _mailbox_path
        write_owner_message(self.drive_root, "hello", task_id="abc123", msg_id="m1")
        path = _mailbox_path(self.drive_root, "abc123")
        self.assertTrue(path.exists())
        content = path.read_text()
        entry = json.loads(content.strip())
        self.assertEqual(entry["text"], "hello")
        self.assertEqual(entry["msg_id"], "m1")

    def test_drain_reads_only_own_task(self):
        from ouroboros.owner_inject import write_owner_message, drain_owner_messages
        write_owner_message(self.drive_root, "for task A", task_id="taskA", msg_id="m1")
        write_owner_message(self.drive_root, "for task B", task_id="taskB", msg_id="m2")

        msgs_a = drain_owner_messages(self.drive_root, task_id="taskA")
        msgs_b = drain_owner_messages(self.drive_root, task_id="taskB")

        self.assertEqual(msgs_a, ["for task A"])
        self.assertEqual(msgs_b, ["for task B"])

    def test_drain_dedup_with_seen_ids(self):
        from ouroboros.owner_inject import write_owner_message, drain_owner_messages
        write_owner_message(self.drive_root, "msg1", task_id="t1", msg_id="id1")
        write_owner_message(self.drive_root, "msg2", task_id="t1", msg_id="id2")

        seen = set()
        first_read = drain_owner_messages(self.drive_root, task_id="t1", seen_ids=seen)
        self.assertEqual(len(first_read), 2)
        self.assertEqual(seen, {"id1", "id2"})

        write_owner_message(self.drive_root, "msg3", task_id="t1", msg_id="id3")
        second_read = drain_owner_messages(self.drive_root, task_id="t1", seen_ids=seen)
        self.assertEqual(second_read, ["msg3"])
        self.assertIn("id3", seen)

    def test_cleanup_removes_file(self):
        from ouroboros.owner_inject import write_owner_message, cleanup_task_mailbox, _mailbox_path
        write_owner_message(self.drive_root, "hello", task_id="t1", msg_id="m1")
        path = _mailbox_path(self.drive_root, "t1")
        self.assertTrue(path.exists())

        cleanup_task_mailbox(self.drive_root, "t1")
        self.assertFalse(path.exists())

    def test_drain_nonexistent_task_returns_empty(self):
        from ouroboros.owner_inject import drain_owner_messages
        msgs = drain_owner_messages(self.drive_root, task_id="nonexistent")
        self.assertEqual(msgs, [])

    def test_messages_not_cleared_on_read(self):
        """Messages persist after read (append-only). Only cleanup removes them."""
        from ouroboros.owner_inject import write_owner_message, drain_owner_messages, _mailbox_path
        write_owner_message(self.drive_root, "persistent", task_id="t1", msg_id="m1")

        drain_owner_messages(self.drive_root, task_id="t1")

        path = _mailbox_path(self.drive_root, "t1")
        self.assertTrue(path.exists())
        self.assertIn("persistent", path.read_text())


class TestForwardToWorkerTool(unittest.TestCase):
    """Test that forward_to_worker tool is registered."""

    def test_tool_registered(self):
        from ouroboros.tools.registry import ToolRegistry
        registry = ToolRegistry(
            repo_dir=pathlib.Path("/tmp"),
            drive_root=pathlib.Path("/tmp"),
        )
        tools = registry.available_tools()
        self.assertIn("forward_to_worker", tools)


if __name__ == "__main__":
    unittest.main()
