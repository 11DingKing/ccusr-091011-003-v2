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

## 放行双人复核流程

高风险受控物资的出库必须经 **申请 → 第一复核 → 最终放行** 三步，且三个岗位由
三个不同账号担任；低风险物资为 **申请 → 最终放行** 两步。物资的风险等级
（`Goods.risk_level`: `low`/`high`）决定流程，不由申请人选择。

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/releases/` | POST | 提交放行申请（自动生成「申请」签署，24 小时有效） |
| `/api/releases/` | GET | 申请列表（自动落库过期/失权状态），支持 `status`、`risk_level` 过滤 |
| `/api/releases/{id}/` | GET | 详情：`missing_steps` 展示还缺哪一步、`goods_summary` 物资摘要、`signatures` 每次签署（含签署时看到的摘要）、`reject_info` 拒绝信息 |
| `/api/releases/{id}/review/` | POST | 第一复核（仅高风险）：`{"action":"approve"}` 或 `{"action":"reject","remark":"原因"}` |
| `/api/releases/{id}/approve/` | POST | 最终放行：动作同上；通过即原子扣减库存 |

状态：`pending_review` → `pending_release` → `released`；任一环节拒绝为
`rejected`，超 24 小时为 `expired`，签署链条中任何人被停用或降权（管理权限撤销）
为 `terminated`，均为终态。规则要点：

- 三岗（或两岗）账号必须互不相同；复核/放行岗位要求管理员身份。
- 任何人在签署后权限被撤销，尚未完成的放行立即被阻止并终止。
- 签署在数据库事务内完成，`(申请, 步骤)` 上有唯一约束；重复签署、并发签署
  只有一个确定结果（后到者得到 409 及申请当前状态），库存只扣减一次。

