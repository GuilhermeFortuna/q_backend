STREAM_EPOCH_KEY = "q:stream:epoch"


def stream_key(topic: str) -> str:
    return f"q:stream:{topic}"


def seq_key(topic: str) -> str:
    return f"q:seq:{topic}"


def topic_epoch_key(topic: str) -> str:
    return f"q:epoch:{topic}"


def latest_key(topic: str) -> str:
    return f"q:latest:{topic}"


# Session trade tape (Q-080): the API talks to the publisher only through these keys
# and the shared snapshot cache directory.
TRADE_REQUEST_QUEUE_KEY = "q:trades:requests"
TRADE_INSTANCE_KEY = "q:trades:instance"
TRADE_DIAGNOSTICS_KEY = "q:trades:diagnostics"


def trade_result_key(request_id: str) -> str:
    return f"q:trades:result:{request_id}"


def trade_generation_key(symbol: str) -> str:
    return f"q:trades:generation:{symbol}"
