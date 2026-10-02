"""
仓库管理序列化器
"""
from rest_framework import serializers
from .models import Unit, Category, Variety, Goods, StockIn, StockOut, Warning, Approval
from .models import ReleaseRequest, ReleaseSignature


class UnitSerializer(serializers.ModelSerializer):
    """单位序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UnitCreateSerializer(serializers.Serializer):
    """单位创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=5, required=True, error_messages={
        'required': '请输入单位名称',
        'blank': '单位名称不能为空',
        'min_length': '单位名称至少1个字',
        'max_length': '单位名称最多5个字',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Unit.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('单位名称已存在')
        else:
            if Unit.objects.filter(name=value).exists():
                raise serializers.ValidationError('单位名称已存在')
        return value


class CategorySerializer(serializers.ModelSerializer):
    """品类序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'unit', 'unit_name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CategoryCreateSerializer(serializers.Serializer):
    """品类创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=10, required=True, error_messages={
        'required': '请输入品类名称',
        'blank': '品类名称不能为空',
        'min_length': '品类名称至少1个字',
        'max_length': '品类名称最多10个字',
    })
    unit = serializers.IntegerField(required=True, error_messages={
        'required': '请选择单位',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Category.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('品类名称已存在')
        else:
            if Category.objects.filter(name=value).exists():
                raise serializers.ValidationError('品类名称已存在')
        return value
    
    def validate_unit(self, value):
        if not Unit.objects.filter(pk=value).exists():
            raise serializers.ValidationError('单位不存在')
        return value


class VarietySerializer(serializers.ModelSerializer):
    """品种序列化器"""
    is_in_stock = serializers.BooleanField(read_only=True)
    unit_name = serializers.CharField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Variety
        fields = [
            'id', 'name', 'category', 'category_name', 'unit_name',
            'is_in_stock', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class VarietyCreateSerializer(serializers.Serializer):
    """品种创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=20, required=True, error_messages={
        'required': '请输入品种名称',
        'blank': '品种名称不能为空',
        'min_length': '品种名称至少1个字',
        'max_length': '品种名称最多20个字',
    })
    category = serializers.IntegerField(required=True, error_messages={
        'required': '请选择品类',
    })
    
    def validate_category(self, value):
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value
    
    def validate(self, data):
        instance = self.context.get('instance')
        name = data['name']
        category_id = data['category']
        
        if instance:
            if Variety.objects.filter(name=name, category_id=category_id).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        else:
            if Variety.objects.filter(name=name, category_id=category_id).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        return data


class GoodsSerializer(serializers.ModelSerializer):
    """货物序列化器"""
    variety_name = serializers.CharField(source='variety.name', read_only=True)
    category_name = serializers.CharField(source='variety.category.name', read_only=True)
    unit_name = serializers.CharField(source='variety.category.unit.name', read_only=True)
    is_warning = serializers.BooleanField(read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)

    class Meta:
        model = Goods
        fields = [
            'id', 'name', 'code', 'variety', 'variety_name',
            'category_name', 'unit_name', 'specification',
            'quantity', 'warning_threshold', 'risk_level', 'risk_level_display',
            'location', 'remark', 'is_active', 'is_warning',
            'created_at', 'updated_at'
        ]


class StockInSerializer(serializers.ModelSerializer):
    """入库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    
    class Meta:
        model = StockIn
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'quantity', 'batch_no', 'supplier', 'stock_in_time', 'remark'
        ]


class StockOutSerializer(serializers.ModelSerializer):
    """出库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'receiver', 'receiver_dept', 'quantity', 'status', 'status_display',
            'stock_out_time', 'remark', 'created_at'
        ]


class WarningSerializer(serializers.ModelSerializer):
    """预警记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    
    class Meta:
        model = Warning
        fields = [
            'id', 'goods', 'goods_name', 'type', 'type_display',
            'message', 'is_read', 'created_at'
        ]


class ApprovalSerializer(serializers.ModelSerializer):
    """审批记录序列化器"""
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Approval
        fields = [
            'id', 'stock_out', 'approver', 'approver_name',
            'status', 'status_display', 'remark', 'created_at', 'updated_at'
        ]


# ==================== 放行申请（双人复核） ====================

class ReleaseRequestCreateSerializer(serializers.Serializer):
    """放行申请创建序列化器"""
    goods = serializers.IntegerField(required=True, error_messages={'required': '请选择物资'})
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=True,
        error_messages={'required': '请填写放行数量', 'invalid': '放行数量格式不正确'}
    )
    receiver = serializers.CharField(required=True, max_length=100, error_messages={
        'required': '请填写领用人', 'blank': '领用人不能为空'
    })
    receiver_dept = serializers.CharField(max_length=100, required=False, allow_blank=True)
    purpose = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_goods(self, value):
        if not Goods.objects.filter(pk=value).exists():
            raise serializers.ValidationError('物资不存在')
        return value

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError('放行数量必须大于 0')
        return value

    def validate_receiver(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('领用人不能为空')
        return value


class ReleaseSignSerializer(serializers.Serializer):
    """放行签署序列化器"""
    action = serializers.ChoiceField(
        choices=[('approved', '同意'), ('rejected', '拒绝')],
        required=True, error_messages={'required': '请选择签署动作', 'invalid_choice': '签署动作无效'}
    )
    # 本次要签署的步骤，取自详情接口 next_step；用于乐观并发控制，
    # 防止并发点击把“第一复核”错签成“最终放行”。不传则由服务端取当前下一步。
    role = serializers.ChoiceField(
        choices=[
            ('first_reviewer', '第一复核人'),
            ('final_releaser', '最终放行人'),
        ],
        required=False,
    )
    comment = serializers.CharField(max_length=200, required=False, allow_blank=True)


class ReleaseSignatureSerializer(serializers.ModelSerializer):
    """签署记录序列化器"""
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    signer_id = serializers.IntegerField(source='signer.id', read_only=True)
    signer_name = serializers.SerializerMethodField()

    class Meta:
        model = ReleaseSignature
        fields = [
            'role', 'role_display', 'signer_id', 'signer_name',
            'action', 'action_display', 'comment', 'signed_at'
        ]

    def get_signer_name(self, obj):
        return obj.signer.real_name or obj.signer.username


class ReleaseRequestSerializer(serializers.ModelSerializer):
    """放行申请详情序列化器：展示物资摘要、签署步骤、缺失步骤与当前状态。"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)
    requires_dual_review = serializers.BooleanField(source='is_high_risk', read_only=True)
    is_expired = serializers.SerializerMethodField()
    applicant_name = serializers.SerializerMethodField()
    goods_summary = serializers.SerializerMethodField()
    steps = serializers.SerializerMethodField()
    missing_steps = serializers.SerializerMethodField()
    next_step = serializers.SerializerMethodField()
    signatures = ReleaseSignatureSerializer(many=True, read_only=True)
    can_sign = serializers.SerializerMethodField()
    stock_out_id = serializers.IntegerField(source='stock_out.id', read_only=True)

    class Meta:
        model = ReleaseRequest
        fields = [
            'id', 'status', 'status_display', 'closed_reason',
            'risk_level', 'risk_level_display', 'requires_dual_review',
            'goods', 'goods_summary', 'quantity',
            'receiver', 'receiver_dept', 'purpose',
            'applicant', 'applicant_name',
            'steps', 'missing_steps', 'next_step', 'signatures',
            'can_sign', 'is_expired',
            'stock_out_id', 'expires_at', 'created_at', 'updated_at',
        ]

    def _current_user(self):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return user
        return None

    def get_is_expired(self, obj):
        return obj.is_expired

    def get_applicant_name(self, obj):
        return obj.applicant.real_name or obj.applicant.username

    def get_goods_summary(self, obj):
        """每次签署时看到的物资摘要（取物资实时信息）。"""
        goods = obj.goods
        variety = getattr(goods, 'variety', None)
        category = getattr(variety, 'category', None)
        unit = getattr(category, 'unit', None)
        return {
            'id': goods.id,
            'name': goods.name,
            'code': goods.code,
            'specification': goods.specification,
            'variety_name': variety.name if variety else '',
            'category_name': category.name if category else '',
            'unit_name': unit.name if unit else '',
            'risk_level': goods.risk_level,
            'risk_level_display': goods.get_risk_level_display(),
            'stock_quantity': str(goods.quantity),
            'location': goods.location,
        }

    def get_steps(self, obj):
        """按制度顺序展示每个签署步骤及其完成情况。"""
        by_role = {s.role: s for s in obj.signatures.all()}
        steps = []
        for index, role in enumerate(obj.required_roles, start=1):
            signature = by_role.get(role)
            if signature is None:
                step_status = 'pending'
                signer_info = None
                action = None
                comment = ''
                signed_at = None
            else:
                action = signature.action
                step_status = 'rejected' if action == 'rejected' else 'approved'
                signer_info = {
                    'id': signature.signer_id,
                    'name': signature.signer.real_name or signature.signer.username,
                }
                comment = signature.comment
                signed_at = signature.signed_at
            steps.append({
                'order': index,
                'role': role,
                'role_display': signature.get_role_display() if signature else
                    dict(ReleaseRequest.Role.choices).get(role, role),
                'status': step_status,
                'signer': signer_info,
                'action': action,
                'comment': comment,
                'signed_at': signed_at,
            })
        return steps

    def get_missing_steps(self, obj):
        """还缺少哪些签署步骤（角色 + 中文名称，有序）。"""
        return [
            {'role': role, 'role_display': dict(ReleaseRequest.Role.choices)[role]}
            for role in obj.missing_steps()
        ]

    def get_next_step(self, obj):
        """当前等待完成的那一步；无则为 None。"""
        role = obj.next_role()
        if role is None:
            return None
        return {'role': role, 'role_display': dict(ReleaseRequest.Role.choices)[role]}

    def get_can_sign(self, obj):
        """当前请求用户能否执行下一步签署（供前端按钮置灰判断）。"""
        user = self._current_user()
        if user is None or obj.is_terminal or obj.next_role() is None:
            return False
        from .services import has_release_authority
        if not has_release_authority(user):
            return False
        signed_ids = {s.signer_id for s in obj.signatures.all()}
        return user.id not in signed_ids

