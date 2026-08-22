class IngestionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, details: object | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)
