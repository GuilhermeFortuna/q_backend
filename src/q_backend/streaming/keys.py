STREAM_EPOCH_KEY = "q:stream:epoch"


def stream_key(topic: str) -> str:
    return f"q:stream:{topic}"


def seq_key(topic: str) -> str:
    return f"q:seq:{topic}"


def topic_epoch_key(topic: str) -> str:
    return f"q:epoch:{topic}"


def latest_key(topic: str) -> str:
    return f"q:latest:{topic}"
