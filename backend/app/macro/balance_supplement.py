"""封闭的余额补充目标；不接受任意字段/API或用户可配置的表名。"""
from enum import Enum


class BalanceSupplementTarget(Enum):
    RECEIVABLES = 'receivables'
    ORDERS = 'orders'

    @property
    def field(self):
        return 'accounts_receiv' if self is RECEIVABLES else 'contract_liab'

    @property
    def contract(self):
        return f'enterprise-{self.value}-preview-v1'

    @property
    def witness(self):
        return f'enterprise-{self.value}-witness-v1'

    @property
    def table_prefix(self):
        return 'enterprise_receivable' if self is RECEIVABLES else 'enterprise_order'

    @property
    def models(self):
        if self is RECEIVABLES:
            from app.db.models.enterprise_receivables import ReceivableAttempt, ReceivableResponse, ReceivableSupplement
            return ReceivableAttempt, ReceivableResponse, ReceivableSupplement
        from app.db.models.enterprise_orders import OrderAttempt, OrderResponse, OrderSupplement
        return OrderAttempt, OrderResponse, OrderSupplement


RECEIVABLES = BalanceSupplementTarget.RECEIVABLES
ORDERS = BalanceSupplementTarget.ORDERS


def target_options(target):
    """旧路径不改变调用形状，保留已有注入点及请求身份。"""
    if not isinstance(target, BalanceSupplementTarget):
        raise ValueError('ENTERPRISE_SUPPLEMENT_TARGET_INVALID')
    return {} if target is RECEIVABLES else {'target': target}
