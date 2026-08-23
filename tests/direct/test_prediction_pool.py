import json

CONTRACT = "contracts/PredictionPool.py"
SOURCE_PATTERN = r".*example\.com.*"
LLM_PATTERN = r"prediction market"
CLOSES_AT = "2026-01-01T00:00:00Z"
BEFORE_CLOSE = "2025-12-31T23:59:59Z"
AFTER_CLOSE = "2026-01-02T00:00:00Z"
PAGE_TEXT = "Final score: Rockets 112, Comets 109. The Rockets won the championship game."


def _mock_resolve(direct_vm, payload):
    direct_vm.mock_web(SOURCE_PATTERN, {"status": 200, "body": PAGE_TEXT})
    direct_vm.mock_llm(LLM_PATTERN, json.dumps(payload))


def _create_sample_pool(contract):
    contract.create_pool(
        "p1",
        "Who wins the grand final?",
        ["Rockets", "Comets"],
        "https://example.com/results",
        CLOSES_AT,
    )


def test_owner_creates_pool_happy_path(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    pool = contract.get_pool("p1")
    assert pool["pool_id"] == "p1"
    assert pool["question"] == "Who wins the grand final?"
    assert pool["options"] == ["Rockets", "Comets"]
    assert pool["resolved"] is False
    assert pool["result"] == ""


def test_create_pool_requires_owner(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] only owner"):
        _create_sample_pool(contract)


def test_create_pool_invalid_input_reverts(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT)
    with direct_vm.expect_revert("between 2 and 4 options"):
        contract.create_pool(
            "p1", "Q?", ["only-one"], "https://example.com/s", CLOSES_AT
        )
    with direct_vm.expect_revert("source_url must be http(s)"):
        contract.create_pool("p2", "Q?", ["a", "b"], "ftp://example.com/s", CLOSES_AT)
    with direct_vm.expect_revert("are required"):
        contract.create_pool("", "Q?", ["a", "b"], "https://example.com/s", CLOSES_AT)


def test_duplicate_pool_id_reverts(direct_vm, direct_deploy):
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    with direct_vm.expect_revert("[EXPECTED] duplicate pool id"):
        _create_sample_pool(contract)


def test_bet_flow_updates_ledger_and_pot(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    direct_vm.sender = direct_alice
    contract.bet("p1", 0, 1000, BEFORE_CLOSE)
    direct_vm.sender = direct_bob
    contract.bet("p1", 1, 500, BEFORE_CLOSE)
    alice_bet = contract.get_bet("p1", direct_alice)
    assert int(alice_bet["option_idx"]) == 0
    assert int(alice_bet["amount_atto"]) == 1000
    bob_bet = contract.get_bet("p1", direct_bob)
    assert int(bob_bet["option_idx"]) == 1
    assert int(contract.pot_total("p1")) == 1500


def test_bet_guards_revert(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] option index out of range"):
        contract.bet("p1", 2, 100, BEFORE_CLOSE)
    with direct_vm.expect_revert("[EXPECTED] amount_atto must be positive"):
        contract.bet("p1", 0, 0, BEFORE_CLOSE)
    with direct_vm.expect_revert("[EXPECTED] betting is closed"):
        contract.bet("p1", 0, 100, AFTER_CLOSE)
    contract.bet("p1", 0, 100, BEFORE_CLOSE)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("address already bet"):
        direct_vm.sender = direct_alice
        contract.bet("p1", 1, 100, BEFORE_CLOSE)
    with direct_vm.expect_revert("[EXPECTED] unknown pool id"):
        contract.get_bet("missing", direct_bob)


def test_resolve_before_close_reverts(direct_vm, direct_deploy):
    _mock_resolve(direct_vm, {"winner_index": 0, "reason": "Rockets won"})
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    with direct_vm.expect_revert("[EXPECTED] pool is still open"):
        contract.resolve("p1", BEFORE_CLOSE)


def test_full_resolve_agreement_picks_winner(direct_vm, direct_deploy):
    _mock_resolve(direct_vm, {"winner_index": 0, "reason": "Rockets won the final"})
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    result = contract.resolve("p1", AFTER_CLOSE)
    assert result is True
    pool = contract.get_pool("p1")
    assert pool["resolved"] is True
    assert pool["result"] == 0
    assert pool["reason"] == "Rockets won the final"
    assert contract.winner("p1") == "Rockets"


def test_captured_validator_agrees_with_leader_result(direct_vm, direct_deploy):
    _mock_resolve(direct_vm, {"winner_index": 1, "reason": "Comets upset the field"})
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    contract.resolve("p1", AFTER_CLOSE)
    verdict = direct_vm.run_validator()
    assert verdict is True


def test_indeterminate_source_voids_pool(direct_vm, direct_deploy):
    _mock_resolve(
        direct_vm,
        {"winner_index": -1, "reason": "Source does not name a winner"},
    )
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    result = contract.resolve("p1", AFTER_CLOSE)
    assert result is True
    pool = contract.get_pool("p1")
    assert pool["result"] == "void"
    assert contract.winner("p1") == "void"


def test_double_resolve_and_unknown_pool_reverts(direct_vm, direct_deploy):
    _mock_resolve(direct_vm, {"winner_index": 0, "reason": "decided"})
    contract = direct_deploy(CONTRACT)
    _create_sample_pool(contract)
    contract.resolve("p1", AFTER_CLOSE)
    with direct_vm.expect_revert("pool already resolved"):
        contract.resolve("p1", AFTER_CLOSE)
    with direct_vm.expect_revert("[EXPECTED] unknown pool id"):
        contract.resolve("missing", AFTER_CLOSE)
