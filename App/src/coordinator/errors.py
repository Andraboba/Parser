"""Ошибки предметной области. Каждая знает свой HTTP-статус и код (раздел 5 плана)."""


class CoordinatorError(Exception):
    status = 500
    code = "internal_error"


class TokenRequired(CoordinatorError):
    status = 401
    code = "token_required"


class TokenInvalid(CoordinatorError):
    status = 401
    code = "token_invalid"


class TokenForbidden(CoordinatorError):
    status = 403
    code = "token_forbidden"


class NotAuthenticated(CoordinatorError):
    status = 401
    code = "not_authenticated"


class InvalidCredentials(CoordinatorError):
    status = 401
    code = "invalid_credentials"


class CsrfFailed(CoordinatorError):
    status = 403
    code = "csrf_failed"


class PermissionDenied(CoordinatorError):
    status = 403
    code = "permission_denied"


class UserNotFound(CoordinatorError):
    status = 404
    code = "user_not_found"


class UserConflict(CoordinatorError):
    status = 409
    code = "user_exists"


class LastAdmin(CoordinatorError):
    status = 409
    code = "last_admin"


class TooManyAttempts(CoordinatorError):
    status = 429
    code = "too_many_attempts"


class TokenNotFound(CoordinatorError):
    status = 404
    code = "token_not_found"


class WorkerNotFound(CoordinatorError):
    status = 404
    code = "worker_not_found"


class JobNotFound(CoordinatorError):
    status = 404
    code = "job_not_found"


class LeaseConflict(CoordinatorError):
    """Блок не арендован этим узлом — закрывать его нельзя."""

    status = 409
    code = "lease_conflict"


class JobStateConflict(CoordinatorError):
    status = 409
    code = "job_state_conflict"


class ProtocolMismatch(CoordinatorError):
    status = 409
    code = "protocol_version_mismatch"


class InvalidJob(CoordinatorError):
    """Параметры задания не проходят проверку."""

    status = 422
    code = "invalid_job"


class InvalidResults(CoordinatorError):
    """Результаты не соответствуют блоку."""

    status = 422
    code = "invalid_results"
