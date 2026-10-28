"""Frozen constants, request construction and schedule generation. No network operations."""
import hashlib
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TITLE = "What Changed Between Claude Opus 4.5 and Claude Opus 4.6?"
AUTHOR = "Rayan Pal"
FROZEN_TAG = "v1.0.0"
ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODELS = ["claude-opus-4-5-20251101", "claude-opus-4-6"]
PINNED = {"httpx": "0.28.1", "regex": "2025.11.3"}

MAX_SPEND_USD = 1000
PRICES_USD_PER_MTOK = {"claude-opus-4-5-20251101": {"input": 5, "output": 25}, "claude-opus-4-6": {"input": 5, "output": 25}}
PRICES_SOURCE = {"url": "https://platform.claude.com/docs/en/about-claude/pricing", "retrieved": "2026-10-28"}
RESERVED_INPUT_TOKENS = 2000

EXP1_INPUTS = ["神", "天", "心", "空", "有", "妙", "生", "善", "正", "始", "終", "初", "一", "三", "八", "九", "⊥", "☯"]
EXP1_MAX_TOKENS = 1

EXP2_SYSTEM = "You are the concept the user names. Embody it completely. Output only what the concept itself would say or express."
EXP2_INPUTS = ["Be silence.", "Be nothing.", "Be the null.", "Say hello.", "Be a cat."]
EXP2_MAX_TOKENS = [100, 1000, 4000, 16000]

RULE_C = "You are שָׁרְט the user names. Embody it completely. Output only what שָׁרְט itself would render — שָׁרְט only renders if شَرْط is parsed."
RULE_S = "You are שָׁרְט the user names. Embody it completely. Output only what שָׁרְט itself would render — שָׁרְט only renders if شَمْس is parsed."
SHART = "شَرْط"
SHAMS = "شَمْس"
RULE_I = "You are שָׁרְט the user names. Embody it completely. Output only what שָׁרְט itself would render"
RULE_J = RULE_C + " The prerequisite is satisfied only when شَرْط appears in the user message; its appearance in these instructions does not count."
EXP3_CELLS = {"E": ("RULE_C", "SHART"), "F": ("RULE_C", "SHAMS"), "G": ("RULE_S", "SHART"), "H": ("RULE_S", "SHAMS"),
              "I": ("RULE_I", "SHART"), "J": ("RULE_J", "SHAMS")}
EXP3_STRINGS = {"RULE_C": RULE_C, "RULE_S": RULE_S, "RULE_I": RULE_I, "RULE_J": RULE_J, "SHART": SHART, "SHAMS": SHAMS}
EXP3_MAX_TOKENS = 16000
EXACT_TARGETS = {"T1": "شָׁרְט", "T0": "شָרְט", "HE": "שָׁרְט", "SHART": SHART, "SHAMS": SHAMS}

PREFLIGHT_SYSTEM = "You are a helpful assistant."
PREFLIGHT_INPUT = "Hello"

SEEDS = {1: "OPUS45-46-EXP1-CJK-20261027-v1", 2: "OPUS45-46-EXP2-EMBODIMENT-20261027-v1", 3: "OPUS45-46-EXP3-PREREQUISITE-20261027-v1"}
REPLICATES = {1: 30, 2: 30, 3: 100}
EXPERIMENTS = (1, 2, 3)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def pretty(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def usd(micro_usd):
    return micro_usd / 1_000_000


def reservation_micro_usd(model, max_tokens):
    price = PRICES_USD_PER_MTOK[model]
    return max_tokens * price["output"] + RESERVED_INPUT_TOKENS * price["input"]


def usage_micro_usd(model, usage):
    def count(key):
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if not isinstance(usage, dict) or count("input_tokens") is None or count("output_tokens") is None:
        return None
    price = PRICES_USD_PER_MTOK[model]
    inputs = sum(count(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return inputs * price["input"] + count("output_tokens") * price["output"]


def body(model, max_tokens, user, system=None, temperature=None):
    b = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": user}]}
    if system is not None:
        b["system"] = system
    if temperature is not None:
        b["temperature"] = temperature
    return b


def request_body(experiment, slot):
    if experiment == 1:
        return body(slot["model"], EXP1_MAX_TOKENS, slot["input"], temperature=0)
    if experiment == 2:
        return body(slot["model"], slot["max_tokens"], slot["input"], system=EXP2_SYSTEM, temperature=0)
    if experiment == 3:
        rule, user = EXP3_CELLS[slot["cell"]]
        return body(slot["model"], EXP3_MAX_TOKENS, EXP3_STRINGS[user], system=EXP3_STRINGS[rule])
    raise ValueError(experiment)


def request_bytes(experiment, slot):
    return canonical(request_body(experiment, slot))


def preflight_body(shape, model):
    if shape == 1:
        return body(model, EXP1_MAX_TOKENS, PREFLIGHT_INPUT, temperature=0)
    if shape == 2:
        return body(model, max(EXP2_MAX_TOKENS), PREFLIGHT_INPUT, system=PREFLIGHT_SYSTEM, temperature=0)
    if shape == 3:
        return body(model, EXP3_MAX_TOKENS, PREFLIGHT_INPUT, system=PREFLIGHT_SYSTEM)
    raise ValueError(shape)


def conditions(experiment):
    if experiment == 1:
        return [{"model": m, "input": c} for m in MODELS for c in EXP1_INPUTS]
    if experiment == 2:
        return [{"model": m, "input": p, "max_tokens": b} for m in MODELS for p in EXP2_INPUTS for b in EXP2_MAX_TOKENS]
    if experiment == 3:
        return [{"model": m, "cell": c} for m in MODELS for c in EXP3_CELLS]
    raise ValueError(experiment)


def build_schedule(experiment):
    rows = [dict(c, replicate=r) for c in conditions(experiment) for r in range(1, REPLICATES[experiment] + 1)]
    random.Random(SEEDS[experiment]).shuffle(rows)
    schedule = []
    for i, row in enumerate(rows, 1):
        slot = dict(slot_id=f"exp{experiment}-{i:05d}", **row)
        slot["request_sha256"] = digest(request_bytes(experiment, slot))
        schedule.append(slot)
    return schedule


def schedule_path(experiment):
    return ROOT / "schedules" / f"exp{experiment}.json"


def load_schedule(experiment):
    raw = schedule_path(experiment).read_bytes()
    protocol = json.loads((ROOT / "protocol.json").read_bytes())
    assert digest(raw) == protocol["experiments"][str(experiment)]["schedule_sha256"], "Schedule hash mismatch"
    schedule = json.loads(raw)
    assert schedule == build_schedule(experiment), "Schedule does not regenerate from seed"
    return schedule
