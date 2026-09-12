import pytest
from q_backend.api.routers.strategies import (
    list_strategies,
    get_custom_strategies,
    save_custom_strategy,
    delete_custom_strategy,
    CustomStrategySaveRequest,
)
from q_backend.backtesting.custom_strategy_store import save_custom_strategies
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def clean_custom_strategies(tmp_path, monkeypatch):
    mock_file = tmp_path / "custom_strategies.json"
    monkeypatch.setattr("q_backend.backtesting.custom_strategy_store.custom_strategies_path", lambda: mock_file)
    save_custom_strategies([])


def test_custom_strategies_crud_functions():
    # 1. Get empty custom strategies
    response = get_custom_strategies()
    assert response == []

    # 2. Save a custom strategy
    req = CustomStrategySaveRequest(
        name="MyCustomRSI",
        base_strategy="RSIMeanReversion",
        description="RSI strategy with stop loss and take profit",
        parameters={
            "period": 10,
            "oversold": 25.0,
            "overbought": 75.0,
            "stop_loss_pct": 0.015,
            "take_profit_pct": 0.03,
        },
    )
    response = save_custom_strategy(req)
    assert response["status"] == "success"

    # 3. Retrieve saved custom strategy
    response = get_custom_strategies()
    assert len(response) == 1
    assert response[0]["name"] == "MyCustomRSI"
    assert response[0]["parameters"]["stop_loss_pct"] == 0.015

    # 4. Verify custom strategy is included in global strategy registry list
    response = list_strategies()
    strategies = [s.name for s in response["strategies"]]
    assert "MyCustomRSI" in strategies

    # 5. Overwrite validation failure (built-in name conflict)
    req_conflict = CustomStrategySaveRequest(name="RSIMeanReversion", base_strategy="RSIMeanReversion", parameters={})
    with pytest.raises(HTTPException) as excinfo:
        save_custom_strategy(req_conflict)
    assert excinfo.value.status_code == 400
    assert "Cannot overwrite built-in strategy" in excinfo.value.detail

    # 6. Invalid base strategy validation failure
    req_invalid_base = CustomStrategySaveRequest(name="AnotherCustom", base_strategy="NonExistentBase", parameters={})
    with pytest.raises(HTTPException) as excinfo:
        save_custom_strategy(req_invalid_base)
    assert excinfo.value.status_code == 400
    assert "Base strategy" in excinfo.value.detail

    # 7. Delete custom strategy
    response = delete_custom_strategy("MyCustomRSI")
    assert response["status"] == "success"

    # 8. Check empty list again
    response = get_custom_strategies()
    assert response == []
