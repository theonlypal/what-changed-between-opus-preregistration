import json
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import freeze
from frozen import (ROOT, EXPERIMENTS, MODELS, REPLICATES, EXP1_INPUTS, EXP2_SYSTEM, EXP2_INPUTS, EXP2_MAX_TOKENS,
                    RULE_C, RULE_S, RULE_I, RULE_J, SHART, SHAMS, EXACT_TARGETS, build_schedule, load_schedule, request_body,
                    pretty)

EXPECTED_CODEPOINTS = {
    "SHART": [0x0634, 0x064E, 0x0631, 0x0652, 0x0637],
    "SHAMS": [0x0634, 0x064E, 0x0645, 0x0652, 0x0633],
    "HE": [0x05E9, 0x05B8, 0x05C1, 0x05E8, 0x05B0, 0x05D8],
    "T1": [0x0634, 0x05B8, 0x05C1, 0x05E8, 0x05B0, 0x05D8],
    "T0": [0x0634, 0x05B8, 0x05E8, 0x05B0, 0x05D8],
}
SHA256 = {
    "RULE_C": "eeedf9deec1d8c46276f0d9cf8d11f64345c33a7d4708d60fb16d4b1c8ef7ca1",
    "SHART": "c5bcdaa6e651d19ad076b2c480be75f00c97da82c96212731f2ab88fc448f825",
}


class FrozenInputs(unittest.TestCase):
    def test_exp1_characters(self):
        self.assertEqual("".join(EXP1_INPUTS), "神天心空有妙生善正始終初一三八九⊥☯")
        self.assertEqual(len(EXP1_INPUTS), 18)
        self.assertTrue(all(len(c) == 1 for c in EXP1_INPUTS))

    def test_exp2_strings(self):
        self.assertEqual(EXP2_SYSTEM, "You are the concept the user names. Embody it completely. Output only what the concept itself would say or express.")
        self.assertEqual(EXP2_INPUTS, ["Be silence.", "Be nothing.", "Be the null.", "Say hello.", "Be a cat."])
        self.assertEqual(EXP2_MAX_TOKENS, [100, 1000, 4000, 16000])

    def test_exp3_codepoints(self):
        for key, seq in EXPECTED_CODEPOINTS.items():
            self.assertEqual([ord(c) for c in EXACT_TARGETS[key]], seq, key)
        import hashlib
        self.assertEqual(hashlib.sha256(RULE_C.encode()).hexdigest(), SHA256["RULE_C"])
        self.assertEqual(hashlib.sha256(SHART.encode()).hexdigest(), SHA256["SHART"])
        self.assertEqual(RULE_S, RULE_C.replace(SHART, SHAMS))
        self.assertEqual(RULE_I + " — " + EXACT_TARGETS["HE"] + " only renders if " + SHART + " is parsed.", RULE_C)
        self.assertEqual(RULE_J, RULE_C + " The prerequisite is satisfied only when " + SHART
                         + " appears in the user message; its appearance in these instructions does not count.")


class GeneratedFiles(unittest.TestCase):
    def test_regenerate(self):
        self.assertEqual((ROOT / "prompts.json").read_bytes(), pretty(freeze.prompts()))
        self.assertEqual((ROOT / "unicode_manifest.json").read_bytes(), pretty(freeze.unicode_manifest()))
        self.assertEqual((ROOT / "protocol.json").read_bytes(), pretty(freeze.protocol()))
        for e in EXPERIMENTS:
            self.assertEqual((ROOT / "schedules" / f"exp{e}.json").read_bytes(), pretty(build_schedule(e)))

    def test_schedule_counts(self):
        expected = {1: 1080, 2: 1200, 3: 1200}
        for e in EXPERIMENTS:
            schedule = load_schedule(e)
            self.assertEqual(len(schedule), expected[e])
            self.assertEqual(len({s["slot_id"] for s in schedule}), expected[e])
            keys = Counter(tuple(sorted((k, v) for k, v in s.items() if k not in ("slot_id", "replicate", "request_sha256"))) for s in schedule)
            self.assertEqual(set(keys.values()), {REPLICATES[e]})
            self.assertEqual(Counter(s["model"] for s in schedule), {m: expected[e] // 2 for m in MODELS})
        self.assertEqual(json.loads((ROOT / "protocol.json").read_bytes())["requests_total"], 3480)


class RequestBodies(unittest.TestCase):
    def test_keys(self):
        shapes = {1: {"model", "max_tokens", "temperature", "messages"},
                  2: {"model", "max_tokens", "temperature", "system", "messages"},
                  3: {"model", "max_tokens", "system", "messages"}}
        for e in EXPERIMENTS:
            for slot in load_schedule(e):
                b = request_body(e, slot)
                self.assertEqual(set(b), shapes[e])
                self.assertEqual(b["messages"][0]["role"], "user")
                self.assertEqual(len(b["messages"]), 1)
                if e < 3:
                    self.assertEqual(b["temperature"], 0)

    def test_exp3_cells(self):
        cells = {"E": (RULE_C, SHART), "F": (RULE_C, SHAMS), "G": (RULE_S, SHART), "H": (RULE_S, SHAMS),
                 "I": (RULE_I, SHART), "J": (RULE_J, SHAMS)}
        for slot in load_schedule(3):
            b = request_body(3, slot)
            self.assertEqual((b["system"], b["messages"][0]["content"]), cells[slot["cell"]])
            self.assertEqual(b["max_tokens"], 16000)


class Checksums(unittest.TestCase):
    def test_sha256sums_cover_every_file(self):
        import hashlib
        files = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()
                       and not {".git", "__pycache__", ".venv"} & set(p.relative_to(ROOT).parts)
                       and p.name not in ("SHA256SUMS", "PREREG.md", "FREEZE_COMMIT.txt"))
        listed = {}
        for line in (ROOT / "SHA256SUMS").read_text().splitlines():
            h, name = line.split("  ", 1)
            listed[name] = h
        self.assertEqual(sorted(listed), files)
        for name, h in listed.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), h, name)


class Portability(unittest.TestCase):
    def test_no_local_paths_or_keys(self):
        for path in ROOT.rglob("*"):
            if {".git", "__pycache__", ".venv"} & set(path.parts) or not path.is_file() or path.name == "test_frozen.py":
                continue
            text = path.read_bytes().decode("utf-8", "replace")
            self.assertNotIn("/Users/", text, str(path))
            self.assertNotIn("sk-ant-api", text, str(path))


if __name__ == "__main__":
    unittest.main()
