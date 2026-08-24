# PredictionPool

PredictionPool is a generalized prediction-market intelligent contract: the owner opens a pool with a
question, 2-4 options and one authoritative source URL, users take exactly one position from an internal
ledger before close time, and anyone can settle after close - an AI leader reads the source, an independent
AI validator re-reads it, and only an agreed `winner_index` pays out (or voids the pool).

## How it works

1. `create_pool(pool_id, question, options, source_url, closes_at_iso)` - owner-only; 2-4 option labels,
   one http(s) source.
2. `bet(pool_id, option_idx, amount_atto, now_iso)` - one bet per address per pool, only while open.
3. `resolve(pool_id, now_iso)` - allowed once `now_iso >= closes_at_iso`. The leader fetches `source_url`
   (`gl.nondet.web.get`, status classified: >= 500 -> `[TRANSIENT]`, other non-2xx -> `[EXTERNAL]`),
   truncates the body to 6000 chars, and prompts the model for
   `{"winner_index": <int or -1>, "reason": "..."}`. The validator reruns the identical fetch+prompt and
   requires **winner_index equality**; disagreement rotates leadership instead of settling. A `-1` verdict
   stores `"void"` (all positions conceptually refundable).

**Time simplification:** block-time access is intentionally avoided. Both `bet` and `resolve` take an
explicit `now_iso` argument that is compared lexicographically against `closes_at_iso`. Use uniform UTC
ISO-8601 strings (`2026-01-01T00:00:00Z`) - lexicographic order then equals chronological order.

## Contract interface

| Method | Kind | Description |
| --- | --- | --- |
| `create_pool(pool_id, question, options, source_url, closes_at_iso)` | write | Owner-only pool creation |
| `bet(pool_id, option_idx: i256, amount_atto: u256, now_iso)` | write | One internal-ledger bet per address while open |
| `resolve(pool_id, now_iso) -> bool` | write | After close: leader/validator AI settlement |
| `get_owner() -> str` | view | Deployer address |
| `get_pool(pool_id) -> dict` | view | Question, options, source, close time, result |
| `get_bet(pool_id, bettor: Address) -> dict` | view | `{option_idx, amount_atto}` |
| `pot_total(pool_id) -> u256` | view | Sum of all staked amounts |
| `winner(pool_id) -> str` | view | Winning option label, `"void"`, or `""` if unresolved |

## Quickstart

```bash
pip install -r requirements.txt
genvm-lint check contracts/PredictionPool.py
> Windows note: if the linter output crashes with UnicodeEncodeError (cp1252), run `$env:PYTHONUTF8=1` first.
pytest tests/direct -v
gltest tests/integration -v -s --network studionet
genlayer network set studionet
genlayer deploy --contract contracts/PredictionPool.py --args []
```

Sample calls:

```bash
genlayer call PredictionPool get_pool --args '["p1"]'
genlayer call PredictionPool pot_total --args '["p1"]'
genlayer write PredictionPool create_pool --args '["p1", "Who wins?", ["alpha", "beta"], "https://example.com/results", "2026-12-31T00:00:00Z"]'
genlayer write PredictionPool bet --args '["p1", 0, "500000000000000000", "2026-06-01T12:00:00Z"]'
genlayer write PredictionPool resolve --args '["p1", "2027-01-01T00:00:00Z"]'
```

## Design notes

- **Equivalence principle.** `_fetch_source` + prompt building live in `_judge_pool()`, executed by both
  the leader and the validator through `gl.vm.run_nondet_unsafe(_leader_fn, _validator_fn)`. The decision
  field compared is the integer `winner_index` (including `-1` = indeterminate), never free text. Storage
  writes happen only after consensus returns.
- **Validator error handling.** Non-`Return` leader results go through `_handle_leader_error`: exact
  message equality for deterministic `[EXPECTED]`/`[EXTERNAL]` failures, prefix agreement for
  `[TRANSIENT]` (nodes may observe different transient network states), anything else rejects and the
  executor rotates leaders.
- **Error taxonomy.** `[EXPECTED]` for user guards (unknown pool, betting closed, duplicate actions),
  `[EXTERNAL]` for source failures, `[TRANSIENT]` for retryable HTTP >= 500, `[LLM_ERROR]` for unusable
  model output. All raised as `gl.vm.UserError`.
- **Storage.** Only `TreeMap`/`DynArray`/scalars/`Address`; option labels and results are JSON strings,
  choices are plain strings/ints in nested maps.

## Limitations

- Balances are an **internal ledger** (`bet_amounts`): no native deposits, payouts or refunds are moved -
  `void` is bookkeeping only in this demo scope.
- Closing times rely on caller-supplied `now_iso` strings (documented simplification); a hostile caller
  could claim any time. Production would use block timestamps via consensus-provided context.
- One source URL means one point of failure; a paywalled or edited page can flip the outcome.
- No fee model, odds curve or proportional payout math is implemented yet.
