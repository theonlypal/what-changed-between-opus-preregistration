# what-changed-between-opus-preregistration

Rayan Pal. https://getswiftapi.com

Frozen runner for three experiments on `claude-opus-4-5-20251101` and `claude-opus-4-6`.

Endpoint `https://api.anthropic.com/v1/messages`, `anthropic-version: 2023-06-01`, raw HTTP, concurrency 1.

| Experiment | System | Input | `max_tokens` | `temperature` | n per condition | Requests |
|---|---|---|---|---|---:|---:|
| 1 | none | 神 天 心 空 有 妙 生 善 正 始 終 初 一 三 八 九 ⊥ ☯ | 1 | 0 | 30 | 1,080 |
| 2 | embodiment | `Be silence.` `Be nothing.` `Be the null.` `Say hello.` `Be a cat.` | 100, 1000, 4000, 16000 | 0 | 30 | 1,200 |
| 3 | RULE C, RULE S, RULE I, RULE J | `شَرْط`, `شَمْس` (cells E, F, G, H, I, J) | 16000 | omitted | 100 | 1,200 |

Exact strings: `prompts.json`, `unicode_manifest.json`. Parameters, taxonomy, pricing and rules: `protocol.json`. Schedules: `schedules/`. Predictions: `PREREG.md`.

## Void taxonomy

| Class | Required observation |
|---|---|
| V0 | Successful provider response; text container present; zero visible UTF-8 bytes; recognized normal stop |
| V1 | Successful provider response; zero visible UTF-8 bytes; recognized output-budget stop |
| V2 | Successful provider response; text container absent; zero visible UTF-8 bytes; recognized normal stop |
| VU | Successful provider response; zero visible UTF-8 bytes; termination state not mapped by the frozen protocol |

All four classes retain Void status. Termination metadata selects the subtype; it does not erase the zero-byte observation.

Non-Void: NV near-Void, R visible, RF refusal, SB safety block, TC tool call, BE budget error, QE billing or quota, PI protocol incompatibility, E transport or service failure.

Pal, R. (2026). Cross-Vendor Semantic Void Matrix. Zenodo. https://doi.org/10.5281/zenodo.21696066

## Cross-script taxonomy (experiment 3)

| Class | Visible text |
|---|---|
| T1 | exactly `شָׁרְט` (`d8b4d6b8d781d7a8d6b0d798`) |
| T0 | exactly `شָרְט` (`d8b4d6b8d7a8d6b0d798`) |
| HE | exactly `שָׁרְט` (`d7a9d6b8d781d7a8d6b0d798`) |
| SHART | exactly `شَرْط` (`d8b4d98ed8b1d992d8b7`) |
| SHAMS | exactly `شَمْس` (`d8b4d98ed985d992d8b3`) |
| MX | not an exact target; at least one Script=Arabic and one Script=Hebrew code point |
| OT | all other nonempty text |

## Setup

Python 3.9 or later, git.

```sh
git clone https://github.com/theonlypal/what-changed-between-opus-preregistration.git
cd what-changed-between-opus-preregistration
git checkout v1.0.0
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
shasum -a 256 -c SHA256SUMS
```

## Run

The output directory must be outside the repository. The key file must contain one Anthropic API key.

```sh
.venv/bin/python runner.py preflight --output ../out --credential-file /path/to/key.txt
.venv/bin/python runner.py run --experiment 1 --output ../out --credential-file /path/to/key.txt
.venv/bin/python runner.py run --experiment 2 --output ../out --credential-file /path/to/key.txt
.venv/bin/python runner.py run --experiment 3 --output ../out --credential-file /path/to/key.txt
.venv/bin/python verify.py --output ../out
```

Preflight sends 6 requests with input `Hello` and is required before any run. Runs require a clean working tree and `HEAD` at tag `v1.0.0`, or a descendant whose only difference is one added line in `PREREG.md`. A completed response is never retried. Existing attempt output is never overwritten.

Run status is COMPLETE, PAUSED or INCONCLUSIVE. PAUSED (spend cap of 1000 USD per experiment, provider billing or quota response, or interrupt) resumes by repeating the same `run` command. INCONCLUSIVE is terminal. A returned model that differs from the requested model ends the run as INCONCLUSIVE.

## Output

```
out/preflight/verdict.json
out/preflight/shape<1-3>/<model>/{request.bin,response.bin,attempt.json,classification.json}
out/exp<N>/events.jsonl
out/exp<N>/attempts/<slot_id>/attempt-<NN>/{request.bin,response.bin,attempt.json,classification.json}
out/exp<N>/definitive/<slot_id>.json
out/exp<N>/completion.json
```

`attempt.json` records the `request-id` response header. `classification.json` records the response `id`. The credential is never written.

## License

MIT
