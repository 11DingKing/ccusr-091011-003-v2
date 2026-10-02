"""
放行申请双人复核工作流。

规则（高风险物资）：申请 -> 第一复核 -> 最终放行，三个岗位必须由三个
不同账号担任；低风险物资：申请 -> 最终放行，两个不同账号。所有状态迁移
都在数据库事务内完成，签署步骤上有唯一约束，配合申请行级锁，保证重复
签署与并发签署只能产生一个确定结果。
"""
from decimal import Decimal

from django.db import connections, transaction
from django.db.utils import OperationalError
from django.db.models import F
from django.utils import timezone

from .models import (
    Goods, ReleaseSignature, StockOut,
    RELEASE_EXPIRY, STEP_APPLY, STEP_REVIEW, STEP_RELEASE, STEP_LABELS,
)

PENDING_STATUSES = ('pending_review', 'pending_release')
SIGNABLE_STEPS = (STEP_REVIEW, STEP_RELEASE)

# 复核/放行岗位要求管理员权限；申请岗位仅要求账号在岗
ROLE_STEPS = (STEP_REVIEW, STEP_RELEASE)


class WorkflowError(Exception):
    """工作流规则被违反，message 可直接展示给调用方。"""

    def __init__(self, message, code=400):
        self.message = message
        self.code = code
        super().__init__(message)


def _for_update(queryset):
    """支持行锁的后端加 SELECT FOR UPDATE；不支持时退化为普通查询。

    SQLite（Django 5.1 之前）不支持行锁，但 (stock_out, step) 唯一约束
    同样能让并发签署在线性化点上决出唯一胜者，语义不受影响。
    """
    if connections[queryset.db].features.has_select_for_update:
        return queryset.select_for_update()
    return queryset


def lock_release(pk):
    """锁定并返回指定放行申请（不存在抛 StockOut.DoesNotExist）。"""
    return (
        _for_update(StockOut.objects)
        .select_related('goods__variety__category__unit', 'operator')
        .prefetch_related('signatures__signer')
        .get(pk=pk)
    )


# ==================== 提交申请 ====================

def create_release(*, applicant, goods_id, quantity, receiver,
                   receiver_dept='', remark=''):
    """申请人提交放行申请，同时落库「申请」签署记录。"""
    quantity = Decimal(str(quantity))
    if quantity <= 0:
        raise WorkflowError('出库数量必须大于0')
    if not receiver or not str(receiver).strip():
        raise WorkflowError('领用人不能为空')

    with transaction.atomic():
        goods = (
            _for_update(Goods.objects)
            .select_related('variety__category__unit')
            .get(pk=goods_id)
        )
        if not applicant.is_active:
            raise WorkflowError('账号已停用，不能提交放行申请', 403)
        if not goods.is_active:
            raise WorkflowError('该物资已停用，不能申请放行')
        if goods.quantity < quantity:
            raise WorkflowError(f'库存不足，当前库存 {goods.quantity}')

        now = timezone.now()
        high = goods.risk_level == 'high'
        stock_out = StockOut.objects.create(
            goods=goods,
            operator=applicant,
            receiver=receiver.strip(),
            receiver_dept=receiver_dept,
            quantity=quantity,
            risk_level=goods.risk_level,
            status='pending_review' if high else 'pending_release',
            current_step=STEP_REVIEW if high else STEP_RELEASE,
            expires_at=now + RELEASE_EXPIRY,
            goods_name=goods.name,
            goods_code=goods.code,
            remark=remark,
        )
        ReleaseSignature.objects.create(
            stock_out=stock_out,
            step=STEP_APPLY,
            signer=applicant,
            action='approve',
            goods_snapshot=stock_out.goods_summary(),
            remark='提交放行申请',
        )
        return stock_out


# ==================== 签署（通过/拒绝） ====================

def sign_release(*, stock_out_id, signer, step, action, remark=''):
    """对指定步骤进行签署。

    调用方必须显式声明要签署的步骤（review/release），避免并发时误签到
    下一岗位。返回更新后的 StockOut。
    """
    if step not in SIGNABLE_STEPS:
        raise WorkflowError('不支持的签署步骤')
    if action not in ('approve', 'reject'):
        raise WorkflowError('签署动作只能是 approve 或 reject')
    if action == 'reject' and not remark.strip():
        raise WorkflowError('拒绝时必须填写原因')

    # 整个签署在单事务内完成；同一申请的并发请求在申请行锁处串行化，
    # 后到者基于已提交状态重新校验，因此重复/并发签署只有一个确定结果。
    # SQLite WAL 下，快照过期的写者提交时会得到 "database is locked"，
    # 此时重试即可读到先提交者的确定状态，而不是把 500 暴露给调用方。
    attempts = 0
    while True:
        attempts += 1
        blocked = None
        try:
            with transaction.atomic():
                try:
                    stock_out = lock_release(stock_out_id)
                except StockOut.DoesNotExist:
                    raise WorkflowError('放行申请不存在', 404)

                # 过期 / 链条失权先落为终态（随本事务一起提交）
                evaluate_state(stock_out)

                if stock_out.is_terminal:
                    if stock_out.status == 'terminated':
                        blocked = f'放行流程已终止：{stock_out.terminate_reason}'
                    elif stock_out.status == 'expired':
                        blocked = '放行申请已过期，不能再签署'
                    else:
                        blocked = f'放行申请已终结（{stock_out.get_status_display()}），不能再签署'
                else:
                    expected = stock_out.next_missing_step()
                    if expected != step:
                        if expected is None:
                            blocked = '所有签署步骤均已完成'
                        else:
                            blocked = (
                                f'当前应执行的步骤是「{STEP_LABELS[expected]}」，'
                                f'不能直接签署「{STEP_LABELS[step]}」'
                            )
                    else:
                        blocked = _check_signer_eligibility(stock_out, signer, step)

                if blocked is None:
                    signature = _insert_signature(stock_out, signer, step, action, remark)
                    if signature is None:
                        # 唯一约束兜底：该步骤已存在签署记录
                        blocked = '该步骤已被签署，结果以先完成的请求为准'
                    elif action == 'reject':
                        stock_out = _apply_reject(stock_out, signature)
                    else:
                        stock_out = _apply_approve(stock_out, signature)
        except OperationalError as exc:
            if attempts < 3 and _is_lock_conflict(exc):
                continue
            raise WorkflowError('系统繁忙，请稍后重试', 409)
        break

    # 拒绝签署的请求在事务正常提交后再报错：失权终止等状态不被回滚
    if blocked is not None:
        raise WorkflowError(blocked, 409)
    return stock_out


def _is_lock_conflict(exc):
    """SQLite 的 database is locked / PostgreSQL 行锁等待失败。"""
    message = str(exc).lower()
    return (
        'locked' in message
        or 'could not obtain lock' in message
        or ('lock' in message and 'timeout' in message)
    )


def _insert_signature(stock_out, signer, step, action, remark):
    """创建签署记录；撞 (stock_out, step) 唯一约束时返回 None。"""
    from django.db import IntegrityError

    snapshot = stock_out.goods_summary()
    try:
        # 嵌套事务即 SAVEPOINT：约束冲突只回滚到保存点，外层事务的
        # 状态更新不受影响（PostgreSQL 上尤其必要）
        with transaction.atomic():
            return ReleaseSignature.objects.create(
                stock_out=stock_out,
                step=step,
                signer=signer,
                action=action,
                goods_snapshot=snapshot,
                remark=remark,
            )
    except IntegrityError:
        return None


def _apply_reject(stock_out, signature):
    now = timezone.now()
    stock_out.status = 'rejected'
    stock_out.current_step = signature.step
    stock_out.rejected_at = now
    stock_out.reject_step = signature.step
    stock_out.rejected_by_id = signature.signer_id
    stock_out.reject_reason = signature.remark
    stock_out.save(update_fields=[
        'status', 'current_step', 'rejected_at', 'reject_step',
        'rejected_by_id', 'reject_reason',
    ])
    return stock_out


def _apply_approve(stock_out, signature):
    nxt = stock_out.next_step(signature.step)
    if nxt is not None:
        stock_out.current_step = nxt
        stock_out.status = (
            'pending_release' if nxt == STEP_RELEASE else 'pending_review'
        )
        stock_out.save(update_fields=['current_step', 'status'])
        return stock_out

    # 全部岗位签署完成：扣减库存并放行（与签署同一事务，同生共死）
    goods = _for_update(Goods.objects).get(pk=stock_out.goods_id)
    if goods.quantity < stock_out.quantity:
        # 库存被其他业务占用：本次放行不能落库，交由调用方决定拒绝或重试
        raise WorkflowError(
            f'库存不足（当前 {goods.quantity}），无法完成放行', 409
        )
    goods.quantity = F('quantity') - stock_out.quantity
    goods.save(update_fields=['quantity'])

    now = timezone.now()
    stock_out.status = 'released'
    stock_out.current_step = signature.step
    stock_out.released_at = now
    stock_out.stock_out_time = now
    stock_out.save(update_fields=[
        'status', 'current_step', 'released_at', 'stock_out_time',
    ])
    return stock_out


# ==================== 权限与三岗分离 ====================

def _check_signer_eligibility(stock_out, signer, step):
    """当前签署人合法，且申请链条上所有人的权限都未被撤销。

    返回 None 表示通过，否则返回可直接展示的拒绝原因。
    """
    if not signer.is_active:
        return '账号已停用，无法签署'
    if step in ROLE_STEPS and not signer.is_admin:
        return f'当前账号没有「{STEP_LABELS[step]}」权限，需要管理员身份'

    # 三岗分离：申请人、第一复核人、最终放行人不得为同一账号
    if signer.pk == stock_out.operator_id:
        return f'申请人不能兼任「{STEP_LABELS[step]}」，必须由另一名人员签署'
    if stock_out.signatures.filter(signer_id=signer.pk).exists():
        return '该账号已在本申请的其他岗位签署，不能再次签署'

    # 签署后权限撤销：链条上任何人失权，立即阻止未完成的放行
    revoked = _find_revoked_party(stock_out)
    if revoked is not None:
        _terminate(stock_out, revoked)
        return f'{revoked}，放行流程终止'
    return None


def _find_revoked_party(stock_out):
    """返回权限已被撤销的一方描述；无则返回 None。

    优先使用 prefetch_related('signatures__signer') 预取的缓存。
    """
    operator = stock_out.operator
    if operator is not None and not operator.is_active:
        return f'申请人（{operator.username}）的账号已被停用，签署权限已被撤销'

    # all() 命中 prefetch 缓存；未预取时逐条查询（链条最多 3 条）
    for sig in stock_out.signatures.all():
        person = sig.signer
        label = STEP_LABELS.get(sig.step, sig.step)
        if person is None:
            return f'{label}签署人账号已被删除，签署权限已被撤销'
        if not person.is_active:
            return f'{label}签署人（{person.username}）的账号已被停用，签署权限已被撤销'
        if sig.step in ROLE_STEPS and not person.is_admin:
            return f'{label}签署人（{person.username}）的管理权限已被撤销'
    return None


def _terminate(stock_out, reason):
    stock_out.status = 'terminated'
    stock_out.terminate_reason = reason
    stock_out.save(update_fields=['status', 'terminate_reason'])


# ==================== 过期 / 失权状态维护 ====================

def evaluate_state(stock_out):
    """在已锁定的申请行上判定过期与权限撤销，落库为终态后返回。"""
    if stock_out.is_terminal:
        return stock_out
    if stock_out.is_expired:
        stock_out.status = 'expired'
        stock_out.save(update_fields=['status'])
        return stock_out
    revoked = _find_revoked_party(stock_out)
    if revoked is not None:
        _terminate(stock_out, revoked)
    return stock_out


def sweep_pending():
    """批量把已过期或签署链条失权的在途申请置为终态（列表接口用）。"""
    with transaction.atomic():
        expired = StockOut.objects.filter(
            status__in=PENDING_STATUSES,
            expires_at__isnull=False,
            expires_at__lt=timezone.now(),
        )
        expired.update(status='expired')

        pending = StockOut.objects.filter(status__in=PENDING_STATUSES)
        for stock_out in pending.select_related('operator').prefetch_related(
            'signatures__signer'
        ):
            revoked = _find_revoked_party(stock_out)
            if revoked is not None:
                StockOut.objects.filter(pk=stock_out.pk).update(
                    status='terminated', terminate_reason=revoked
                )
