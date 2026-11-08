"""
Optimizer-specific exceptions.
These map to HTTP error codes in the backend — the optimizer itself has no HTTP dependency.
"""


class MissingBaselineError(Exception):
    """
    Raised when an owner does not have an estimate for the service required
    by one of its own deliverables.
    Maps to HTTP 422 MISSING_BASELINE in the backend.
    """

    def __init__(self, missing_fields: list[str]):
        self.missing_fields = missing_fields
        msg = (
            f"Owner estimate missing for: {', '.join(missing_fields)}. "
            "Each owner must supply an estimate for every service its own "
            "deliverables require. Missing estimates mean unsupported service, "
            "not zero effort."
        )
        super().__init__(msg)
