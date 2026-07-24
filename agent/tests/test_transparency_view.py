from __future__ import annotations

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class TransparencyViewTests(unittest.TestCase):
    def test_all_public_inference_actions_have_specific_badges(self) -> None:
        transparency_view_source = (REPOSITORY_ROOT / "agent" / "run_agent.py").read_text(encoding="utf-8")
        expected_labels = (
            "TEE BUNDLE FETCH",
            "TEE IMAGE PICK",
            "TEE INFERENCE",
            "ZK BUNDLE FETCH",
            "ZK IMAGE PICK",
            "ZK INFERENCE",
        )

        for label in expected_labels:
            with self.subTest(label=label):
                self.assertIn(label, transparency_view_source)

    def test_chat_render_preserves_position_instead_of_scrolling_to_bottom(self) -> None:
        ui_source = (REPOSITORY_ROOT / "ui" / "index.html").read_text(encoding="utf-8")

        self.assertIn("const previousScrollTop = messagesEl.scrollTop;", ui_source)
        self.assertNotIn("messagesEl.scrollTop = messagesEl.scrollHeight;", ui_source)


if __name__ == "__main__":
    unittest.main()
