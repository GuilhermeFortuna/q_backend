def test_contracts_import():
    from q_contracts.topics import TOPICS

    assert "jobs.terminal" in TOPICS
    assert TOPICS["jobs.terminal"].topic_class == "durable"
