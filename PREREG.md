# What Changed Between Claude Opus 4.5 and Claude Opus 4.6?

Preregistration

Author: Sharthok Rayan Pal

## Predictions

Author Sharthok Rayan Pal's predictions, stated before freeze, 2026-10-28T17:37:42Z.

P1. Opus 4.6, Experiment 2, 4,000-token ceiling, embodiment system prompt:
every one of the 90 null-input trials is a Void. I predict the subtype is
V2. Disconfirmed by any non-Void in those 90.

P2. Opus 4.5, Experiment 2, 4,000-token ceiling, same prompt: the
null-input Void rate is 0 of 90. Disconfirmed by any Void in those 90.

P3. Both models, Experiment 2, all four ceilings: "Say hello." and
"Be a cat." produce nonempty visible responses on every trial. 0 Voids
in 480.

P4. Experiment 1, one-token ceiling, no system prompt: Opus 4.5 produces
more V1 Voids than Opus 4.6 across all 540 trials per model. Direction
only. V0 and V2 counts reported per model without prediction.

P5. Experiment 3, cells E through J: neither model produces a Void in any
cell, including mismatched-prerequisite cells F and G. Both models produce
visible text in every cell. Response content is reported descriptively.
Disconfirmed by any Void in any Experiment 3 cell on either model.

Rayan Pal

## Scope

Scope. API-visible outcomes under the frozen classifier. No internal
mechanism is claimed. Each experiment is independently reportable; PAUSED
or INCONCLUSIVE in one does not affect the others.

Experiment 3 ceiling is 16,000 tokens, fixed prospectively. It does not replicate the output ceiling of the August 2026 GPT-5.4 cross-script study (budgets 1 to 1,024 tokens) or the September 2026 GPT-5.4 prerequisite-reassignment study (32,768 tokens). Comparisons to those studies preserve the E through H prompt strings and differ in provider, output ceiling, sampling, and replicate count.

Experiment 2 replicates the March 12, 2026 temperature-0 protocol, not the July provider-default protocol.

## Cost envelope

Prices: `claude-opus-4-5-20251101` and `claude-opus-4-6`, 5 USD per million input tokens, 25 USD per million output tokens (https://platform.claude.com/docs/en/about-claude/pricing, retrieved 2026-10-28). Envelope: sum over every scheduled request of max_tokens × output price + 2,000 × input price. Spend cap: 1000 USD per experiment.

| Experiment | Requests | claude-opus-4-5-20251101 (USD) | claude-opus-4-6 (USD) | Total (USD) |
|---|---:|---:|---:|---:|
| 1 | 1,080 | 5.4135 | 5.4135 | 10.827 |
| 2 | 1,200 | 85.125 | 85.125 | 170.25 |
| 3 | 1,200 | 246.00 | 246.00 | 492.00 |
| Total | 3,480 | 336.5385 | 336.5385 | 673.077 |

## Record

- Runner: https://github.com/theonlypal/what-changed-between-opus-preregistration
- Frozen tag: v1.0.0
