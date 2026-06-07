class ExpectedTrialFailure(Exception):
    """Raised when a trial fails for an expected, non-bug reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
