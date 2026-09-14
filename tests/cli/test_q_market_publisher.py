from q_backend.cli.q_market_publisher import main


def test_empty_symbols_are_refused(capsys):
    assert main([]) == 1
    assert "no symbols configured" in capsys.readouterr().err
