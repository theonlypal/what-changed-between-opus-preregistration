"""Serial frozen runner. Raw persistence precedes classification and progress."""
import argparse
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
import httpx
import regex
from classifier import classify
from frozen import (ROOT, FROZEN_TAG, ENDPOINT, ANTHROPIC_VERSION, MODELS, PINNED, EXPERIMENTS, MAX_SPEND_USD, canonical,
                    digest, request_bytes, preflight_body, load_schedule, usd)
from verify import Ledger, attempt_path, charge, counts_as_try, interrupted, replay, reservation

RESPONSE_HEADERS = {"request-id", "date", "content-type", "retry-after", "x-should-retry"}
BACKOFF_SECONDS = [5, 15, 30]
MAX_ATTEMPTS = 4
RETRY_AFTER_MAX_SECONDS = 120
TIMEOUT = httpx.Timeout(connect=30, read=600, write=30, pool=30)


def utc():
    return datetime.now(timezone.utc).isoformat()


def save_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def save_json(path, data):
    save_bytes(path, canonical(data) + b"\n")


def replace_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(canonical(data) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def head():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def environment():
    return {"python": platform.python_version(), "httpx": httpx.__version__, "regex": regex.__version__}


def check_environment():
    env = environment()
    for name, version in PINNED.items():
        assert env[name] == version, f"{name} {env[name]} differs from pinned {version}"
    return env


def check_freeze():
    commit = git("rev-parse", "HEAD")
    tagged = git("rev-parse", FROZEN_TAG + "^{commit}")
    if commit != tagged:
        assert subprocess.run(["git", "merge-base", "--is-ancestor", tagged, commit], cwd=ROOT).returncode == 0, \
            "HEAD does not descend from the frozen tag " + FROZEN_TAG
        assert git("diff", "--numstat", tagged, commit).split() == ["1", "0", "PREREG.md"], \
            "HEAD differs from the frozen tag beyond one added line in PREREG.md"
    assert not git("status", "--porcelain", "--untracked-files=all"), "Working tree must be clean"
    for experiment in EXPERIMENTS:
        load_schedule(experiment)
    return commit


def credential(path):
    keys = re.findall(r"sk-ant-[A-Za-z0-9_\-]+", Path(path).read_text())
    assert len(keys) == 1, "Credential file must contain exactly one Anthropic API key"
    return keys[0]


def make_client(key, transport=None):
    return httpx.Client(headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
                        timeout=TIMEOUT, trust_env=False, follow_redirects=False,
                        transport=transport or httpx.HTTPTransport(retries=0))


class Events:
    def __init__(self, path, ledger):
        if ledger.seq == 0:
            assert not path.exists(), "Event log already exists"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.seq = ledger.seq
        self.previous = ledger.head

    def add(self, event, data):
        self.seq += 1
        row = {"seq": self.seq, "prev_hash": self.previous, "event": event, "data": data, "timestamp_utc": utc()}
        row["event_hash"] = digest(canonical(row))
        with self.path.open("ab") as f:
            f.write(canonical(row) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        self.previous = row["event_hash"]


def attempt(client, folder, experiment, body, label):
    folder.mkdir(parents=True, exist_ok=False)
    request = client.build_request("POST", ENDPOINT, content=body, headers={"content-type": "application/json"})
    assert request.content == body
    save_bytes(folder / "request.bin", request.content)
    sent_headers = {k: ("REDACTED" if k.lower() == "x-api-key" else v) for k, v in request.headers.items()}
    meta = {"slot_id": label, "timestamp_utc": utc(), "url": str(request.url), "request_headers": sent_headers,
            "request_sha256": digest(request.content), "http_status": None, "response_headers": {},
            "response_sha256": None, "transport_error": None}
    started = time.monotonic()
    raw = b""
    try:
        response = client.send(request)
        raw = response.content
        meta["http_status"] = response.status_code
        meta["response_headers"] = {k: v for k, v in response.headers.items() if k.lower() in RESPONSE_HEADERS}
    except httpx.TransportError as exc:
        meta["transport_error"] = {"type": type(exc).__name__, "retry_eligible": True}
    except httpx.HTTPError as exc:
        meta["transport_error"] = {"type": type(exc).__name__, "retry_eligible": False}
    except KeyboardInterrupt:
        meta["transport_error"] = {"type": "KeyboardInterrupt", "retry_eligible": True}
    meta.update(duration_seconds=time.monotonic() - started, completed_at_utc=utc(), response_sha256=digest(raw))
    save_bytes(folder / "response.bin", raw)
    save_json(folder / "attempt.json", meta)
    result = classify(experiment, meta["http_status"], raw)
    save_json(folder / "classification.json", result)
    return meta, result


def retry_eligible(meta, result):
    if meta["transport_error"]:
        return meta["transport_error"]["retry_eligible"]
    status = meta["http_status"]
    if result["class"] == "QE":
        return False
    return status == 429 or (isinstance(status, int) and 500 <= status <= 599)


def retry_wait(meta, tries):
    value = meta["response_headers"].get("retry-after")
    status = meta["http_status"]
    if value is not None and (status == 429 or (isinstance(status, int) and 500 <= status <= 599)):
        try:
            return max(0, min(math.ceil(float(value)), RETRY_AFTER_MAX_SECONDS)), "retry-after"
        except (ValueError, OverflowError):
            pass
    return BACKOFF_SECONDS[tries - 1], "backoff"


def preflight(output, client, frozen=True):
    folder = output / "preflight"
    assert not folder.exists(), "Preflight already exists"
    commit = check_freeze() if frozen else head()
    env = check_environment()
    checks = []
    for shape in EXPERIMENTS:
        for model in MODELS:
            body = canonical(preflight_body(shape, model))
            meta, result = attempt(client, folder / f"shape{shape}" / model, shape, body, f"preflight-shape{shape}-{model}")
            if interrupted(meta):
                raise KeyboardInterrupt
            ok = (meta["http_status"] == 200 and result["model_completion"] and result["returned_model"] == model
                  and str(result["response_id"]).startswith("msg_") and "request-id" in meta["response_headers"])
            checks.append({"shape": shape, "model": model, "pass": ok, "http_status": meta["http_status"],
                           "returned_model": result["returned_model"], "response_id": result["response_id"],
                           "request_id": meta["response_headers"].get("request-id"), "stop_reason": result["stop_reason"],
                           "class": result["class"], "request_sha256": meta["request_sha256"]})
    status = "PASS" if all(c["pass"] for c in checks) else "FAIL"
    save_json(folder / "verdict.json", {"status": status, "frozen": frozen, "commit": commit, "tag": FROZEN_TAG,
                                         "environment": env, "checks": checks, "timestamp_utc": utc()})
    for c in checks:
        print(f"shape{c['shape']} {c['model']}: {'PASS' if c['pass'] else 'FAIL'} http={c['http_status']} "
              f"stop={c['stop_reason']} id={c['response_id']} request-id={c['request_id']}", flush=True)
    print("PREFLIGHT " + status, flush=True)
    return status == "PASS"


class Pause(Exception):
    pass


def finish_slot(folder, events, ledger, slot, n, experiment):
    path = attempt_path(folder, slot["slot_id"], n)
    meta = json.loads((path / "attempt.json").read_bytes())
    result = json.loads((path / "classification.json").read_bytes())
    record = {"slot_id": slot["slot_id"], "attempt": n, "class": result["class"], "void": result["void"],
              "requested_model": slot["model"], "returned_model": result["returned_model"],
              "response_id": result["response_id"], "request_id": meta["response_headers"].get("request-id"),
              "request_sha256": meta["request_sha256"], "response_sha256": meta["response_sha256"]}
    target = folder / "definitive" / (slot["slot_id"] + ".json")
    if target.exists():
        assert json.loads(target.read_bytes()) == record, "Existing definitive record differs"
    else:
        save_json(target, record)
    ledger.complete(record)
    events.add("slot_completed", record)
    print(f"exp{experiment} {len(ledger.definitive)} {slot['slot_id']} {slot['model']} {result['class']}", flush=True)
    if result["returned_model"] != slot["model"]:
        raise RuntimeError("Returned model differs from requested model")


def execute(experiment, output, client, frozen=True):
    commit = check_freeze() if frozen else head()
    env = check_environment()
    schedule = load_schedule(experiment)
    slots = {s["slot_id"]: s for s in schedule}
    folder = output / f"exp{experiment}"
    if folder.exists():
        ledger = replay(folder, experiment, schedule)
        assert ledger.status not in ("COMPLETE", "INCONCLUSIVE"), f"Run is {ledger.status}; refusing rerun"
        assert (ledger.run["commit"], ledger.run["frozen"], ledger.run["environment"]) == (commit, frozen, env), \
            "Resume requires the commit, freeze mode and environment of the original run"
        events = Events(folder / "events.jsonl", ledger)
        events.add("run_resumed", {"commit": commit, "completed": len(ledger.definitive), "spent_usd": usd(ledger.spent())})
    else:
        ledger = Ledger()
        events = Events(folder / "events.jsonl", ledger)
        events.add("run_started", {"experiment": experiment, "commit": commit, "tag": FROZEN_TAG, "frozen": frozen,
                                   "environment": env, "slots": len(schedule), "max_spend_usd": MAX_SPEND_USD})
    status, reason = "COMPLETE", None
    try:
        if ledger.open:
            slot_id, n, _ = ledger.open
            spent = ledger.settle(slot_id, n, "reservation", ledger.open[2], False)
            events.add("attempt_abandoned", {"slot_id": slot_id, "attempt": n, "spent_usd": usd(spent)})
        if ledger.awaiting:
            finish_slot(folder, events, ledger, slots[ledger.awaiting[0]], ledger.awaiting[1], experiment)
        for record in ledger.definitive.values():
            if record["returned_model"] != record["requested_model"]:
                raise RuntimeError("Returned model differs from requested model")
        for slot in schedule:
            slot_id = slot["slot_id"]
            if slot_id in ledger.definitive:
                continue
            body = request_bytes(experiment, slot)
            assert digest(body) == slot["request_sha256"], "Request bytes differ from schedule"
            reserved = reservation(experiment, slot)
            while True:
                if ledger.tries[slot_id] >= MAX_ATTEMPTS:
                    raise RuntimeError("No definitive model completion for " + slot_id)
                if ledger.spent() + reserved > MAX_SPEND_USD * 1_000_000:
                    raise Pause("spend cap")
                n = ledger.attempts[slot_id] + 1
                ledger.start(slot_id, n, reserved)
                events.add("attempt_started", {"slot_id": slot_id, "attempt": n, "request_sha256": slot["request_sha256"],
                                               "reserved_usd": usd(reserved)})
                meta, result = attempt(client, attempt_path(folder, slot_id, n), experiment, body, slot_id)
                kind, amount = charge(meta, result, reserved, slot["model"])
                spent = ledger.settle(slot_id, n, kind, amount, counts_as_try(meta, result))
                events.add("attempt_persisted", {"slot_id": slot_id, "attempt": n, "http_status": meta["http_status"],
                                                 "response_sha256": meta["response_sha256"], "class": result["class"],
                                                 "model_completion": result["model_completion"], "charge": kind,
                                                 "cost_usd": usd(amount), "interrupted": interrupted(meta),
                                                 "spent_usd": usd(spent)})
                if interrupted(meta):
                    raise KeyboardInterrupt
                if result["class"] == "QE":
                    raise Pause("provider quota")
                if result["model_completion"]:
                    ledger.awaiting = (slot_id, n)
                    finish_slot(folder, events, ledger, slot, n, experiment)
                    break
                if not retry_eligible(meta, result) or ledger.tries[slot_id] >= MAX_ATTEMPTS:
                    raise RuntimeError("No definitive model completion for " + slot_id)
                seconds, source = retry_wait(meta, ledger.tries[slot_id])
                events.add("retry_wait", {"slot_id": slot_id, "attempt": n, "seconds": seconds, "source": source})
                print(f"exp{experiment} {slot_id} attempt {n} http={meta['http_status']} wait {seconds}s ({source})", flush=True)
                time.sleep(seconds)
        if frozen:
            check_freeze()
    except Pause as exc:
        status, reason = "PAUSED", str(exc)
    except KeyboardInterrupt:
        status, reason = "PAUSED", "interrupted"
    except Exception as exc:
        status = "INCONCLUSIVE"
        reason = str(exc) if isinstance(exc, (AssertionError, RuntimeError)) else type(exc).__name__
    summary = {"completed": len(ledger.definitive), "spent_usd": usd(ledger.spent())}
    if status == "COMPLETE":
        events.add("run_completed", summary)
    elif status == "PAUSED":
        events.add("run_paused", dict(summary, reason=reason))
    else:
        events.add("run_stopped", dict(summary, reason=reason, result="INCONCLUSIVE"))
    replace_json(folder / "completion.json", {"experiment": experiment, "status": status, "reason": reason,
                                              "completed_slots": len(ledger.definitive), "scheduled_slots": len(schedule),
                                              "spent_usd": usd(ledger.spent()), "max_spend_usd": MAX_SPEND_USD,
                                              "event_chain_head": events.previous, "completed_at_utc": utc()})
    print(f"exp{experiment} {status} {len(ledger.definitive)}/{len(schedule)} spent_usd={usd(ledger.spent()):.6f}"
          + (f" reason={reason}" if reason else ""), flush=True)
    return status == "COMPLETE"


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    run = sub.add_parser("run")
    run.add_argument("--experiment", type=int, choices=EXPERIMENTS, required=True)
    for s in (pre, run):
        s.add_argument("--output", type=Path, required=True)
        s.add_argument("--credential-file", required=True)
    pre.add_argument("--unfrozen", action="store_true")
    args = p.parse_args()
    output = args.output.resolve()
    assert output != ROOT and ROOT not in output.parents, "Output must be outside the runner repository"
    with make_client(credential(args.credential_file)) as client:
        if args.command == "preflight":
            return 0 if preflight(output, client, frozen=not args.unfrozen) else 2
        verdict = json.loads((output / "preflight" / "verdict.json").read_bytes())
        assert verdict["status"] == "PASS" and verdict["frozen"], "A frozen preflight PASS is required"
        assert verdict["commit"] == git("rev-parse", "HEAD"), "Preflight ran on a different commit"
        return 0 if execute(args.experiment, output, client) else 2


if __name__ == "__main__":
    sys.exit(main())
