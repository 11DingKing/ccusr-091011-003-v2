"""放行申请双人复核流程测试。"""
import threading
from datetime import timedelta
from decimal import Decimal

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import (
    Category, Goods, ReleaseRequest, ReleaseSignature, StockOut, Unit, Variety,
)
from apps.warehouse.services import (
    ReleaseError, block_requests_with_revoked_signers, create_release_request,
    expire_due_requests, sign_release_request,
)


class ReleaseFixture(TestCase):
    def setUp(self):
        self.applicant = User.objects.create_user("applicant", "pass1234", role="admin")
        self.reviewer = User.objects.create_user("reviewer", "pass1234", role="admin")
        self.releaser = User.objects.create_user("releaser", "pass1234", role="admin")
        self.plain_user = User.objects.create_user("plain", "pass1234", role="user")

        self.unit = Unit.objects.create(name="件", created_by=self.applicant)
        self.category = Category.objects.create(
            name="受控介质", unit=self.unit, created_by=self.applicant
        )
        self.variety = Variety.objects.create(
            name="封存硬盘", category=self.category, created_by=self.applicant
        )
        self.normal_goods = Goods.objects.create(
            variety=self.variety, name="普通记录终端", code="N-001",
            quantity=Decimal("10"), risk_level=Goods.RISK_NORMAL,
        )
        self.high_goods = Goods.objects.create(
            variety=self.variety, name="涉密存储介质", code="H-001",
            quantity=Decimal("4"), risk_level=Goods.RISK_HIGH,
        )

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def create_high_request(self, quantity=Decimal("1")):
        return create_release_request(
            applicant=self.applicant, goods=self.high_goods,
            quantity=quantity, receiver="法院执行局", receiver_dept="技术处",
            purpose="临时取证",
        )

    def create_normal_request(self, quantity=Decimal("1")):
        return create_release_request(
            applicant=self.applicant, goods=self.normal_goods,
            quantity=quantity, receiver="内勤组",
        )


class RiskLevelRoutingTest(ReleaseFixture):
    def test_normal_goods_requires_two_distinct_roles(self):
        req = self.create_normal_request()
        self.assertFalse(req.is_high_risk)
        self.assertEqual(
            req.required_roles,
            [ReleaseRequest.Role.APPLICANT, ReleaseRequest.Role.FINAL_RELEASER],
        )
        self.assertEqual(
            req.missing_steps(),
            [ReleaseRequest.Role.FINAL_RELEASER],
        )

    def test_high_risk_requires_three_distinct_roles(self):
        req = self.create_high_request()
        self.assertTrue(req.is_high_risk)
        self.assertEqual(
            req.required_roles,
            [
                ReleaseRequest.Role.APPLICANT,
                ReleaseRequest.Role.FIRST_REVIEWER,
                ReleaseRequest.Role.FINAL_RELEASER,
            ],
        )
        self.assertEqual(
            req.missing_steps(),
            [ReleaseRequest.Role.FIRST_REVIEWER, ReleaseRequest.Role.FINAL_RELEASER],
        )
        self.assertEqual(req.next_role(), ReleaseRequest.Role.FIRST_REVIEWER)


class NormalFlowTest(ReleaseFixture):
    def test_two_distinct_people_release_normal_goods(self):
        req = self.create_normal_request(quantity=Decimal("2"))
        req = sign_release_request(
            request_id=req.id, signer=self.releaser, action="approved"
        )
        self.assertEqual(req.status, ReleaseRequest.Status.RELEASED)
        self.normal_goods.refresh_from_db()
        self.assertEqual(self.normal_goods.quantity, Decimal("8"))
        stock_out = req.stock_out
        self.assertIsNotNone(stock_out)
        self.assertEqual(stock_out.status, "completed")
        self.assertEqual(stock_out.quantity, Decimal("2"))
        self.assertEqual(stock_out.operator_id, self.releaser.id)

    def test_applicant_cannot_also_be_releaser(self):
        req = self.create_normal_request()
        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.applicant, action="approved"
            )
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn("不得由同一账号兼任", ctx.exception.message)
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.SIGNING)
        self.normal_goods.refresh_from_db()
        self.assertEqual(self.normal_goods.quantity, Decimal("10"))

    def test_plain_user_cannot_apply_or_sign(self):
        with self.assertRaises(ReleaseError) as ctx:
            create_release_request(
                applicant=self.plain_user, goods=self.normal_goods,
                quantity=Decimal("1"), receiver="x",
            )
        self.assertEqual(ctx.exception.code, 403)
        req = self.create_normal_request()
        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.plain_user, action="approved"
            )
        self.assertEqual(ctx.exception.code, 403)


class HighRiskFlowTest(ReleaseFixture):
    def test_three_distinct_people_release_high_risk(self):
        req = self.create_high_request()

        req = sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        self.assertEqual(req.status, ReleaseRequest.Status.SIGNING)
        self.assertEqual(req.next_role(), ReleaseRequest.Role.FINAL_RELEASER)

        req = sign_release_request(
            request_id=req.id, signer=self.releaser, action="approved"
        )
        self.assertEqual(req.status, ReleaseRequest.Status.RELEASED)
        self.high_goods.refresh_from_db()
        self.assertEqual(self.high_goods.quantity, Decimal("3"))
        roles = list(
            ReleaseSignature.objects.filter(request=req)
            .order_by("signed_at", "id")
            .values_list("role", "signer_id", "action")
        )
        self.assertEqual([r[0] for r in roles], [
            "applicant", "first_reviewer", "final_releaser"
        ])
        signer_ids = [r[1] for r in roles]
        self.assertEqual(len(set(signer_ids)), 3)
        self.assertTrue(all(r[2] == "approved" for r in roles))

    def test_reviewer_cannot_be_applicant(self):
        req = self.create_high_request()
        with self.assertRaises(ReleaseError):
            sign_release_request(
                request_id=req.id, signer=self.applicant, action="approved"
            )

    def test_releaser_cannot_be_reviewer(self):
        req = self.create_high_request()
        sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        with self.assertRaises(ReleaseError):
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="approved"
            )
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.SIGNING)


class RejectTest(ReleaseFixture):
    def test_first_reviewer_rejection_is_terminal(self):
        req = self.create_high_request()
        req = sign_release_request(
            request_id=req.id, signer=self.reviewer,
            action="rejected", comment="手续不全",
        )
        self.assertEqual(req.status, ReleaseRequest.Status.REJECTED)
        self.assertIn("第一复核人拒绝", req.closed_reason)
        self.assertIn("手续不全", req.closed_reason)
        self.high_goods.refresh_from_db()
        self.assertEqual(self.high_goods.quantity, Decimal("4"))
        self.assertEqual(StockOut.objects.count(), 0)

        # 终态后任何签署都被拒绝，状态不改变
        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.releaser, action="approved"
            )
        self.assertEqual(ctx.exception.code, 409)
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.REJECTED)

    def test_final_releaser_rejection(self):
        req = self.create_high_request()
        sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        req = sign_release_request(
            request_id=req.id, signer=self.releaser, action="rejected"
        )
        self.assertEqual(req.status, ReleaseRequest.Status.REJECTED)


class ExpiryTest(ReleaseFixture):
    def test_expired_request_blocks_later_signature(self):
        req = create_release_request(
            applicant=self.applicant, goods=self.high_goods,
            quantity=Decimal("1"), receiver="法院执行局",
        )
        # 手动置为已过期
        ReleaseRequest.objects.filter(pk=req.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="approved"
            )
        self.assertEqual(ctx.exception.code, 409)
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.EXPIRED)
        self.assertTrue(req.is_terminal)

    def test_expire_due_requests_batch_job(self):
        req = self.create_high_request()
        ReleaseRequest.objects.filter(pk=req.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        count = expire_due_requests()
        self.assertEqual(count, 1)
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.EXPIRED)
        # 幂等：再跑一次不重复计数
        self.assertEqual(expire_due_requests(), 0)


class PermissionRevocationTest(ReleaseFixture):
    def test_deactivating_reviewer_blocks_inflight_release(self):
        req = self.create_high_request()
        sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        # 第一复核人签署后被停用
        User.objects.filter(pk=self.reviewer.pk).update(is_active=False)

        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.releaser, action="approved"
            )
        self.assertEqual(ctx.exception.code, 409)
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.BLOCKED)
        self.assertIn("第一复核人", req.closed_reason)
        self.high_goods.refresh_from_db()
        self.assertEqual(self.high_goods.quantity, Decimal("4"))

    def test_demoting_applicant_blocks_release(self):
        req = self.create_high_request()
        User.objects.filter(pk=self.applicant.pk).update(role="user")
        with self.assertRaises(ReleaseError):
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="approved"
            )
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.BLOCKED)

    def test_blocked_is_terminal(self):
        req = self.create_high_request()
        User.objects.filter(pk=self.applicant.pk).update(is_active=False)
        with self.assertRaises(ReleaseError):
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="approved"
            )
        # 即使恢复权限，被阻断的申请也不能继续
        User.objects.filter(pk=self.applicant.pk).update(is_active=True)
        with self.assertRaises(ReleaseError):
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="approved"
            )

    def test_batch_block_job_catches_revocation(self):
        req = self.create_high_request()
        User.objects.filter(pk=self.applicant.pk).update(is_active=False)
        count = block_requests_with_revoked_signers()
        self.assertEqual(count, 1)
        req.refresh_from_db()
        self.assertEqual(req.status, ReleaseRequest.Status.BLOCKED)


class DuplicateSignTest(ReleaseFixture):
    def test_duplicate_signature_request_is_idempotent_conflict(self):
        req = self.create_high_request()
        sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        # 同一复核人重复签署
        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="approved"
            )
        self.assertEqual(ctx.exception.code, 409)
        # 换动作也不能覆盖已有签署
        with self.assertRaises(ReleaseError):
            sign_release_request(
                request_id=req.id, signer=self.reviewer, action="rejected"
            )
        self.assertEqual(
            ReleaseSignature.objects.filter(request=req, role="first_reviewer").count(),
            1,
        )


class InsufficientStockTest(ReleaseFixture):
    def test_release_deducts_atomically_and_rejects_shortage(self):
        # 申请 4 件，申请后库存被别处占用到 2 件
        req = create_release_request(
            applicant=self.applicant, goods=self.high_goods,
            quantity=Decimal("4"), receiver="法院执行局",
        )
        Goods.objects.filter(pk=self.high_goods.pk).update(quantity=Decimal("2"))
        sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        with self.assertRaises(ReleaseError) as ctx:
            sign_release_request(
                request_id=req.id, signer=self.releaser, action="approved"
            )
        self.assertEqual(ctx.exception.code, 409)
        self.assertIn("库存不足", ctx.exception.message)
        self.high_goods.refresh_from_db()
        self.assertEqual(self.high_goods.quantity, Decimal("2"))
        self.assertEqual(StockOut.objects.count(), 0)


class ConcurrentSignTest(TransactionTestCase):
    """两个账号并发抢同一个签署步骤，只能有一个成功、一个确定冲突。"""

    def setUp(self):
        self.applicant = User.objects.create_user("c-applicant", "pass1234", role="admin")
        self.reviewer_a = User.objects.create_user("c-rev-a", "pass1234", role="admin")
        self.reviewer_b = User.objects.create_user("c-rev-b", "pass1234", role="admin")
        unit = Unit.objects.create(name="件", created_by=self.applicant)
        category = Category.objects.create(name="介质", unit=unit, created_by=self.applicant)
        variety = Variety.objects.create(name="硬盘", category=category, created_by=self.applicant)
        self.goods = Goods.objects.create(
            variety=variety, name="涉密盘", code="C-001",
            quantity=Decimal("5"), risk_level=Goods.RISK_HIGH,
        )
        self.req = create_release_request(
            applicant=self.applicant, goods=self.goods,
            quantity=Decimal("1"), receiver="法院",
        )

    def test_parallel_signatures_for_same_step_resolve_to_one(self):
        results = []
        barrier = threading.Barrier(2)

        def sign(user):
            try:
                barrier.wait(timeout=10)
                sign_release_request(
                    request_id=self.req.id, signer=user, action="approved",
                    # 两名复核人都看到“第一复核”待签并同时点击
                    role="first_reviewer",
                )
                results.append((200, user.username))
            except ReleaseError as exc:
                results.append((exc.code, user.username))
            except Exception as exc:  # pragma: no cover - 暴露非预期异常
                results.append((500, repr(exc)))
            finally:
                connections.close_all()

        t1 = threading.Thread(target=sign, args=(self.reviewer_a,))
        t2 = threading.Thread(target=sign, args=(self.reviewer_b,))
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        self.assertEqual(len(results), 2)
        codes = sorted(r[0] for r in results)
        self.assertEqual(codes, [200, 409])
        # 步骤唯一：只有一条第一复核签署
        self.assertEqual(
            ReleaseSignature.objects.filter(
                request_id=self.req.id, role="first_reviewer"
            ).count(),
            1,
        )
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, ReleaseRequest.Status.SIGNING)
        self.assertEqual(self.req.next_role(), ReleaseRequest.Role.FINAL_RELEASER)
        # 库存未被扣减（还差最终放行）
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("5"))


class ReleaseAPITest(ReleaseFixture):
    def test_full_high_risk_flow_over_api(self):
        client = self.client_for(self.applicant)
        created = client.post("/api/release-requests/", {
            "goods": self.high_goods.id,
            "quantity": "2",
            "receiver": "法院执行局",
            "receiver_dept": "技术处",
            "purpose": "临时取证",
        }, format="json")
        self.assertEqual(created.status_code, 200, created.content)
        data = created.json()["data"]
        self.assertEqual(data["status"], "signing")
        self.assertTrue(data["requires_dual_review"])
        self.assertEqual(
            [s["role"] for s in data["missing_steps"]],
            ["first_reviewer", "final_releaser"],
        )
        self.assertEqual(data["next_step"]["role"], "first_reviewer")
        # 每次签署看到的物资摘要
        self.assertEqual(data["goods_summary"]["code"], "H-001")
        self.assertEqual(data["goods_summary"]["risk_level_display"], "高风险物资")
        self.assertEqual(len(data["steps"]), 3)
        self.assertEqual(data["steps"][0]["status"], "approved")
        self.assertEqual(data["steps"][1]["status"], "pending")
        self.assertTrue(data["can_sign"] is False)  # 申请人不能再签
        rid = data["id"]

        # 复核人看到的详情：can_sign=True，缺失步骤清楚
        rev_client = self.client_for(self.reviewer)
        detail = rev_client.get(f"/api/release-requests/{rid}/").json()["data"]
        self.assertTrue(detail["can_sign"])
        signed = rev_client.post(
            f"/api/release-requests/{rid}/sign/",
            {"action": "approved"}, format="json",
        )
        self.assertEqual(signed.status_code, 200)
        signed_data = signed.json()["data"]
        self.assertEqual(signed_data["next_step"]["role"], "final_releaser")

        # 同一账号再次签署 → 409
        dup = rev_client.post(
            f"/api/release-requests/{rid}/sign/",
            {"action": "approved"}, format="json",
        )
        self.assertEqual(dup.status_code, 409)

        # 最终放行人拒绝 → 状态 rejected 清晰可见
        rel_client = self.client_for(self.releaser)
        rejected = rel_client.post(
            f"/api/release-requests/{rid}/sign/",
            {"action": "rejected", "comment": "待补批文"}, format="json",
        )
        self.assertEqual(rejected.status_code, 200)
        rdata = rejected.json()["data"]
        self.assertEqual(rdata["status"], "rejected")
        self.assertEqual(rdata["status_display"], "已拒绝")
        self.assertEqual(rdata["missing_steps"], [])
        self.assertFalse(rdata["can_sign"])

    def test_api_rejects_same_account_as_reviewer(self):
        client = self.client_for(self.applicant)
        created = client.post("/api/release-requests/", {
            "goods": self.high_goods.id,
            "quantity": "1",
            "receiver": "法院执行局",
        }, format="json")
        rid = created.json()["data"]["id"]
        resp = client.post(
            f"/api/release-requests/{rid}/sign/",
            {"action": "approved"}, format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("不得由同一账号兼任", resp.json()["message"])

    def test_api_requires_authentication(self):
        resp = APIClient().get("/api/release-requests/")
        self.assertEqual(resp.status_code, 401)

    def test_api_create_validates_inputs(self):
        client = self.client_for(self.applicant)
        missing = client.post("/api/release-requests/", {}, format="json")
        self.assertEqual(missing.status_code, 400)
        bad_qty = client.post("/api/release-requests/", {
            "goods": self.normal_goods.id,
            "quantity": "-1",
            "receiver": "内勤组",
        }, format="json")
        self.assertEqual(bad_qty.status_code, 400)
        unknown_goods = client.post("/api/release-requests/", {
            "goods": 999999,
            "quantity": "1",
            "receiver": "内勤组",
        }, format="json")
        self.assertEqual(unknown_goods.status_code, 400)
        overstock = client.post("/api/release-requests/", {
            "goods": self.normal_goods.id,
            "quantity": "999",
            "receiver": "内勤组",
        }, format="json")
        self.assertEqual(overstock.status_code, 400)
        self.assertIn("库存不足", overstock.json()["message"])

    def test_api_list_filters_and_paginates(self):
        self.create_high_request()
        self.create_normal_request()
        client = self.client_for(self.applicant)
        resp = client.get("/api/release-requests/?risk_level=high").json()["data"]
        self.assertEqual(resp["total"], 1)
        self.assertEqual(resp["list"][0]["risk_level"], "high")
        resp2 = client.get("/api/release-requests/?status=signing").json()["data"]
        self.assertEqual(resp2["total"], 2)

    def test_api_permission_revocation_shows_blocked_status(self):
        req = self.create_high_request()
        sign_release_request(
            request_id=req.id, signer=self.reviewer, action="approved"
        )
        User.objects.filter(pk=self.reviewer.pk).update(is_active=False)
        client = self.client_for(self.releaser)
        resp = client.post(
            f"/api/release-requests/{req.id}/sign/",
            {"action": "approved"}, format="json",
        )
        self.assertEqual(resp.status_code, 409)
        detail = client.get(f"/api/release-requests/{req.id}/").json()["data"]
        self.assertEqual(detail["status"], "blocked")
        self.assertEqual(detail["status_display"], "权限阻止")
        self.assertIn("权限已撤销", detail["closed_reason"])
