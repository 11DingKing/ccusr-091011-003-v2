"""
受控物资放行申请视图（按风险等级的双人复核流程）。
"""
import logging

from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import success_response, error_response
from .models import Goods, ReleaseRequest
from .serializers import (
    ReleaseRequestCreateSerializer,
    ReleaseRequestSerializer,
    ReleaseSignSerializer,
)
from .services import (
    ReleaseError,
    create_release_request,
    settle_request,
    sign_release_request,
)

logger = logging.getLogger('apps')


def _first_error(serializer):
    errors = serializer.errors
    first_error = list(errors.values())[0]
    if isinstance(first_error, list):
        first_error = first_error[0]
    return str(first_error)


class ReleaseRequestListCreateView(APIView):
    """放行申请列表 / 创建放行申请。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = (
            ReleaseRequest.objects
            .select_related('goods__variety__category__unit', 'applicant')
            .prefetch_related('signatures__signer')
            .all()
            .order_by('-created_at')
        )

        status_param = request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)
        risk = request.query_params.get('risk_level')
        if risk:
            queryset = queryset.filter(risk_level=risk)
        goods_id = request.query_params.get('goods')
        if goods_id:
            queryset = queryset.filter(goods_id=goods_id)
        keyword = request.query_params.get('search')
        if keyword:
            queryset = queryset.filter(
                Q(goods__name__icontains=keyword)
                | Q(goods__code__icontains=keyword)
                | Q(receiver__icontains=keyword)
            )

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = max(int(request.query_params.get('page_size', 10)), 1)
        except (TypeError, ValueError):
            page, page_size = 1, 10
        total = queryset.count()
        items = queryset[(page - 1) * page_size:page * page_size]
        serializer = ReleaseRequestSerializer(items, many=True, context={'request': request})

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size,
        })

    def post(self, request):
        serializer = ReleaseRequestCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        data = serializer.validated_data
        try:
            goods = Goods.objects.get(pk=data['goods'])
        except Goods.DoesNotExist:
            return error_response(message='物资不存在', code=404)

        try:
            release = create_release_request(
                applicant=request.user,
                goods=goods,
                quantity=data['quantity'],
                receiver=data['receiver'],
                receiver_dept=data.get('receiver_dept', ''),
                purpose=data.get('purpose', ''),
            )
        except ReleaseError as exc:
            return error_response(message=exc.message, code=exc.code)

        logger.info(
            '用户 %s 提交放行申请 #%s（物资 %s，数量 %s，风险等级 %s）',
            request.user.username, release.id, goods.code,
            release.quantity, release.get_risk_level_display(),
        )

        release = settle_request(release.id)
        return success_response(
            data=ReleaseRequestSerializer(release, context={'request': request}).data,
            message='放行申请已提交，等待复核放行',
        )


class ReleaseRequestDetailView(APIView):
    """放行申请详情：清楚展示还缺少哪一步、每次签署的物资摘要与签署记录。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            release = settle_request(pk)
        except ReleaseError as exc:
            return error_response(message=exc.message, code=exc.code)
        return success_response(
            data=ReleaseRequestSerializer(release, context={'request': request}).data
        )


class ReleaseSignView(APIView):
    """对放行申请执行下一步签署（同意 / 拒绝）。

    申请人、第一复核人、最终放行人不得为同一账号；重复签署与并发签署
    只能产生一个确定结果（唯一约束 + 行锁 + 状态机保证）。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = ReleaseSignSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        try:
            release = sign_release_request(
                request_id=pk,
                signer=request.user,
                action=serializer.validated_data['action'],
                comment=serializer.validated_data.get('comment', ''),
                role=serializer.validated_data.get('role'),
            )
        except ReleaseError as exc:
            return error_response(message=exc.message, code=exc.code)

        action_label = serializer.validated_data['action']
        logger.info(
            '用户 %s 对放行申请 #%s 执行签署：%s，结果状态 %s',
            request.user.username, pk, action_label, release.status,
        )

        release = settle_request(release.id)
        return success_response(
            data=ReleaseRequestSerializer(release, context={'request': request}).data,
            message='已拒绝放行' if release.status == ReleaseRequest.Status.REJECTED
            else '签署成功' if release.status != ReleaseRequest.Status.RELEASED
            else '全部签署完成，已放行',
        )
