class AppError(Exception):
    status_code = 500
    message = "Internal server error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.message
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    message = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    message = "Conflict"


class ValidationError(AppError):
    status_code = 422
    message = "Validation error"
