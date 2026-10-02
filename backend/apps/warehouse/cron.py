"""
可由管理命令或进程内调度器调用的周期任务（放行申请生命周期结算）。
"""
import logging

from .services import block_requests_with_revoked_signers, expire_due_requests

logger = logging.getLogger('apps')


def settle_release_requests():
    """结算放行申请：过期置为已过期、签署人权限撤销置为权限阻止。

    建议短间隔执行（如每 5 分钟）；即使不执行，签署与查询接口也会惰性结算。
    """
    expired = expire_due_requests()
    blocked = block_requests_with_revoked_signers()
    if expired or blocked:
        logger.info(
            '放行申请结算完成：新过期 %s 条，权限阻断 %s 条', expired, blocked
        )
    return {'expired': expired, 'blocked': blocked}
