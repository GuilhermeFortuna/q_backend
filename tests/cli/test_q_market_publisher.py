from q_backend.cli.q_market_publisher import main
from q_backend.observability.systemd import EX_CONFIG


def test_empty_symbols_are_refused(capsys):
    assert main([]) == EX_CONFIG
    assert "no symbols configured" in capsys.readouterr().err
