"""统一错误类型, HTTP 层据此映射状态码。"""


class ApiError(Exception):
    """业务错误基类, 携带 HTTP 状态码与稳定错误码。"""

    status = 400
    code = "bad_request"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ValidationError(ApiError):
    status = 400
    code = "validation"


class Unauthorized(ApiError):
    status = 401
    code = "unauthorized"


class Forbidden(ApiError):
    status = 403
    code = "forbidden"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class Conflict(ApiError):
    status = 409
    code = "conflict"
