"""跨编排边界传递稳定分类，绝不按异常正文识别锁忙或配置。"""


class JobLockedError(ValueError):
    pass


class JobConfigurationError(ValueError):
    pass
