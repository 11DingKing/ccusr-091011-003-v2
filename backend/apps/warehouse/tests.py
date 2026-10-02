import threading
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from .models import (
    Category, Goods, ReleaseSignature, StockIn, StockOut, Unit, Variety, Warning,
    STEP_REVIEW, STEP_RELEASE,
)


class WarehouseFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("warehouse-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(name="受控器材", unit=self.unit, created_by=self.user)
        self.variety = Variety.objects.create(name="记录终端", category=self.category, created_by=self.user)
        self.goods = Goods.objects.create(
            variety=self.variety,
            name="执法记录终端",
            code="DEV-001",
            quantity=Decimal("12"),
            warning_threshold=Decimal("5"),
        )

    def api_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def make_admins(self, count):
        users = []
        for i in range(count):
            users.append(User.objects.create_user(f"admin-{i}", "pass123456", role="admin"))
        return users


class WarehouseModelTest(WarehouseFixture):
    def test_relationship_flags(self):
        self.assertTrue(self.unit.is_linked)
        self.assertTrue(self.category.is_linked)
        self.assertTrue(self.variety.is_in_stock)
        self.assertFalse(self.goods.is_warning)

    def test_unique_unit_name(self):
        with self.assertRaises(IntegrityError):
            Unit.objects.create(name="件", created_by=self.user)

    def test_stock_records(self):
        inbound = StockIn.objects.create(goods=self.goods, operator=self.user, quantity=Decimal("3"))
        outbound = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="保管员", quantity=Decimal("2")
        )
        self.assertEqual(inbound.goods_id, self.goods.id)
        self.assertEqual(outbound.status, "pending_review")

    def test_warning_record(self):
        warning = Warning.objects.create(goods=self.goods, type="low_stock", message="库存不足")
        self.assertFalse(warning.is_read)
        self.assertIn("执法记录终端", str(warning))

    def test_signature_unique_per_step(self):
        stock_out = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="x",
            quantity=Decimal("1"), risk_level="high",
        )
        other = User.objects.create_user("other", "pass123456", role="admin")
        ReleaseSignature.objects.create(
            stock_out=stock_out, step=STEP_REVIEW, signer=other, action="approve"
        )
        third = User.objects.create_user("third", "pass123456", role="admin")
        with self.assertRaises(IntegrityError):
            ReleaseSignature.objects.create(
                stock_out=stock_out, step=STEP_REVIEW, signer=third, action="approve"
            )


class WarehouseAPITest(WarehouseFixture):
    def test_list_units(self):
        response = self.client.get("/api/units/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)

    def test_create_unit_and_reject_duplicate(self):
        created = self.client.post("/api/units/", {"name": "箱"}, format="json")
        duplicate = self.client.post("/api/units/", {"name": "箱"}, format="json")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_update_linked_unit(self):
        response = self.client.put(f"/api/units/{self.unit.id}/", {"name": "台"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.name, "台")

    def test_refuse_delete_linked_unit(self):
        response = self.client.delete(f"/api/units/{self.unit.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Unit.objects.filter(pk=self.unit.id).exists())

    def test_create_category_validates_unit(self):
        ok = self.client.post("/api/categories/", {"name": "封存介质", "unit": self.unit.id}, format="json")
        bad = self.client.post("/api/categories/", {"name": "无效分类", "unit": 99999}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 400)

    def test_create_variety_and_duplicate_boundary(self):
        ok = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        duplicate = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_requires_authentication(self):
        anonymous = APIClient().get("/api/units/")
        self.assertEqual(anonymous.status_code, 401)


class ReleaseWorkflowTest(WarehouseFixture):
    """双人复核工作流：风险分级、三岗分离、失权阻断、终态与幂等。"""

    def _create(self, client, risk_goods=None, **overrides):
        goods = risk_goods or self.goods
        payload = {
            "goods": goods.id,
            "quantity": "2",
            "receiver": "法院值班员",
            "receiver_dept": "执行局",
        }
        payload.update(overrides)
        return client.post("/api/releases/", payload, format="json")

    def test_low_risk_skips_first_review(self):
        # self.goods 默认 low：申请后直接待最终放行，缺少步骤不含第一复核
        response = self._create(self.client)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["risk_level"], "low")
        self.assertEqual(data["status"], "pending_release")
        steps = [(s["step"], s["done"]) for s in data["missing_steps"]]
        self.assertEqual(steps, [("apply", True), ("release", False)])

    def test_high_risk_requires_three_distinct_accounts(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant, reviewer, releaser = self.make_admins(3)

        created = self._create(self.api_for(applicant))
        self.assertEqual(created.status_code, 200)
        rid = created.json()["data"]["id"]
        self.assertEqual(created.json()["data"]["status"], "pending_review")

        # 申请人不能兼任第一复核人
        own = self.api_for(applicant).post(
            f"/api/releases/{rid}/review/", {"action": "approve"}, format="json"
        )
        self.assertEqual(own.status_code, 409)

        # 第一复核人不能再兼任最终放行人（稍后验证）
        reviewed = self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/", {"action": "approve"}, format="json"
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["data"]["status"], "pending_release")

        same_person = self.api_for(reviewer).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(same_person.status_code, 409)
        self.assertIn("其他岗位", same_person.json()["message"])

        # 第三人放行：库存扣减，状态 released
        released = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(released.status_code, 200, released.content)
        data = released.json()["data"]
        self.assertEqual(data["status"], "released")
        self.assertTrue(all(s["done"] for s in data["missing_steps"]))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10"))

    def test_cannot_skip_review_step_on_high_risk(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant, releaser = self.make_admins(2)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]

        skipped = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(skipped.status_code, 409)
        self.assertIn("第一复核", skipped.json()["message"])

    def test_non_admin_cannot_review(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant = self.make_admins(1)[0]
        plain = User.objects.create_user("plain", "pass123456", role="user")
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]

        response = self.api_for(plain).post(
            f"/api/releases/{rid}/review/", {"action": "approve"}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("权限", response.json()["message"])

    def test_reject_at_review_records_reason_and_terminal_state(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant, reviewer = self.make_admins(2)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]

        # 拒绝必须填写原因
        no_reason = self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/", {"action": "reject"}, format="json"
        )
        self.assertEqual(no_reason.status_code, 400)

        rejected = self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/",
            {"action": "reject", "remark": "文书编号不符"}, format="json",
        )
        self.assertEqual(rejected.status_code, 200)
        data = rejected.json()["data"]
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["reject_info"]["reason"], "文书编号不符")
        self.assertEqual(data["reject_info"]["step"], "review")
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("12"))  # 拒绝不扣库存

        # 终态后任何签署都被拒绝
        again = self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/",
            {"action": "approve"}, format="json",
        )
        self.assertEqual(again.status_code, 409)

    def test_expired_application_blocks_signing_and_shows_state(self):
        applicant, releaser = self.make_admins(2)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]
        StockOut.objects.filter(pk=rid).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        detail = self.client.get(f"/api/releases/{rid}/")
        self.assertEqual(detail.json()["data"]["status"], "expired")

        late = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(late.status_code, 409)
        self.assertIn("过期", late.json()["message"])
        self.assertEqual(StockOut.objects.get(pk=rid).status, "expired")

    def test_permission_revoked_after_signature_blocks_release(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant, reviewer, releaser = self.make_admins(3)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]
        self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/", {"action": "approve"}, format="json"
        )

        # 第一复核人签署后被停用：尚未完成的放行必须被阻止
        reviewer.is_active = False
        reviewer.save(update_fields=["is_active"])

        blocked = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("权限已被撤销", blocked.json()["message"])
        stock_out = StockOut.objects.get(pk=rid)
        self.assertEqual(stock_out.status, "terminated")
        self.assertIn("第一复核", stock_out.terminate_reason)

    def test_applicant_deactivation_blocks_release(self):
        applicant, releaser = self.make_admins(2)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]
        applicant.is_active = False
        applicant.save(update_fields=["is_active"])

        blocked = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(StockOut.objects.get(pk=rid).status, "terminated")

    def test_reviewer_role_downgrade_blocks_release(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant, reviewer, releaser = self.make_admins(3)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]
        self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/", {"action": "approve"}, format="json"
        )

        reviewer.role = "user"
        reviewer.save(update_fields=["role"])

        blocked = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(StockOut.objects.get(pk=rid).status, "terminated")

    def test_duplicate_signature_is_idempotent_conflict(self):
        applicant, releaser = self.make_admins(2)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]

        first = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(first.status_code, 200)

        second = self.api_for(releaser).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(second.status_code, 409)
        # 只有一条放行签署记录，库存只扣一次
        self.assertEqual(
            ReleaseSignature.objects.filter(
                stock_out_id=rid, step=STEP_RELEASE
            ).count(),
            1,
        )
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10"))

    def test_concurrent_final_signatures_yield_single_outcome(self):
        # 真正的跨连接并发验证在 ReleaseConcurrencyTest（TransactionTestCase）
        applicant, first_rel, second_rel = self.make_admins(3)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]

        first = self.api_for(first_rel).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        second = self.api_for(second_rel).post(
            f"/api/releases/{rid}/approve/", {"action": "approve"}, format="json"
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(StockOut.objects.get(pk=rid).status, "released")
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("10"))

    def test_detail_exposes_goods_summary_with_each_signature(self):
        self.goods.risk_level = "high"
        self.goods.save(update_fields=["risk_level"])
        applicant, reviewer = self.make_admins(2)
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]
        self.api_for(reviewer).post(
            f"/api/releases/{rid}/review/", {"action": "approve"}, format="json"
        )

        detail = self.client.get(f"/api/releases/{rid}/").json()["data"]
        self.assertEqual(detail["goods_summary"]["goods_code"], "DEV-001")
        # DRF DecimalField 按 decimal_places=2 规范化输入
        self.assertEqual(detail["goods_summary"]["quantity"], "2.00")
        for sig in detail["signatures"]:
            self.assertEqual(sig["goods_snapshot"]["goods_code"], "DEV-001")
            self.assertIn("risk_level", sig["goods_snapshot"])

    def test_create_validates_stock_and_input(self):
        short = self._create(self.client, quantity="999")
        self.assertEqual(short.status_code, 400)
        no_receiver = self._create(self.client, receiver="")
        self.assertEqual(no_receiver.status_code, 400)
        bad_qty = self._create(self.client, quantity="0")
        self.assertEqual(bad_qty.status_code, 400)

    def test_list_sweeps_expired_applications(self):
        applicant = self.make_admins(1)[0]
        rid = self._create(self.api_for(applicant)).json()["data"]["id"]
        StockOut.objects.filter(pk=rid).update(
            expires_at=timezone.now() - timedelta(hours=1)
        )
        listing = self.client.get("/api/releases/").json()["data"]
        self.assertEqual(listing["list"][0]["status"], "expired")


class ReleaseConcurrencyTest(TransactionTestCase):
    """并发签署：真实独立连接（TransactionTestCase）下只能有一个确定结果。

    不能用 TestCase——它把夹具包在测试事务里，其他线程的连接看不到数据。
    """

    def setUp(self):
        # WAL 下读不阻塞写：并发签署线程在唯一约束处决出唯一胜者，
        # 而不是在默认回滚日志模式下互相等锁超时
        from django.db import connection
        self._is_sqlite = connection.vendor == "sqlite"
        if self._is_sqlite:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode=WAL")

        self.admin = User.objects.create_user("seed-admin", "pass123456", role="admin")
        self.applicant = User.objects.create_user("applicant", "pass123456", role="admin")
        User.objects.create_user("rel-a", "pass123456", role="admin")
        User.objects.create_user("rel-b", "pass123456", role="admin")
        self.unit = Unit.objects.create(name="件", created_by=self.admin)
        self.category = Category.objects.create(
            name="受控介质", unit=self.unit, created_by=self.admin
        )
        self.variety = Variety.objects.create(
            name="封存硬盘", category=self.category, created_by=self.admin
        )
        self.goods = Goods.objects.create(
            variety=self.variety, name="涉密硬盘", code="DISK-009",
            quantity=Decimal("5"), warning_threshold=Decimal("1"),
        )

    def api_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def test_concurrent_final_signatures_yield_single_outcome(self):
        applicant_client = self.api_for(self.applicant)
        rid = applicant_client.post(
            "/api/releases/",
            {"goods": self.goods.id, "quantity": "2", "receiver": "值班员"},
            format="json",
        ).json()["data"]["id"]

        outcomes = []
        barrier = threading.Barrier(2)

        def sign(username):
            signer = User.objects.get(username=username)
            client = self.api_for(signer)
            barrier.wait()
            response = client.post(
                f"/api/releases/{rid}/approve/",
                {"action": "approve"}, format="json",
            )
            outcomes.append(response.status_code)

        threads = [
            threading.Thread(target=sign, args=("rel-a",)),
            threading.Thread(target=sign, args=("rel-b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "并发签署线程未在限定时间内结束（可能死锁）")

        self.assertCountEqual(outcomes, [200, 409])
        self.assertEqual(
            ReleaseSignature.objects.filter(
                stock_out_id=rid, step=STEP_RELEASE
            ).count(),
            1,
        )
        self.assertEqual(StockOut.objects.get(pk=rid).status, "released")
        self.assertEqual(Goods.objects.get(pk=self.goods.pk).quantity, Decimal("3"))
