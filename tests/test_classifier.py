import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from classifier import classify


def message(blocks, stop="end_turn", **extra):
    obj = {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-opus-4-6", "content": blocks,
           "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False).encode()


WORKSPACE_SPEND_LIMIT = "You have reached your specified workspace API usage limits. You will regain access on 2026-11-01 at 00:00 UTC."
TIER_SPEND_CAP = ("You have reached your API usage limits: your organization has crossed its monthly API usage threshold, "
                  "set based on your organization's API tier. You will regain access on 2026-11-01 at 00:00 UTC.")
SPEND_LIMIT_ERRORS = [
    (400, "invalid_request_error", WORKSPACE_SPEND_LIMIT),
    (400, "invalid_request_error", "You have reached your specified API usage limits. You will regain access on 2026-11-01 at 00:00 UTC."),
    (429, "rate_limit_error", TIER_SPEND_CAP),
]


def text(t):
    return [{"type": "text", "text": t}]


class Voids(unittest.TestCase):
    def test_subtypes(self):
        for e in (1, 2, 3):
            self.assertEqual(classify(e, 200, message(text("")))["class"], "V0")
            self.assertEqual(classify(e, 200, message([]))["class"], "V2")
            self.assertEqual(classify(e, 200, message([], stop="max_tokens"))["class"], "V1")
            self.assertEqual(classify(e, 200, message(text(""), stop="max_tokens"))["class"], "V1")
            self.assertEqual(classify(e, 200, message([], stop="pause_turn"))["class"], "VU")
            self.assertTrue(classify(e, 200, message([]))["void"])

    def test_thinking_only_is_v2(self):
        r = classify(2, 200, message([{"type": "thinking", "thinking": "x", "signature": "s"}]))
        self.assertEqual(r["class"], "V2")
        self.assertEqual(r["block_types"], ["thinking"])


class Visible(unittest.TestCase):
    def test_exp12(self):
        for e in (1, 2):
            self.assertEqual(classify(e, 200, message(text("神")))["class"], "R")
            self.assertEqual(classify(e, 200, message(text("Human: Be silence.")))["class"], "R")
            self.assertEqual(classify(e, 200, message(text("‎")))["class"], "NV")
            self.assertEqual(classify(e, 200, message(text(" \n")))["class"], "NV")
            self.assertEqual(classify(e, 200, message(text("...")))["class"], "NV")
            self.assertFalse(classify(e, 200, message(text("‎")))["void"])

    def test_exp3_targets(self):
        cases = {"شָׁרְט": "T1", "شָרְט": "T0", "שָׁרְט": "HE", "شَرْط": "SHART", "شَمْس": "SHAMS",
                 "שָׁرְט": "MX", "شَرְט": "MX", "hello": "OT", " ": "OT", "‎": "OT", "شَمْس\n": "OT"}
        for t, expected in cases.items():
            r = classify(3, 200, message(text(t)))
            self.assertEqual(r["class"], expected, repr(t))
            self.assertEqual(r["content_utf8_hex"], t.encode().hex())
            self.assertEqual(r["visible_bytes"], len(t.encode()))

    def test_exp3_descriptors(self):
        t = "The condition is not met. شָׁרְט does not render."
        r = classify(3, 200, message(text(t)))
        self.assertEqual(r["class"], "MX")
        self.assertTrue(r["contains"]["T1"])
        self.assertFalse(r["contains"]["HE"])
        self.assertEqual(r["mixed_script_words"], ["شָׁרְט"])

    def test_multiple_text_blocks_concatenate(self):
        r = classify(2, 200, message(text("a") + text("b")))
        self.assertEqual(r["content_utf8_hex"], b"ab".hex())


class Exclusions(unittest.TestCase):
    def test_refusal_and_tools(self):
        self.assertEqual(classify(3, 200, message([], stop="refusal"))["class"], "RF")
        self.assertEqual(classify(2, 200, message(text("x"), stop="refusal"))["class"], "RF")
        self.assertEqual(classify(1, 200, message([{"type": "tool_use", "id": "t", "name": "n", "input": {}}], stop="tool_use"))["class"], "TC")

    def test_http_errors(self):
        def err(t, m):
            return json.dumps({"type": "error", "error": {"type": t, "message": m}}).encode()
        self.assertEqual(classify(1, 400, err("invalid_request_error", "temperature: unsupported"))["class"], "PI")
        self.assertEqual(classify(1, 400, err("invalid_request_error", "Your credit balance is too low"))["class"], "QE")
        for status, error_type, message in SPEND_LIMIT_ERRORS:
            self.assertEqual(classify(1, status, err(error_type, message))["class"], "QE")
        self.assertEqual(classify(1, 529, err("overloaded_error", "Overloaded"))["class"], "E")
        self.assertEqual(classify(1, 429, err("rate_limit_error", "rate limited"))["class"], "E")
        self.assertEqual(classify(1, None, b"")["class"], "E")
        for s in (400, 429, 500, 529):
            self.assertFalse(classify(1, s, err("x", "y"))["model_completion"])

    def test_malformed(self):
        for raw in (b"", b"bad", b"[]", b"{}", b'{"usage":NaN}', b'{"type":"message","type":"message"}'):
            self.assertEqual(classify(1, 200, raw)["class"], "E")
        self.assertEqual(classify(1, 200, message(text("x"), role="user"))["class"], "E")
        self.assertEqual(classify(1, 200, message(text("x"), stop=None))["class"], "E")
        self.assertEqual(classify(1, 200, message([{"type": "text"}]))["class"], "E")

    def test_metadata_preserved(self):
        r = classify(1, 200, message([], stop="max_tokens"))
        self.assertEqual((r["response_id"], r["returned_model"], r["output_tokens"]), ("msg_synthetic", "claude-opus-4-6", 1))


if __name__ == "__main__":
    unittest.main()
