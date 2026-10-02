"""
放行申请签署状态机服务。

角色与流程（按申请时快照的物资风险等级决定）：
- 普通物资：申请人(applicant) → 最终放行人(final_releaser)
- 高风险物资：申请人(applicant) → 第一复核人(first_reviewer) → 最终放行人(final_releaser)
三个角色不得由同一账号兼任。

终态（不可逆）：released 已放行 / rejected 已拒绝 / expired 已过期 / blocked 权限阻止。

并发安全（重复签署与并发签署只产生一个确定结果）：
1. 签署临界区由事务首条 gate UPDATE 对申请行加写锁串行化（MySQL/PG 为行锁，
   SQLite 为库级保留锁）；
2. 每个 (申请, 角色) 受数据库唯一约束兜底；
3. 客户端按详情接口的 next_step 显式回传要签署的 role，锁内校验它仍是当前
   下一步——并发落败方在胜出方提交后才持锁，发现步骤已前移即确定返回 409，
   绝不会把“第一复核”静默记成“最终放行”；
4. SQLite 表锁冲突（不触发 busy timeout）做有界短重试，事务整体回滚无部分写入；
5. 最终放行用条件 UPDATE 抢占状态，并用 F() 条件 UPDATE 原子扣减库存，杜绝超卖。

权限撤销：已签署账号被停用(is_active=False)或降级为普通用户(role='user')后，
尚未完成的放行一律阻断（惰性结算 + 定时任务双通道）。
"""
from datetime import timedelta
from time import sleep as time_sleep

from django.conf import settings
from django.db import IntegrityError, OperationalError as DbOperationalError, transaction
from django.db.models import F as models_F
from django.utils import timezone

from apps.authentication.models import User
from ..models import Goods, ReleaseRequest, ReleaseSignature, StockOut

# 具备放行签署权限的角色（在职管理员）
AUTHORIZED_ROLES = ('admin', 'superadmin')

# 各数据库并发锁冲突错误信息特征（SQLite/MySQL/PostgreSQL）
_LOCK_MARKERS = (
    'locked',                    # SQLite: database (table) is locked
    'lock wait timeout',         # MySQL: 1205
    'could not obtain lock',     # PostgreSQL
    'deadlock',                  # MySQL/PostgreSQL 死锁
)
_LOCK_RETRY_TIMES = 5
_LOCK_RETRY_DELAY = 0.05

ROLE_LABELS = dict(ReleaseRequest.Role.choices)


class ReleaseError(Exception):
    """放行业务校验失败。"""

    def __init__(self, message, code=400):
        self.message = message
        self.code = code
        super().__init__(message)


class LifecycleSettledError(ReleaseError):
    """签署事务内检测到过期 / 权限撤销并已写入终态。

    异常会回滚当前 atomic 块，外层负责在独立事务中重新结算落库后再抛出，
    保证终态不随校验失败一起回滚。
    """

    def __init__(self, status):
        self.settled_status = status
        if status == ReleaseRequest.Status.EXPIRED:
            super().__init__('放行申请已过期，不能继续签署', code=409)
        else:
            super().__init__('已有签署人权限被撤销，放行已被阻断', code=409)


def has_release_authority(user):
    """该账号当前是否具备放行签署权限（在职管理员）。"""
    if not getattr(user, 'id', None):
        return False
    return User.objects.filter(
        pk=user.id, is_active=True, role__in=AUTHORIZED_ROLES
    ).exists()


def validity_delta():
    return timedelta(seconds=getattr(
        settings, 'RELEASE_REQUEST_TTL_SECONDS', 24 * 60 * 60
    ))


def _is_lock_error(exc):
    return isinstance(exc, DbOperationalError) and any(
        marker in str(exc).lower() for marker in _LOCK_MARKERS
    )


def _locked_request(request_id):
    """加行锁取出申请，并预载签署记录与物资摘要关联。"""
    return (
        ReleaseRequest.objects
        .select_for_update()
        .prefetch_related('signatures__signer')
        .select_related('goods__variety__category__unit', 'applicant')
        .get(pk=request_id)
    )


def _prior_signers(request_obj):
    """已完成签署的 (role, user_id, user) 列表，按角色顺序。"""
    by_role = {s.role: s for s in request_obj.signatures.all()}
    return [
        (role, by_role[role].signer_id, by_role[role].signer)
        for role in request_obj.required_roles if role in by_role
    ]


def _revoked_prior_signer(request_obj):
    """找出权限已被撤销的已签署人；没有则返回 None。"""
    prior = _prior_signers(request_obj)
    valid_ids = set(
        User.objects.filter(
            pk__in=[uid for _, uid, _ in prior],
            is_active=True,
            role__in=AUTHORIZED_ROLES,
        ).values_list('pk', flat=True)
    )
    for role, uid, signer in prior:
        if uid not in valid_ids:
            return role, signer
    return None


def _label(role):
    return ROLE_LABELS.get(role, role)


def _mark_blocked(request_obj, role, signer):
    """签署后权限被撤销：阻断尚未完成的放行（幂等）。"""
    name = getattr(signer, 'real_name', '') or getattr(signer, 'username', '') \
        or f'#{getattr(signer, "id", "?")}'
    request_obj.status = ReleaseRequest.Status.BLOCKED
    request_obj.closed_reason = f'{_label(role)}（{name}）权限已撤销，放行终止'
    request_obj.save(update_fields=['status', 'closed_reason', 'updated_at'])


def _mark_expired(request_obj):
    request_obj.status = ReleaseRequest.Status.EXPIRED
    request_obj.closed_reason = '超过签署截止时间，放行申请已过期'
    request_obj.save(update_fields=['status', 'closed_reason', 'updated_at'])


def settle_lifecycle(request_obj):
    """惰性结算：过期 / 已签署人权限撤销 → 推进到对应终态（持锁调用）。"""
    if request_obj.is_terminal:
        return
    if timezone.now() >= request_obj.expires_at:
        _mark_expired(request_obj)
        return
    revoked = _revoked_prior_signer(request_obj)
    if revoked is not None:
        _mark_blocked(request_obj, revoked[0], revoked[1])


def _insert_signature(request_obj, role, signer, action, comment):
    """插入签署记录；并发抢占失败抛 409（嵌套 savepoint 保证事务可继续）。

    插入后显式清除实例上的 signatures prefetch 缓存（反向外键 create
    不会自动失效它），使后续 next_role() / 互斥判断读到最新状态。
    """
    try:
        with transaction.atomic():
            signature = ReleaseSignature.objects.create(
                request=request_obj, role=role, signer=signer,
                action=action, comment=comment,
            )
    except IntegrityError:
        raise ReleaseError('该步骤已被签署，请刷新后重试', code=409)
    cache = getattr(request_obj, '_prefetched_objects_cache', None)
    if cache is not None:
        cache.pop('signatures', None)
    return signature


@transaction.atomic
def create_release_request(*, applicant, goods, quantity, receiver,
                           receiver_dept='', purpose=''):
    """提交放行申请，提交即视为申请人完成签署。"""
    if not has_release_authority(applicant):
        raise ReleaseError('申请人账号无放行申请权限（需在职管理员）', code=403)
    if quantity <= 0:
        raise ReleaseError('放行数量必须大于 0')
    if not receiver or not str(receiver).strip():
        raise ReleaseError('领用人不能为空')

    # 预检仅为友好提示；最终防超卖由放行时的条件 UPDATE 原子保证
    goods_obj = Goods.objects.get(pk=goods.pk)
    if not goods_obj.is_active:
        raise ReleaseError('该物资已停用，不能申请放行')
    if quantity > goods_obj.quantity:
        raise ReleaseError(f'库存不足，当前库存为 {goods_obj.quantity}')

    request_obj = ReleaseRequest.objects.create(
        goods=goods_obj,
        quantity=quantity,
        receiver=receiver.strip(),
        receiver_dept=receiver_dept,
        purpose=purpose,
        risk_level=goods_obj.risk_level,
        applicant=applicant,
        expires_at=timezone.now() + validity_delta(),
    )
    _insert_signature(
        request_obj, ReleaseRequest.Role.APPLICANT, applicant,
        ReleaseSignature.Action.APPROVED, purpose,
    )
    return request_obj


@transaction.atomic
def _sign_inner(*, request_id, signer, action, comment, intended_role):
    """持有行锁的签署核心；返回申请对象。

    ``intended_role`` 是本次要签署的步骤。锁内校验它必须仍是当前下一步，
    否则视为被并发请求抢先签署，返回 409。
    """
    # Gate：事务第一条语句即对申请行加写锁，把整个签署临界区串行化
    gated = ReleaseRequest.objects.filter(pk=request_id).update(
        updated_at=models_F('updated_at')
    )
    if gated == 0:
        raise ReleaseError('放行申请不存在', code=404)

    try:
        request_obj = _locked_request(request_id)
    except ReleaseRequest.DoesNotExist:
        raise ReleaseError('放行申请不存在', code=404)

    # 终态不可逆：重复签署只能看到同一个确定结果
    if request_obj.is_terminal:
        raise ReleaseError(
            f'放行申请已结束（{request_obj.get_status_display()}），不能重复签署',
            code=409,
        )

    # 惰性结算过期 / 权限撤销：写入终态并抛出，由外层保证终态落库
    settle_lifecycle(request_obj)
    if request_obj.is_terminal:
        raise LifecycleSettledError(request_obj.status)

    if not has_release_authority(signer):
        raise ReleaseError('当前账号无放行签署权限（需在职管理员）', code=403)

    role = request_obj.next_role()
    if role is None:
        raise ReleaseError('该申请没有待签署步骤', code=409)

    # 乐观并发控制：待签步骤已前移 → 确定 409，不把复核人记成放行人
    if role != intended_role:
        raise ReleaseError(
            '该步骤刚刚已被他人签署，请刷新后重试', code=409
        )

    prior = _prior_signers(request_obj)
    if any(uid == signer.id for _, uid, _ in prior):
        labels = '、'.join(_label(r) for r, _, _ in prior)
        raise ReleaseError(
            f'申请人、复核人与放行人不得由同一账号兼任，该账号已担任：{labels}',
            code=409,
        )

    if action == ReleaseSignature.Action.REJECTED:
        _insert_signature(
            request_obj, role, signer,
            ReleaseSignature.Action.REJECTED, comment,
        )
        reason = f'{_label(role)}拒绝'
        if comment:
            reason = f'{reason}：{comment}'
        request_obj.status = ReleaseRequest.Status.REJECTED
        request_obj.closed_reason = reason
        request_obj.save(update_fields=['status', 'closed_reason', 'updated_at'])
        return request_obj

    _insert_signature(
        request_obj, role, signer,
        ReleaseSignature.Action.APPROVED, comment,
    )

    if request_obj.next_role() is not None:
        request_obj.save(update_fields=['updated_at'])
        return request_obj

    # 最后一步：条件 UPDATE 抢占“放行”结果
    claimed = ReleaseRequest.objects.filter(
        pk=request_obj.pk, status=ReleaseRequest.Status.SIGNING
    ).update(version=request_obj.version + 1)
    if claimed != 1:
        raise ReleaseError('放行申请状态已变更，请刷新后重试', code=409)

    goods_obj = Goods.objects.get(pk=request_obj.goods_id)
    if not goods_obj.is_active:
        raise ReleaseError('物资已停用，不能放行', code=409)

    # 原子条件扣减（不依赖行锁，SQLite/MySQL/PG 均安全）：库存不足 affected=0
    deducted = Goods.objects.filter(
        pk=goods_obj.pk, quantity__gte=request_obj.quantity
    ).update(quantity=models_F('quantity') - request_obj.quantity)
    if deducted != 1:
        raise ReleaseError('库存不足，放行失败', code=409)

    stock_out = StockOut.objects.create(
        goods=goods_obj,
        operator=signer,
        receiver=request_obj.receiver,
        receiver_dept=request_obj.receiver_dept,
        quantity=request_obj.quantity,
        status='completed',
        stock_out_time=timezone.now(),
        remark=f'放行申请 #{request_obj.pk} 完成',
    )

    request_obj.refresh_from_db()
    request_obj.status = ReleaseRequest.Status.RELEASED
    request_obj.stock_out = stock_out
    request_obj.closed_reason = '全部签署完成，已放行'
    request_obj.save(update_fields=['status', 'stock_out', 'closed_reason', 'updated_at'])
    return request_obj


def _get_request_for_view(request_id):
    """无锁读取申请（供外层确定下一步），不存在抛 404，终态抛 409。"""
    current = (
        ReleaseRequest.objects
        .prefetch_related('signatures')
        .filter(pk=request_id)
        .first()
    )
    if current is None:
        raise ReleaseError('放行申请不存在', code=404)
    return current


def sign_release_request(*, request_id, signer, action, comment='', role=None):
    """对放行申请执行下一步签署（同意 / 拒绝）。

    :param role: 本次要签署的步骤，取自详情接口 next_step。为 None 时由
        服务端读取当前下一步（仅限无并发的串行调用 / 脚本）。
    """
    if action not in ReleaseSignature.Action.values:
        raise ReleaseError("签署动作仅支持 approved / rejected")
    if role is not None and role not in ReleaseRequest.Role.values:
        raise ReleaseError('未知的签署步骤', code=400)

    intended_role = role
    if intended_role is None:
        current = _get_request_for_view(request_id)
        if current.is_terminal:
            raise ReleaseError(
                f'放行申请已结束（{current.get_status_display()}），不能重复签署',
                code=409,
            )
        intended_role = current.next_role()
        if intended_role is None:
            raise ReleaseError('该申请没有待签署步骤', code=409)

    last_lock_error = None
    for attempt in range(_LOCK_RETRY_TIMES + 1):
        try:
            return _sign_inner(
                request_id=request_id, signer=signer, action=action,
                comment=comment, intended_role=intended_role,
            )
        except LifecycleSettledError as exc:
            # 内层事务因过期 / 撤销回滚后，在独立事务重新结算落库终态
            try:
                with transaction.atomic():
                    settle_lifecycle(_locked_request(request_id))
            except DbOperationalError as settle_exc:
                if not _is_lock_error(settle_exc):
                    raise
            raise ReleaseError(exc.message, code=exc.code) from exc
        except DbOperationalError as exc:
            if not _is_lock_error(exc):
                raise
            if attempt >= _LOCK_RETRY_TIMES:
                last_lock_error = exc
                break
            time_sleep(_LOCK_RETRY_DELAY * (attempt + 1))

    # 重试用尽仍冲突：结果必须确定，统一 409（绝不放行）
    latest = (
        ReleaseRequest.objects.filter(pk=request_id)
        .values_list('status', flat=True).first()
    )
    if latest == ReleaseRequest.Status.EXPIRED:
        raise ReleaseError('放行申请已过期，不能继续签署', code=409) from last_lock_error
    if latest == ReleaseRequest.Status.BLOCKED:
        raise ReleaseError('已有签署人权限被撤销，放行已被阻断', code=409) from last_lock_error
    raise ReleaseError('签署并发冲突，请刷新后重试', code=409) from last_lock_error


@transaction.atomic
def settle_request(request_id):
    """供查询接口调用的惰性结算（过期 / 权限撤销即时可见）。"""
    try:
        request_obj = _locked_request(request_id)
    except ReleaseRequest.DoesNotExist:
        raise ReleaseError('放行申请不存在', code=404)
    settle_lifecycle(request_obj)
    return request_obj


def expire_due_requests():
    """定时任务：把已到截止时间仍在签署中的申请批量置为过期。返回过期数量。"""
    count = 0
    with transaction.atomic():
        due = list(
            ReleaseRequest.objects
            .select_for_update()
            .filter(status=ReleaseRequest.Status.SIGNING, expires_at__lte=timezone.now())
        )
        for request_obj in due:
            _mark_expired(request_obj)
            count += 1
    return count


def block_requests_with_revoked_signers():
    """定时任务：主动阻断已签署人权限被撤销的在途申请。返回阻断数量。"""
    count = 0
    with transaction.atomic():
        pending = (
            ReleaseRequest.objects
            .select_for_update()
            .filter(status=ReleaseRequest.Status.SIGNING)
            .prefetch_related('signatures__signer')
        )
        for request_obj in pending:
            revoked = _revoked_prior_signer(request_obj)
            if revoked is not None:
                _mark_blocked(request_obj, revoked[0], revoked[1])
                count += 1
    return count
