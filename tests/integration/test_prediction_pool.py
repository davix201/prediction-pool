from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded


def test_prediction_pool_deploy_create_pool_and_view():
    factory = get_contract_factory("PredictionPool")
    contract = factory.deploy()
    tx = contract.create_pool(
        args=[
            "p1",
            "Will the proposal pass?",
            ["yes", "no"],
            "https://example.com/source",
            "2026-12-31T00:00:00Z",
        ]
    ).transact()
    assert tx_execution_succeeded(tx)
    pool = contract.get_pool(args=["p1"]).call()
    assert pool["pool_id"] == "p1"
    assert pool["options"] == ["yes", "no"]
    assert pool["resolved"] is False
