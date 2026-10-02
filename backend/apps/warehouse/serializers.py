"""
仓库管理序列化器
"""
from rest_framework import serializers
from .models import (
    Unit, Category, Variety, Goods, StockIn, StockOut,
    Warning, ReleaseSignature, STEP_LABELS,
)


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
    
    class Meta:
        model = Goods
        fields = [
            'id', 'name', 'code', 'variety', 'variety_name',
            'category_name', 'unit_name', 'specification',
            'quantity', 'warning_threshold', 'risk_level',
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


class ReleaseCreateSerializer(serializers.Serializer):
    """放行申请提交"""
    goods = serializers.IntegerField(required=True, error_messages={'required': '请选择物资'})
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0.01,
        required=True, error_messages={'required': '请填写出库数量', 'min_value': '出库数量必须大于0'},
    )
    receiver = serializers.CharField(
        min_length=1, max_length=100, required=True,
        error_messages={'required': '请填写领用人', 'blank': '领用人不能为空'},
    )
    receiver_dept = serializers.CharField(max_length=100, required=False, allow_blank=True)
    remark = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class ReleaseSignSerializer(serializers.Serializer):
    """复核/放行签署"""
    action = serializers.ChoiceField(
        choices=['approve', 'reject'], required=True,
        error_messages={'required': '请选择签署动作', 'invalid_choice': '动作只能是 approve 或 reject'},
    )
    remark = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate_remark(self, value):
        if self.initial_data.get('action') == 'reject' and not value.strip():
            raise serializers.ValidationError('拒绝时必须填写原因')
        return value


class ReleaseSignatureSerializer(serializers.ModelSerializer):
    """单次签署记录"""
    signer_id = serializers.IntegerField(source='signer.id', read_only=True)
    signer_name = serializers.SerializerMethodField()
    step_display = serializers.CharField(read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = ReleaseSignature
        fields = [
            'step', 'step_display', 'signer_id', 'signer_name',
            'action', 'action_display', 'remark', 'goods_snapshot', 'signed_at',
        ]

    def get_signer_name(self, obj):
        if not obj.signer_id:
            return ''
        return obj.signer.real_name or obj.signer.username


class ReleaseListSerializer(serializers.ModelSerializer):
    """放行申请列表项"""
    applicant_name = serializers.SerializerMethodField()
    goods_name = serializers.CharField(read_only=True)
    goods_code = serializers.CharField(read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    current_step_display = serializers.SerializerMethodField()

    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name', 'goods_code', 'applicant_name',
            'receiver', 'receiver_dept', 'quantity',
            'risk_level', 'risk_level_display',
            'status', 'status_display', 'current_step', 'current_step_display',
            'expires_at', 'rejected_at', 'released_at',
            'created_at',
        ]

    def get_applicant_name(self, obj):
        if not obj.operator_id:
            return ''
        return obj.operator.real_name or obj.operator.username

    def get_current_step_display(self, obj):
        if obj.is_terminal:
            return ''
        return STEP_LABELS.get(obj.current_step, obj.current_step)


class ReleaseDetailSerializer(ReleaseListSerializer):
    """放行申请详情：缺少步骤、物资摘要、每次签署记录、拒绝/过期状态"""
    signatures = ReleaseSignatureSerializer(many=True, read_only=True)
    missing_steps = serializers.SerializerMethodField()
    goods_summary = serializers.SerializerMethodField()
    reject_info = serializers.SerializerMethodField()

    class Meta(ReleaseListSerializer.Meta):
        fields = ReleaseListSerializer.Meta.fields + [
            'signatures', 'missing_steps', 'goods_summary',
            'reject_step', 'reject_reason', 'reject_info',
            'terminate_reason', 'remark', 'stock_out_time',
        ]

    def get_missing_steps(self, obj):
        return obj.missing_steps()

    def get_goods_summary(self, obj):
        return obj.goods_summary()

    def get_reject_info(self, obj):
        if obj.status != 'rejected':
            return None
        return {
            'rejected_by': (obj.rejected_by.real_name or obj.rejected_by.username)
            if obj.rejected_by_id else '',
            'step': obj.reject_step,
            'step_display': STEP_LABELS.get(obj.reject_step, obj.reject_step),
            'reason': obj.reject_reason,
            'at': obj.rejected_at,
        }


class StockOutSerializer(serializers.ModelSerializer):
    """出库记录序列化器（兼容保留）"""
    goods_name = serializers.CharField(read_only=True)
    operator_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name', 'operator', 'operator_name',
            'receiver', 'receiver_dept', 'quantity', 'status', 'status_display',
            'stock_out_time', 'remark', 'created_at'
        ]

    def get_operator_name(self, obj):
        if not obj.operator_id:
            return ''
        return obj.operator.real_name or obj.operator.username


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
