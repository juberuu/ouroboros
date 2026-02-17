"""Smoke tests for VLM (Vision Language Model) support."""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import pathlib

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestLLMVisionQuery(unittest.TestCase):
    """Test LLMClient.vision_query() message format."""

    def test_vision_query_url_format(self):
        """vision_query builds correct message format for URL images."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")

        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "I see a test image."}, {"prompt_tokens": 10, "completion_tokens": 5}

        client.chat = mock_chat

        text, usage = client.vision_query(
            prompt="What do you see?",
            images=[{"url": "https://example.com/test.png"}],
            model="anthropic/claude-sonnet-4.6",
        )

        self.assertEqual(text, "I see a test image.")
        self.assertEqual(len(captured_messages), 1)
        content = captured_messages[0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "What do you see?")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("url", content[1]["image_url"])
        self.assertEqual(content[1]["image_url"]["url"], "https://example.com/test.png")

    def test_vision_query_base64_format(self):
        """vision_query builds correct data URI for base64 images."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")
        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "Base64 image description."}, {}

        client.chat = mock_chat

        fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        text, _ = client.vision_query(
            prompt="Describe this.",
            images=[{"base64": fake_b64, "mime": "image/png"}],
        )

        self.assertEqual(text, "Base64 image description.")
        content = captured_messages[0]["content"]
        image_part = content[1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn(fake_b64, image_part["image_url"]["url"])

    def test_vision_query_multiple_images(self):
        """vision_query handles multiple images in one call."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")
        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "Two images."}, {}

        client.chat = mock_chat

        client.vision_query(
            prompt="Compare these images.",
            images=[
                {"url": "https://example.com/img1.png"},
                {"url": "https://example.com/img2.png"},
            ],
        )

        content = captured_messages[0]["content"]
        self.assertEqual(len(content), 3)  # text + 2 images

    def test_vision_query_empty_images(self):
        """vision_query works with no images (just text)."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            return {"content": "Text only."}, {}

        client.chat = mock_chat

        text, _ = client.vision_query(prompt="Hello", images=[])
        self.assertEqual(text, "Text only.")


class TestAnalyzeScreenshotTool(unittest.TestCase):
    """Test the analyze_screenshot tool."""

    def _make_ctx(self, with_screenshot=True):
        from ouroboros.tools.registry import ToolContext, BrowserState
        ctx = MagicMock(spec=ToolContext)
        ctx.browser_state = BrowserState()
        ctx.event_queue = None
        ctx.task_id = "test-task"
        ctx.current_task_type = "task"
        if with_screenshot:
            ctx.browser_state.last_screenshot_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        else:
            ctx.browser_state.last_screenshot_b64 = None
        return ctx

    def test_no_screenshot_returns_warning(self):
        """analyze_screenshot returns warning when no screenshot available."""
        from ouroboros.tools.vision import _analyze_screenshot

        ctx = self._make_ctx(with_screenshot=False)
        result = _analyze_screenshot(ctx, prompt="What do you see?")
        self.assertIn("⚠️", result)
        self.assertIn("screenshot", result.lower())

    def test_analyze_screenshot_calls_vlm(self):
        """analyze_screenshot calls VLM with the screenshot base64."""
        from ouroboros.tools.vision import _analyze_screenshot

        ctx = self._make_ctx(with_screenshot=True)

        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("Beautiful UI.", {"prompt_tokens": 100, "completion_tokens": 20})
            mock_get_client.return_value = mock_client

            result = _analyze_screenshot(ctx, prompt="Describe the UI.")

        self.assertEqual(result, "Beautiful UI.")
        mock_client.vision_query.assert_called_once()
        call_kwargs = mock_client.vision_query.call_args
        # Check that base64 image was passed
        images = call_kwargs[1].get("images") or call_kwargs[0][1]
        self.assertEqual(len(images), 1)
        self.assertIn("base64", images[0])


class TestVlmQueryTool(unittest.TestCase):
    """Test the vlm_query tool."""

    def _make_ctx(self):
        from ouroboros.tools.registry import ToolContext, BrowserState
        ctx = MagicMock(spec=ToolContext)
        ctx.browser_state = BrowserState()
        ctx.event_queue = None
        ctx.task_id = "test-task"
        ctx.current_task_type = "task"
        return ctx

    def test_vlm_query_requires_image(self):
        """vlm_query returns error when no image provided."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        result = _vlm_query(ctx, prompt="What is this?")
        self.assertIn("⚠️", result)

    def test_vlm_query_with_url(self):
        """vlm_query calls VLM with URL image."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()

        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("A logo.", {})
            mock_get_client.return_value = mock_client

            result = _vlm_query(ctx, prompt="What is the logo?", image_url="https://example.com/logo.png")

        self.assertEqual(result, "A logo.")
        call_kwargs = mock_client.vision_query.call_args
        images = call_kwargs[1].get("images") or call_kwargs[0][1]
        self.assertEqual(images[0]["url"], "https://example.com/logo.png")

    def test_vlm_query_tool_registered(self):
        """vlm_query and analyze_screenshot tools are properly registered."""
        import pathlib
        from ouroboros.tools.registry import ToolRegistry

        registry = ToolRegistry(
            repo_dir=pathlib.Path("/tmp"),
            drive_root=pathlib.Path("/tmp"),
        )
        tools = registry.available_tools()
        self.assertIn("analyze_screenshot", tools, "analyze_screenshot must be registered")
        self.assertIn("vlm_query", tools, "vlm_query must be registered")


if __name__ == "__main__":
    unittest.main()
