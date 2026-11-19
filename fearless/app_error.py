"""Expected failures raised by draft rehearsal operations."""


class RehearsalError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail
