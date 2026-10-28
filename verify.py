"""Offline verification of a run output and event-log replay. No network operations."""
import argparse
import collections
import json
from pathlib import Path
import sys
from classifier import classify
from frozen import (EXPERIMENTS, canonical, digest, request_body, request_bytes, load_schedule, reservation_micro_usd,
                    usage_micro_usd, usd)

TERMINAL_EVENTS = {"run_completed": "COMPLETE", "run_paused": "PAUSED", "run_stopped": "INCONCLUSIVE"}


def condition_key(experiment, slot):
    if experiment == 1:
        return (slot["model"], slot["input"])
    if experiment == 2:
        return (slot["model"], slot["input"], slot["max_tokens"])
    return (slot["model"], slot["cell"])


def attempt_path(folder, slot_id, n):
    return folder / "attempts" / slot_id / f"attempt-{n:02d}"


def reservation(experiment, slot):
    return reservation_micro_usd(slot["model"], request_body(experiment, slot)["max_tokens"])


def interrupted(meta):
    return (meta["transport_error"] or {}).get("type") == "KeyboardInterrupt"


def counts_as_try(meta, result):
    return not interrupted(meta) and result["class"] != "QE"


def charge(meta, result, reserved, model):
    """Reported usage replaces the reservation; a 2xx without usage or a transport failure keeps it; HTTP errors cost 0."""
    status = meta["http_status"]
    ok = isinstance(status, int) and 200 <= status <= 299
    cost = usage_micro_usd(model, result["usage"]) if ok and result["usage"] is not None else None
    if cost is not None:
        return "usage", cost
    if ok or meta["transport_error"]:
        return "reservation", reserved
    return "none", 0


class Ledger:
    """Spend and progress state of one experiment folder, rebuilt identically by the runner and by replay."""

    def __init__(self):
        self.committed = 0
        self.attempts = collections.Counter()
        self.tries = collections.Counter()
        self.definitive = {}
        self.open = None
        self.awaiting = None
        self.status = None
        self.run = None
        self.seq = 0
        self.head = "0" * 64

    def spent(self):
        return self.committed

    def start(self, slot_id, n, reserved):
        assert self.open is None and slot_id not in self.definitive, "Attempt out of order"
        assert n == self.attempts[slot_id] + 1, "Attempt number out of order"
        self.attempts[slot_id] = n
        self.open = (slot_id, n, reserved)

    def settle(self, slot_id, n, kind, amount, counted):
        assert self.open and self.open[:2] == (slot_id, n), "Attempt settlement out of order"
        self.committed += amount
        if counted:
            self.tries[slot_id] += 1
        self.open = None
        return self.spent()

    def complete(self, record):
        assert self.awaiting == (record["slot_id"], record["attempt"]), "Definitive record does not follow its completion"
        assert record["slot_id"] not in self.definitive, "Duplicate definitive record"
        self.definitive[record["slot_id"]] = record
        self.awaiting = None


def check_chain(path):
    previous, seq, rows = "0" * 64, 0, []
    for line in path.read_bytes().splitlines():
        row = json.loads(line)
        seq += 1
        assert row["seq"] == seq, "Event sequence gap"
        assert row["prev_hash"] == previous, "Event chain break"
        expected = row.pop("event_hash")
        assert digest(canonical(row)) == expected, "Event hash mismatch"
        previous = expected
        rows.append(row)
    return rows, previous


def check_attempt(experiment, folder, body):
    meta = json.loads((folder / "attempt.json").read_bytes())
    request = (folder / "request.bin").read_bytes()
    response = (folder / "response.bin").read_bytes()
    assert request == body, "Request bytes differ from frozen request"
    assert digest(request) == meta["request_sha256"], "Request hash mismatch"
    assert digest(response) == meta["response_sha256"], "Response hash mismatch"
    assert meta["request_headers"].get("x-api-key") in (None, "REDACTED"), "Credential persisted"
    result = classify(experiment, meta["http_status"], response)
    assert result == json.loads((folder / "classification.json").read_bytes()), "Classification does not reproduce"
    return meta, result


def replay(folder, experiment, schedule):
    rows, head = check_chain(folder / "events.jsonl")
    slots = {s["slot_id"]: s for s in schedule}
    ledger = Ledger()
    for i, row in enumerate(rows):
        event, data = row["event"], row["data"]
        if i == 0:
            assert event == "run_started" and data["experiment"] == experiment and data["slots"] == len(schedule), "Bad run start"
            ledger.run = data
        elif event == "run_resumed":
            assert ledger.status not in ("COMPLETE", "INCONCLUSIVE"), "Resumed a terminal run"
            assert data["spent_usd"] == usd(ledger.spent()), "Spend does not replay"
            ledger.status = None
        elif event == "attempt_started":
            slot = slots[data["slot_id"]]
            reserved = reservation(experiment, slot)
            assert data["request_sha256"] == slot["request_sha256"] and data["reserved_usd"] == usd(reserved), "Bad reservation"
            ledger.start(data["slot_id"], data["attempt"], reserved)
        elif event == "attempt_persisted":
            slot = slots[data["slot_id"]]
            meta, result = check_attempt(experiment, attempt_path(folder, data["slot_id"], data["attempt"]),
                                         request_bytes(experiment, slot))
            assert ledger.open, "Attempt persisted without start"
            kind, amount = charge(meta, result, ledger.open[2], slot["model"])
            observed = (data["response_sha256"], data["class"], data["model_completion"], data["charge"], data["interrupted"])
            assert observed == (meta["response_sha256"], result["class"], result["model_completion"], kind, interrupted(meta)), \
                "Persisted event differs from attempt files"
            assert data["spent_usd"] == usd(ledger.settle(data["slot_id"], data["attempt"], kind, amount, counts_as_try(meta, result))), \
                "Spend does not replay"
            if result["model_completion"]:
                ledger.awaiting = (data["slot_id"], data["attempt"])
        elif event == "attempt_abandoned":
            assert ledger.open, "Abandoned attempt without start"
            spent = ledger.settle(data["slot_id"], data["attempt"], "reservation", ledger.open[2], False)
            assert data["spent_usd"] == usd(spent), "Spend does not replay"
        elif event == "retry_wait":
            assert data["slot_id"] in slots and ledger.open is None, "Bad retry wait"
        elif event == "slot_completed":
            ledger.complete(data)
        elif event in TERMINAL_EVENTS:
            assert ledger.status is None, "Second terminal event without resume"
            assert data["spent_usd"] == usd(ledger.spent()), "Spend does not replay"
            ledger.status = TERMINAL_EVENTS[event]
        else:
            raise AssertionError("Unknown event " + event)
    ledger.seq, ledger.head = len(rows), head
    return ledger


def verify_experiment(output, experiment):
    folder = output / f"exp{experiment}"
    schedule = load_schedule(experiment)
    slots = {s["slot_id"]: s for s in schedule}
    ledger = replay(folder, experiment, schedule)
    completion = json.loads((folder / "completion.json").read_bytes())
    assert completion["event_chain_head"] == ledger.head, "Completion does not match event chain head"
    assert completion["status"] == ledger.status, "Completion status differs from event log"
    assert completion["spent_usd"] == usd(ledger.spent()), "Completion spend differs from event log"
    assert completion["completed_slots"] == len(ledger.definitive), "Completed slot count mismatch"
    for slot_dir in (folder / "attempts").iterdir() if (folder / "attempts").exists() else []:
        assert slot_dir.name in slots, "Attempt folder for unscheduled slot " + slot_dir.name
        allowed = {f"attempt-{n:02d}" for n in range(1, ledger.attempts[slot_dir.name] + 1)}
        assert {p.name for p in slot_dir.iterdir()} <= allowed, "Attempt folder absent from event log"
    files = {p.name[:-len(".json")] for p in (folder / "definitive").iterdir()} if (folder / "definitive").exists() else set()
    assert files == set(ledger.definitive), "Definitive records differ from event log"
    counts = collections.defaultdict(collections.Counter)
    for slot_id, record in ledger.definitive.items():
        slot = slots[slot_id]
        assert json.loads((folder / "definitive" / (slot_id + ".json")).read_bytes()) == record, "Definitive record differs"
        assert record["attempt"] == ledger.attempts[slot_id], "Definitive record is not the last attempt"
        path = attempt_path(folder, slot_id, record["attempt"])
        result = json.loads((path / "classification.json").read_bytes())
        meta = json.loads((path / "attempt.json").read_bytes())
        assert record["class"] == result["class"] and record["response_sha256"] == meta["response_sha256"]
        assert record["requested_model"] == slot["model"]
        counts[condition_key(experiment, slot)][record["class"]] += 1
    if ledger.status == "COMPLETE":
        assert set(ledger.definitive) == set(slots), "COMPLETE without exactly one definitive record per scheduled slot"
    return ledger.status, len(ledger.definitive), len(schedule), counts, usd(ledger.spent())


def verify_preflight(output):
    verdict = json.loads((output / "preflight" / "verdict.json").read_bytes())
    for check in verdict["checks"]:
        folder = output / "preflight" / f"shape{check['shape']}" / check["model"]
        meta = json.loads((folder / "attempt.json").read_bytes())
        assert digest((folder / "request.bin").read_bytes()) == meta["request_sha256"] == check["request_sha256"]
        assert digest((folder / "response.bin").read_bytes()) == meta["response_sha256"]
    return verdict


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--experiment", type=int, choices=EXPERIMENTS)
    args = p.parse_args()
    output = args.output.resolve()
    failures = 0
    if (output / "preflight" / "verdict.json").exists():
        verdict = verify_preflight(output)
        print(f"preflight {verdict['status']} frozen={verdict['frozen']} commit={verdict['commit']}")
    for experiment in [args.experiment] if args.experiment else EXPERIMENTS:
        if not (output / f"exp{experiment}").exists():
            continue
        try:
            status, seen, total, counts, spent = verify_experiment(output, experiment)
        except (AssertionError, KeyError, OSError, ValueError) as exc:
            failures += 1
            print(f"exp{experiment} VERIFY FAIL: {exc}")
            continue
        print(f"exp{experiment} VERIFY PASS definitive={seen}/{total} spent_usd={spent:.6f}")
        print(f"exp{experiment} {status}")
        for key in sorted(counts, key=str):
            print("  " + " | ".join(map(str, key)) + " | " + " ".join(f"{c}={n}" for c, n in sorted(counts[key].items())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
