import unittest

from lelamp.agent.openclaw import build_messages, sanitize_spoken_content


class OpenClawOutputTests(unittest.TestCase):
    def test_request_marks_content_as_direct_speech(self):
        messages = build_messages("而白")
        self.assertEqual(messages[-1], {"role": "user", "content": "而白"})
        self.assertTrue(any("直接由台灯朗读" in item["content"] for item in messages))

    def test_removes_think_block(self):
        self.assertEqual(
            sanitize_spoken_content("<think>分析一下</think>行，知道了。"),
            "行，知道了。",
        )

    def test_collapses_leaked_ambiguity_analysis(self):
        leaked = (
            '用户输入“而白”，这很可能是错字。\n\n'
            "考虑到语音输入可能不完整，按照规则应该要求重说。\n\n"
            "刚才没听清，你再说一遍？"
        )
        self.assertEqual(sanitize_spoken_content(leaked), "刚才没听清，你再说一遍？")

    def test_preserves_normal_short_answer(self):
        self.assertEqual(sanitize_spoken_content("行，给你定好了。"), "行，给你定好了。")

    def test_blocks_english_tool_planning_leak(self):
        leaked = (
            'The user says "有我点个头了" which seems like a voice recognition error.\n\n'
            "According to the rules, I should use a tool.\n\n"
            'Let me call lelamp_queue_expression with name="nod".'
        )
        self.assertEqual(sanitize_spoken_content(leaked), "这次动作没执行成功，你再说一遍。")

    def test_blocks_no_reply_control_token(self):
        self.assertEqual(
            sanitize_spoken_content("直接回答要求重说即可。NO_REPLY"),
            "这次动作没执行成功，你再说一遍。",
        )

    def test_blocks_bare_tool_name(self):
        self.assertEqual(
            sanitize_spoken_content("lelamp_enter_work_light"),
            "这次动作没执行成功，你再说一遍。",
        )


if __name__ == "__main__":
    unittest.main()
