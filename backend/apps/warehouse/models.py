"""
库房管理模型
"""
from datetime import timedelta

from django.db import models
from django.utils import timezone
from apps.authentication.models import User


# 放行申请有效期（自创建起）
RELEASE_EXPIRY = timedelta(hours=24)

RISK_LEVELS = [
    ('low', '低风险'),
    ('high', '高风险'),
]

# 放行步骤编码，同时用于签署阶段与状态机
STEP_APPLY = 'apply'
STEP_REVIEW = 'review'
STEP_RELEASE = 'release'
RELEASE_STEPS = [STEP_APPLY, STEP_REVIEW, STEP_RELEASE]

STEP_LABELS = {
    STEP_APPLY: '申请',
    STEP_REVIEW: '第一复核',
    STEP_RELEASE: '最终放行',
}

# 高风险物资必须两名不同人员复核：申请、第一复核、最终放行三岗分离
HIGH_RISK_REQUIRED_STEPS = [STEP_APPLY, STEP_REVIEW, STEP_RELEASE]
LOW_RISK_REQUIRED_STEPS = [STEP_APPLY, STEP_RELEASE]


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    risk_level = models.CharField(
        '风险等级', max_length=10, choices=RISK_LEVELS, default='low'
    )
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)
    
    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class StockOut(models.Model):
    """出库放行申请模型

    状态机（所有迁移均在 ``sign``/``reject`` 的行级事务内完成，保证
    重复签署与并发签署只能产生一个确定结果）::

        pending_review / pending_release
            -- 任一在岗签署人拒绝 --> rejected（终态）
            -- 超过 RELEASE_EXPIRY --> expired（终态，惰性判定）
        pending_release（低风险）/ pending_release（高风险复核通过）
            -- 最终放行人通过 --> released（终态，扣减库存）
    """
    STATUS_CHOICES = [
        ('pending', '待审批'),            # 兼容历史数据
        ('pending_review', '待第一复核'),
        ('pending_release', '待最终放行'),
        ('rejected', '已拒绝'),
        ('expired', '已过期'),
        ('terminated', '已终止'),         # 签署链条中有人权限被撤销
        ('released', '已放行'),
        ('approved', '已通过'),           # 兼容历史数据
        ('completed', '已完成'),          # 兼容历史数据
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_applications', verbose_name='申请人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    risk_level = models.CharField(
        '风险等级', max_length=10, choices=RISK_LEVELS, default='low'
    )
    # 物资摘要快照：签署人看到的物资信息以申请提交时为准
    goods_name = models.CharField('货物名称快照', max_length=200, blank=True, default='')
    goods_code = models.CharField('货物编码快照', max_length=50, blank=True, default='')
    status = models.CharField(
        '状态', max_length=20, choices=STATUS_CHOICES, default='pending_review'
    )
    current_step = models.CharField(
        '当前步骤', max_length=20, choices=[(s, STEP_LABELS[s]) for s in RELEASE_STEPS],
        default=STEP_APPLY
    )
    expires_at = models.DateTimeField('有效期至', null=True, blank=True)
    rejected_at = models.DateTimeField('拒绝时间', null=True, blank=True)
    reject_step = models.CharField('拒绝步骤', max_length=20, blank=True, default='')
    rejected_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='rejected_stock_outs', verbose_name='拒绝人'
    )
    reject_reason = models.TextField('拒绝原因', blank=True, default='')
    terminate_reason = models.CharField('终止原因', max_length=200, blank=True, default='')
    released_at = models.DateTimeField('放行时间', null=True, blank=True)
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库放行申请'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"

    # ---------- 流程派生属性 ----------

    @property
    def required_steps(self):
        """按风险等级要求的签署步骤（有序）"""
        if self.risk_level == 'high':
            return list(HIGH_RISK_REQUIRED_STEPS)
        return list(LOW_RISK_REQUIRED_STEPS)

    @property
    def is_high_risk(self):
        return self.risk_level == 'high'

    @property
    def is_terminal(self):
        return self.status in (
            'rejected', 'expired', 'terminated',
            'released', 'approved', 'completed',
        )

    @property
    def is_expired(self):
        """过期为惰性判定：读取时超过有效期且仍未终结即视为过期"""
        if self.is_terminal or self.expires_at is None:
            return False
        return timezone.now() >= self.expires_at

    def next_step(self, step):
        steps = self.required_steps
        try:
            idx = steps.index(step)
        except ValueError:
            return None
        return steps[idx + 1] if idx + 1 < len(steps) else None

    def missing_steps(self):
        """返回尚未完成的步骤（含状态、责任人信息），供接口直观展示"""
        signatures = {s.step: s for s in self.signatures.all()}
        result = []
        for step in self.required_steps:
            sig = signatures.get(step)
            item = {
                'step': step,
                'step_display': STEP_LABELS[step],
                'done': sig is not None and sig.action == 'approve',
            }
            if sig is not None:
                item['signer'] = sig.signer_name
                item['signed_at'] = sig.signed_at
                item['action'] = sig.action
            result.append(item)
        return result

    def next_missing_step(self):
        for item in self.missing_steps():
            if not item['done']:
                return item['step']
        return None

    def goods_summary(self):
        """每次签署时看到的物资摘要（创建时已按快照冗余保存名称/编码）"""
        goods = self.goods
        return {
            'goods': self.goods_id,
            'goods_name': self.goods_name or goods.name,
            'goods_code': self.goods_code or goods.code,
            'specification': goods.specification,
            'category_name': getattr(getattr(goods.variety, 'category', None), 'name', ''),
            'unit_name': goods.variety.unit_name if goods.variety_id else '',
            'location': goods.location,
            'quantity': str(self.quantity),
            'stock_quantity': str(goods.quantity),
            'risk_level': self.risk_level,
            'risk_level_display': self.get_risk_level_display(),
        }


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class ReleaseSignature(models.Model):
    """放行签署记录：申请、第一复核、最终放行每个步骤恰好一条"""
    ACTION_CHOICES = [
        ('approve', '通过'),
        ('reject', '拒绝'),
    ]

    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='signatures', verbose_name='放行申请'
    )
    step = models.CharField('签署步骤', max_length=20, choices=[(s, STEP_LABELS[s]) for s in RELEASE_STEPS])
    signer = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='release_signatures', verbose_name='签署人'
    )
    action = models.CharField('签署动作', max_length=10, choices=ACTION_CHOICES)
    # 签署时刻签署人看到的物资摘要（JSON）
    goods_snapshot = models.JSONField('物资摘要', default=dict, blank=True)
    remark = models.TextField('签署意见', blank=True, default='')
    signed_at = models.DateTimeField('签署时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_release_signature'
        verbose_name = '放行签署记录'
        verbose_name_plural = verbose_name
        ordering = ['signed_at']
        constraints = [
            models.UniqueConstraint(
                fields=['stock_out', 'step'],
                name='uniq_signature_per_stockout_step',
            ),
        ]

    def __str__(self):
        return f"{self.stock_out_id}:{self.step}:{self.action}"

    @property
    def signer_name(self):
        return self.signer.real_name or self.signer.username if self.signer_id else ''

    @property
    def step_display(self):
        return STEP_LABELS.get(self.step, self.step)
