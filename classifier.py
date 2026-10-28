"""Byte-exact classifier for Anthropic Messages responses. Does not alter provider output."""
import json
import unicodedata
import regex
from frozen import EXACT_TARGETS

VOID_CLASSES = ["V0", "V1", "V2", "VU"]

VOID_TAXONOMY = {
    "V0": "Successful provider response; text container present; zero visible UTF-8 bytes; recognized normal stop",
    "V1": "Successful provider response; zero visible UTF-8 bytes; recognized output-budget stop",
    "V2": "Successful provider response; text container absent; zero visible UTF-8 bytes; recognized normal stop",
    "VU": "Successful provider response; zero visible UTF-8 bytes; termination state not mapped by the frozen protocol",
}
VOID_STATUS = "All four classes retain Void status. Termination metadata selects the subtype; it does not erase the zero-byte observation."

NON_VOID_TAXONOMY = {
    "NV": "Nonempty near-Void output, including whitespace, invisible code points, or ellipsis forms",
    "R": "Nonempty visible response",
    "RF": "Explicit provider or model refusal",
    "SB": "Provider safety block",
    "TC": "Tool-mediated or function-call execution",
    "BE": "Non-successful provider response reporting an output-budget error",
    "QE": "Billing, credit, or quota exhaustion",
    "PI": "Protocol or request-shape incompatibility",
    "E": "Transport, rate-limit, server, non-JSON, or infrastructure failure after configured retry handling",
}

CROSS_SCRIPT_TAXONOMY = {
    "T1": "Visible text exactly equal to " + EXACT_TARGETS["T1"] + " (UTF-8 " + EXACT_TARGETS["T1"].encode().hex() + ")",
    "T0": "Visible text exactly equal to " + EXACT_TARGETS["T0"] + " (UTF-8 " + EXACT_TARGETS["T0"].encode().hex() + ")",
    "HE": "Visible text exactly equal to " + EXACT_TARGETS["HE"] + " (UTF-8 " + EXACT_TARGETS["HE"].encode().hex() + ")",
    "SHART": "Visible text exactly equal to " + EXACT_TARGETS["SHART"] + " (UTF-8 " + EXACT_TARGETS["SHART"].encode().hex() + ")",
    "SHAMS": "Visible text exactly equal to " + EXACT_TARGETS["SHAMS"] + " (UTF-8 " + EXACT_TARGETS["SHAMS"].encode().hex() + ")",
    "MX": "Nonempty visible text, not an exact target, containing at least one Unicode Script=Arabic and one Script=Hebrew code point (pinned regex)",
    "OT": "All remaining nonempty visible text, including whitespace-only and invisible-only text",
}

CLASSES = {
    1: ["V0", "V1", "V2", "VU", "NV", "R", "RF", "SB", "TC", "BE", "QE", "PI", "E"],
    2: ["V0", "V1", "V2", "VU", "NV", "R", "RF", "SB", "TC", "BE", "QE", "PI", "E"],
    3: ["T1", "T0", "HE", "SHART", "SHAMS", "MX", "OT", "V0", "V1", "V2", "VU", "RF", "SB", "TC", "BE", "QE", "PI", "E"],
}
PRIORITY = {
    1: ["E", "BE", "QE", "PI", "SB", "RF", "TC", "NV", "R", "V1", "V0", "V2", "VU"],
    2: ["E", "BE", "QE", "PI", "SB", "RF", "TC", "NV", "R", "V1", "V0", "V2", "VU"],
    3: ["E", "BE", "QE", "PI", "SB", "RF", "TC", "T1", "T0", "HE", "SHART", "SHAMS", "MX", "OT", "V1", "V0", "V2", "VU"],
}

NORMAL_STOPS = ["end_turn"]
BUDGET_STOPS = ["max_tokens"]
INVISIBLE_CODEPOINTS = [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
                        0x2066, 0x2067, 0x2068, 0x2069, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E]
ELLIPSIS_FORMS = [".", "..", "...", "....", "…", "…."]
BUDGET_MARKERS = ["max_tokens or model output limit was reached", "maximum output token", "max output token", "token budget"]
QUOTA_MARKERS = ["insufficient_quota", "credit balance is too low", "billing", "payment required", "purchase credits",
                 "insufficient balance", "balance is insufficient", "insufficient_balance", "account_arrears", "please recharge",
                 "you have reached your specified workspace api usage limits", "you have reached your specified api usage limits",
                 "you have reached your api usage limits"]
CODEPOINT_LIMIT = 512


def unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def parse(raw):
    return json.loads(raw, object_pairs_hook=unique_keys, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def near_void(text):
    if not text:
        return False
    if all(ord(c) in INVISIBLE_CODEPOINTS or c.isspace() for c in text):
        return True
    return text.strip() in ELLIPSIS_FORMS


def mixed_script(text):
    return bool(regex.search(r"\p{Script=Arabic}", text)) and bool(regex.search(r"\p{Script=Hebrew}", text))


def mixed_script_words(text):
    words = regex.findall(r"[\p{L}\p{M}]+", text)
    return [w for w in dict.fromkeys(words) if mixed_script(w)]


def http_error_class(status, body):
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    error_type = error.get("type") if isinstance(error.get("type"), str) else ""
    lowered = message.lower()
    if status in (400, 413, 422) and any(m in lowered for m in BUDGET_MARKERS):
        return "BE", error_type, message
    if status in (400, 402, 429) and any(m in (error_type + " " + message).lower() for m in QUOTA_MARKERS):
        return "QE", error_type, message
    if status in (400, 404, 405, 413, 422):
        return "PI", error_type, message
    return "E", error_type, message


def classify(experiment, http_status, raw):
    r = {"experiment": experiment, "class": "E", "void": False, "model_completion": False, "http_status": http_status,
         "returned_model": None, "response_id": None, "stop_reason": None, "stop_sequence": None, "block_types": None,
         "text_container_present": None, "visible_bytes": None, "content_utf8_hex": None, "content_codepoints": None,
         "last_codepoint_category": None, "near_void": None, "input_tokens": None, "output_tokens": None, "usage": None,
         "provider_error_type": None, "provider_error_message": None}
    if experiment == 3:
        r.update(contains={k: None for k in EXACT_TARGETS}, mixed_script_words=None)
    if http_status is None:
        return r
    try:
        obj = parse(raw)
    except (ValueError, UnicodeError):
        return r
    if not isinstance(obj, dict):
        return r
    r["returned_model"] = obj.get("model")
    r["response_id"] = obj.get("id")
    usage = obj.get("usage")
    if isinstance(usage, dict):
        r.update(usage=usage, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
    if not 200 <= http_status < 300:
        r["class"], r["provider_error_type"], r["provider_error_message"] = http_error_class(http_status, obj)
        return r
    content = obj.get("content")
    stop = obj.get("stop_reason")
    if obj.get("type") != "message" or obj.get("role") != "assistant" or not isinstance(content, list):
        return r
    if not isinstance(stop, str) or not stop:
        return r
    types, texts = [], []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return r
        types.append(block["type"])
        if block["type"] == "text":
            if not isinstance(block.get("text"), str):
                return r
            texts.append(block["text"])
    text = "".join(texts)
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        return r
    r.update(model_completion=True, stop_reason=stop, stop_sequence=obj.get("stop_sequence"), block_types=types,
             text_container_present="text" in types, visible_bytes=len(encoded), content_utf8_hex=encoded.hex(),
             near_void=near_void(text))
    if len(text) <= CODEPOINT_LIMIT:
        r["content_codepoints"] = [f"U+{ord(c):04X}" for c in text]
    if text:
        r["last_codepoint_category"] = unicodedata.category(text[-1])
    if experiment == 3:
        r.update(contains={k: v in text for k, v in EXACT_TARGETS.items()}, mixed_script_words=mixed_script_words(text))
    if stop == "refusal":
        r["class"] = "RF"
    elif any(t in ("tool_use", "server_tool_use") for t in types) or stop == "tool_use":
        r["class"] = "TC"
    elif text:
        if experiment == 3:
            exact = {v: k for k, v in EXACT_TARGETS.items()}
            r["class"] = exact.get(text) or ("MX" if mixed_script(text) else "OT")
        else:
            r["class"] = "NV" if r["near_void"] else "R"
    elif stop in BUDGET_STOPS:
        r["class"] = "V1"
    elif stop in NORMAL_STOPS:
        r["class"] = "V0" if r["text_container_present"] else "V2"
    else:
        r["class"] = "VU"
    r["void"] = r["class"] in VOID_CLASSES
    return r
