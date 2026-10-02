# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 受控介质放行（按风险等级双人复核）

货物以 `risk_level` 区分 `normal`（普通物资）与 `high`（高风险物资）。放行申请 `ReleaseRequest` 在提交时快照风险等级，并据此决定签署链：

- 普通物资：申请人 `applicant` → 最终放行人 `final_releaser`
- 高风险物资：申请人 `applicant` → 第一复核人 `first_reviewer` → 最终放行人 `final_releaser`

三类角色不得由同一账号兼任。任一签署人可拒绝（申请立即变为 `rejected`）；超过 `RELEASE_REQUEST_TTL_SECONDS`（默认 24 小时）未完成变为 `expired`；任一已签署人在完成前被停用或降级，在途放行变为 `blocked`。终态不可逆，重复或并发签署只产生一个确定结果（行锁 + (申请,角色) 唯一约束 + 乐观步骤校验）。

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /api/release-requests/` | 提交放行申请（提交即申请人签署） |
| `GET  /api/release-requests/` | 列表，支持 `status` / `risk_level` / `goods` / `search` / 分页 |
| `GET  /api/release-requests/{id}/` | 详情：物资摘要、各步骤状态、`missing_steps`、`next_step`、`can_sign` |
| `POST /api/release-requests/{id}/sign/` | 下一步签署 `{"action":"approved|rejected","role":"first_reviewer|final_releaser","comment":""}` |

签署时建议回传 `role`（取自详情的 `next_step`）作为乐观并发意图：两人同时点击同一复核步骤时，一人成功、另一人得到 `409`，不会被错记为最终放行人。全部签署完成后在同一事务内原子扣减库存并生成 `completed` 出库记录。

状态值：`signing` 签署中 / `released` 已放行 / `rejected` 已拒绝 / `expired` 已过期 / `blocked` 权限阻止。

定时结算（可由外部调度调用）：

```python
from apps.warehouse.cron import settle_release_requests
settle_release_requests()  # 过期 -> expired；签署人权限撤销 -> blocked
```
