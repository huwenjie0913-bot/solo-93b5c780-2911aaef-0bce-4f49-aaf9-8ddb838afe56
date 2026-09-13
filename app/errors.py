"""统一的 4xx 错误模型。

每个错误携带稳定的 ``code``、人类可读的 ``message``，以及指向请求字段的
``field`` 路径（如 ``components[1].qty`` 或 ``components[0].components[1].code``）。
"""


class APIError(Exception):
    status_code = 400

    def __init__(self, code, message, field=None, details=None, status_code=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code

    def to_response(self):
        body = {"error": {"code": self.code, "message": self.message}}
        if self.field is not None:
            body["error"]["field"] = self.field
        if self.details:
            body["error"]["details"] = self.details
        return body, self.status_code


# ---- 细分错误（便于客户端编程处理）-----------------------------------------

def malformed(message, field=None, details=None):
    return APIError("MALFORMED", message, field, details, 400)


def not_found(message, field=None, details=None):
    return APIError("NOT_FOUND", message, field, details, 404)


def conflict(message, field=None, details=None):
    """版本已存在但内容不同（不可变冲突），或换算因子自相矛盾。"""
    return APIError("CONFLICT", message, field, details, 409)


def circular(message, details=None):
    return APIError("CIRCULAR_REFERENCE", message, "components", details, 422)


def unknown_ingredient(message, field, details=None):
    return APIError("UNKNOWN_INGREDIENT", message, field, details, 422)


def unknown_recipe(message, field, details=None):
    return APIError("UNKNOWN_RECIPE", message, field, details, 422)


def unit_conflict(message, field, details=None):
    return APIError("UNIT_CONFLICT", message, field, details, 422)


def missing_declaration(message, field, details=None):
    return APIError("MISSING_DECLARATION", message, field, details, 422)
