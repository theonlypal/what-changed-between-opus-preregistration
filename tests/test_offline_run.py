import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
import verify
from frozen import ENDPOINT, ANTHROPIC_VERSION, load_schedule
from test_classifier import TIER_SPEND_CAP, WORKSPACE_SPEND_LIMIT


def responder(fail_first=0):
    state = {"calls": 0}

    def handle(request):
        state["calls"] += 1
        assert str(request.url) == ENDPOINT
        assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
        assert request.headers["x-api-key"] == "sk-ant-synthetic"
        if state["calls"] <= fail_first:
            return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        body = json.loads(request.content)
        content = [] if "silence" in body["messages"][0]["content"] else [{"type": "text", "text": "x"}]
        return httpx.Response(200, headers={"request-id": "req_synthetic"}, json={
            "id": f"msg_synthetic{state['calls']}", "type": "message", "role": "assistant", "model": body["model"],
            "content": content, "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    return handle


class OfflineRun(unittest.TestCase):
    def setUp(self):
        runner.BACKOFF_SECONDS = [0, 0, 0]

    def test_full_experiment_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(responder()))
            self.assertTrue(runner.execute(2, out, client, frozen=False))
            status, seen, total, counts, spent = verify.verify_experiment(out, 2)
            self.assertEqual((status, seen, total), ("COMPLETE", 1200, 1200))
            for key, c in counts.items():
                self.assertEqual(dict(c), {"V2": 30} if "silence" in key[1] else {"R": 30})
            self.assertFalse(any(b"sk-ant-synthetic" in p.read_bytes() for p in out.rglob("*") if p.is_file()))
            with self.assertRaises(AssertionError):
                runner.execute(2, out, client, frozen=False)

    def test_retry_then_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(responder(fail_first=2)))
            self.assertTrue(runner.execute(1, out, client, frozen=False))
            first = load_schedule(1)[0]["slot_id"]
            self.assertEqual(len(list((out / "exp1" / "attempts" / first).iterdir())), 3)
            self.assertEqual(verify.verify_experiment(out, 1)[:3], ("COMPLETE", 1080, 1080))

    def test_model_mismatch_is_inconclusive(self):
        def handle(request):
            return httpx.Response(200, json={"id": "msg_x", "type": "message", "role": "assistant", "model": "other",
                                             "content": [], "stop_reason": "end_turn", "usage": {}})
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle))
            self.assertFalse(runner.execute(3, out, client, frozen=False))
            completion = json.loads((out / "exp3" / "completion.json").read_bytes())
            self.assertEqual((completion["status"], completion["completed_slots"]), ("INCONCLUSIVE", 1))
            self.assertEqual(verify.verify_experiment(out, 3)[0], "INCONCLUSIVE")
            with self.assertRaises(AssertionError):
                runner.execute(3, out, client, frozen=False)

    def test_tampering_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(responder()))
            runner.execute(1, out, client, frozen=False)
            slot = load_schedule(1)[5]["slot_id"]
            target = out / "exp1" / "attempts" / slot / "attempt-01" / "response.bin"
            target.chmod(0o644)
            target.write_bytes(target.read_bytes().replace(b'"x"', b'"y"'))
            with self.assertRaises(AssertionError):
                verify.verify_experiment(out, 1)


def events(out, experiment):
    return [json.loads(line) for line in (out / f"exp{experiment}" / "events.jsonl").read_bytes().splitlines()]


def completion(out, experiment):
    return json.loads((out / f"exp{experiment}" / "completion.json").read_bytes())


USAGE_COST_USD = 30 / 1_000_000
EXP1_RESERVATION = 10025


def error(status, error_type, message):
    return httpx.Response(status, json={"type": "error", "error": {"type": error_type, "message": message}})


class Resumable(unittest.TestCase):
    def setUp(self):
        runner.BACKOFF_SECONDS = [0, 0, 0]

    def assert_complete_once(self, out, experiment, slots, kept=0):
        status, seen, total, counts, spent = verify.verify_experiment(out, experiment)
        self.assertEqual((status, seen, total), ("COMPLETE", slots, slots))
        self.assertEqual(len(list((out / f"exp{experiment}" / "definitive").iterdir())), slots)
        self.assertEqual([e["event"] for e in events(out, experiment)].count("slot_completed"), slots)
        self.assertEqual(spent, (slots * 30 + kept) / 1_000_000)

    def test_spend_cap_pauses_then_resume_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(responder()))
            with mock.patch.object(runner, "MAX_SPEND_USD", 0.0101):
                self.assertFalse(runner.execute(1, out, client, frozen=False))
            c = completion(out, 1)
            self.assertEqual((c["status"], c["reason"], c["completed_slots"]), ("PAUSED", "spend cap", 3))
            self.assertEqual(c["spent_usd"], 3 * USAGE_COST_USD)
            self.assertEqual(verify.verify_experiment(out, 1)[:3], ("PAUSED", 3, 1080))
            self.assertTrue(runner.execute(1, out, client, frozen=False))
            self.assert_complete_once(out, 1, 1080)
            self.assertEqual([e["event"] for e in events(out, 1)].count("run_resumed"), 1)
            with self.assertRaises(AssertionError):
                runner.execute(1, out, client, frozen=False)

    def test_retry_after_honored(self):
        base = responder()
        calls = {"n": 0}

        def handle(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "7"},
                                      json={"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}})
            if calls["n"] == 2:
                return httpx.Response(529, headers={"retry-after": "900"},
                                      json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
            return base(request)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runner.time, "sleep") as sleep:
            out = Path(tmp)
            self.assertTrue(runner.execute(1, out, runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle)), frozen=False))
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [7, 120])
            waits = [e["data"] for e in events(out, 1) if e["event"] == "retry_wait"]
            self.assertEqual([(w["seconds"], w["source"]) for w in waits], [(7, "retry-after"), (120, "retry-after")])
            self.assert_complete_once(out, 1, 1080)

    def test_remote_protocol_error_retried(self):
        base = responder()
        calls = {"n": 0}

        def handle(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return base(request)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertTrue(runner.execute(1, out, runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle)), frozen=False))
            first = load_schedule(1)[0]["slot_id"]
            meta = json.loads((out / "exp1" / "attempts" / first / "attempt-01" / "attempt.json").read_bytes())
            self.assertEqual(meta["transport_error"], {"type": "RemoteProtocolError", "retry_eligible": True})
            persisted = [e["data"] for e in events(out, 1) if e["event"] == "attempt_persisted" and e["data"]["slot_id"] == first]
            self.assertEqual([(d["charge"], d["spent_usd"]) for d in persisted],
                             [("reservation", EXP1_RESERVATION / 1_000_000), ("usage", (EXP1_RESERVATION + 30) / 1_000_000)])
            self.assert_complete_once(out, 1, 1080, kept=EXP1_RESERVATION)

    def test_two_timeouts_then_success_charges_all_three(self):
        base = responder()
        calls = {"n": 0}

        def handle(request):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise httpx.ReadTimeout("timed out")
            return base(request)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertTrue(runner.execute(1, out, runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle)), frozen=False))
            first = load_schedule(1)[0]["slot_id"]
            persisted = [e["data"] for e in events(out, 1) if e["event"] == "attempt_persisted" and e["data"]["slot_id"] == first]
            self.assertEqual([d["charge"] for d in persisted], ["reservation", "reservation", "usage"])
            self.assertEqual(persisted[-1]["spent_usd"], (2 * EXP1_RESERVATION + 30) / 1_000_000)
            self.assert_complete_once(out, 1, 1080, kept=2 * EXP1_RESERVATION)

    def test_quota_pauses_and_never_exhausts_attempts(self):
        base = responder()
        state = {"calls": 0, "quota": False}

        def handle(request):
            state["calls"] += 1
            if state["calls"] == 3:
                state["quota"] = True
            if state["quota"]:
                return error(429, "rate_limit_error", TIER_SPEND_CAP)
            return base(request)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle))
            for _ in range(runner.MAX_ATTEMPTS + 1):
                self.assertFalse(runner.execute(1, out, client, frozen=False))
                c = completion(out, 1)
                self.assertEqual((c["status"], c["reason"], c["completed_slots"]), ("PAUSED", "provider quota", 2))
                self.assertEqual(verify.verify_experiment(out, 1)[0], "PAUSED")
            state["quota"] = False
            self.assertTrue(runner.execute(1, out, client, frozen=False))
            third = load_schedule(1)[2]["slot_id"]
            self.assertEqual(len(list((out / "exp1" / "attempts" / third).iterdir())), runner.MAX_ATTEMPTS + 2)
            self.assert_complete_once(out, 1, 1080)

    def test_workspace_spend_limit_pauses(self):
        base = responder()
        calls = {"n": 0}

        def handle(request):
            calls["n"] += 1
            if calls["n"] == 2:
                return error(400, "invalid_request_error", WORKSPACE_SPEND_LIMIT)
            return base(request)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle))
            self.assertFalse(runner.execute(1, out, client, frozen=False))
            second = load_schedule(1)[1]["slot_id"]
            result = json.loads((out / "exp1" / "attempts" / second / "attempt-01" / "classification.json").read_bytes())
            self.assertEqual((result["http_status"], result["class"]), (400, "QE"))
            c = completion(out, 1)
            self.assertEqual((c["status"], c["reason"]), ("PAUSED", "provider quota"))
            self.assertTrue(runner.execute(1, out, client, frozen=False))
            self.assert_complete_once(out, 1, 1080)

    def test_keyboard_interrupt_pauses_then_resume_completes(self):
        base = responder()
        calls = {"n": 0}

        def handle(request):
            calls["n"] += 1
            if calls["n"] == 5:
                raise KeyboardInterrupt
            return base(request)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(handle))
            self.assertFalse(runner.execute(1, out, client, frozen=False))
            c = completion(out, 1)
            self.assertEqual((c["status"], c["reason"], c["completed_slots"]), ("PAUSED", "interrupted", 4))
            self.assertEqual(verify.verify_experiment(out, 1)[0], "PAUSED")
            self.assertTrue(runner.execute(1, out, client, frozen=False))
            fifth = load_schedule(1)[4]["slot_id"]
            self.assertEqual(sorted(p.name for p in (out / "exp1" / "attempts" / fifth).iterdir()), ["attempt-01", "attempt-02"])
            self.assert_complete_once(out, 1, 1080, kept=EXP1_RESERVATION)

    def test_interrupt_before_persistence_is_abandoned(self):
        real = runner.attempt
        calls = {"n": 0}

        def interrupting(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real(*args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            client = runner.make_client("sk-ant-synthetic", httpx.MockTransport(responder()))
            with mock.patch.object(runner, "attempt", interrupting):
                self.assertFalse(runner.execute(1, out, client, frozen=False))
            self.assertEqual(completion(out, 1)["status"], "PAUSED")
            self.assertTrue(runner.execute(1, out, client, frozen=False))
            self.assertEqual([e["event"] for e in events(out, 1)].count("attempt_abandoned"), 1)
            self.assert_complete_once(out, 1, 1080, kept=EXP1_RESERVATION)


if __name__ == "__main__":
    unittest.main()
