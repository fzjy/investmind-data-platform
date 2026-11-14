from __future__ import annotations


class TushareError(Exception):
    retryable = False


class TushareAuthError(TushareError):
    pass


class TusharePermissionError(TushareError):
    pass


class TushareSchemaError(TushareError):
    pass


class TushareTransientError(TushareError):
    retryable = True


class TushareNetworkError(TushareTransientError):
    pass


class TushareRateLimitError(TushareTransientError):
    pass


class TushareServerError(TushareTransientError):
    pass


class TushareResponseError(TushareError):
    pass
