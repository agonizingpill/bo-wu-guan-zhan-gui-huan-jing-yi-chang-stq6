# 博物馆展柜环境异常 · Museum Case Anomaly

面向博物馆夜班值守团队的展柜环境异常闭环应用，把温湿度/CO₂采样、门磁上下文、无标签异常识别、值守动作和文物巡检串成可审计的处置流程。

## 真实场景

模拟客户为 **海岚市博物馆藏品保护组**，4 个常设展厅、约 260 个展柜。夜班值守员目前使用门磁面板、环境记录 Excel 和纸质巡检卡分别记录现场情况。主要痛点有三项。

- 门开布展或清洁时温湿度突变，容易被误记成设备故障。
- 连续偏移需要人工翻查多条采样记录，升级馆长的时间点不稳定。
- 值守动作和文物巡检落在不同记录里，主管关闭异常时缺少完整证据链。

上游输入包括展柜档案、门磁状态、温湿度与 CO₂ 采样。系统替代夜班的异常分流和处置留痕，下游由藏品主管和馆长消费升级事件、巡检结果与关闭记录。

## 功能模块

- **展柜档案**。维护展厅、区域、文物绑定、环境窗口和门磁编号。
- **环境采样**。录入带时间的温度、相对湿度、CO₂、门磁状态和现场备注，后端限制可测范围与采样间隔。
- **异常事件**。worker 使用 `RobustScaler + IsolationForest` 计算无标签异常分数，识别环境离群、门开干扰和连续偏移。
- **同厅归并派单**。同展厅多柜同向温湿偏移且时段重叠时归并为待认领候选，值守认领后由一张处置单统一处置；越过保护范围的告警保持单柜升级。
- **值守处置**。按事件上下文写入关门复核、环境调整、传感器检查或馆长处置，动作和操作者进入审计日志。
- **文物巡检**。异常后记录文物、底座、玻璃状态。巡检失败会重新打开事件。

## 业务规则

- 门磁开启段的异常事件进入 `door_interference` 观察类型，必须完成 `verify_door_sealed` 后才能关闭，不能直接判定设备故障。
- 连续三个异常采样点升级为 `critical / escalated`，需要主管追加 `call_curator` 动作。
- 没有已完成值守动作，或最近一次文物巡检不是 `pass`，主管不能关闭事件。
- 同一展柜采样间隔超过 30 分钟会被拒绝，要求补齐缺测时段。
- viewer 只能读取，operator 可录入采样和执行值守/巡检，supervisor 才能创建展柜和关闭异常。

## 快速启动

```bash
cp .env.example .env
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
```

访问 `http://localhost:18268`。

手动 seed 可重复执行，使用自然键 upsert，不会新增重复记录。

```bash
docker compose exec backend python scripts/seed_demo.py
```

关闭并清理容器与卷。

```bash
docker compose down -v --remove-orphans
```

## 演示账号

统一密码为 `museum123`。

| 用户名 | 角色 | 主要权限 |
|---|---|---|
| `lin-operator` | operator | 查看、录入采样、执行值守动作、记录巡检 |
| `guo-supervisor` | supervisor | operator 权限、创建展柜、关闭异常、馆长升级处置 |
| `viewer` | viewer | 查看监测、档案、异常和巡检队列 |

## 技术栈

| 层 | 实现 |
|---|---|
| 前端 | Nginx、原生 HTML/CSS/ES modules |
| 后端 | Python 3.11、FastAPI、psycopg2 |
| 算法 worker | scikit-learn、Redis list queue |
| 数据库 | PostgreSQL 16 |
| 队列 | Redis 7 |
| 认证 | JWT HS256、三角色权限依赖 |

Compose 恰好包含 `frontend`、`backend`、`worker`、`db`、`redis` 五个服务。backend 与 worker 共享命名卷 `/shared`，worker 通过 `/shared/worker-heartbeat` 提供健康信号。

## 算法口径

输入为同一展柜按时间排序的 `[temperature_c, relative_humidity_pct, co2_ppm]`。worker 先使用 `RobustScaler` 按中位数和四分位距变换，再用 `IsolationForest(n_estimators=160, contamination=0.22, random_state=1069)` 输出 `decision_function` 分数和离群标签。低于 22% 分位阈值的样本形成异常候选。

门开采样只改变业务分类为门开干扰。连续异常计数由时间序列中的连续离群样本计算，达到 3 个时升级。少于 6 条有效采样时算法任务失败并把错误写入 `algorithm_runs.error`，便于重试。

## 枚举出现位置

- `CASE_STATUSES`、`ANOMALY_STATUSES`、`ANOMALY_TYPES`、`DUTY_ACTION_TYPES`、`CHECK_RESULTS` 位于 `backend/src/constants/enums.py`，数据库约束位于 `backend/migrations/001_init.sql`，前端展示映射位于 `frontend/src/app.js`。
- 角色 `operator / supervisor / viewer` 位于用户表约束、JWT 依赖 `backend/src/middlewares/auth.py`、路由依赖和前端用户状态中。
- 事件生命周期与值守动作的前置条件位于 `backend/src/main.py` 的异常、值守、巡检和关闭路由。

## 目录

```text
backend/
  migrations/001_init.sql
  scripts/seed_demo.py
  src/main.py
  src/services/algorithm.py
  src/worker.py
frontend/
  index.html
  nginx.conf
  src/app.js
  src/styles.css
docker-compose.yml
```

## License

Internal evaluation project. No external distribution license is granted.


## 业务核验模块

登录后打开页面右下角的「业务核验」。可从实时业务台账筛选记录、导出所选结果，主管可对当前证据签认并填写说明。

- **温湿越限时间窗** 按展柜和采样日将温湿越限、门开背景和有效样本分开计数，便于馆员核对同日环境波动。
- **值守处置交班** 并列展示异常登记时间、升级状态与已有处置次数，供交班时确认异常责任。
- **文物巡检证据链** 将每宗异常和对应文物巡检结果一一关联，找出缺少巡检证据的处置记录。

### 二次扩展核验

- **传感采样断档定位** 按展柜测量时序定位相邻有效采样间隔，单独输出缺少历史对照的展柜。
- **异常处置后环境复测** 用处置关闭后的首条有效采样判断温湿回归情况，并区分尚未处置和待采样。

## 业务处置与验收

登录后打开「展柜恢复与文物保护」。操作员提交结果、凭证号和说明，主管可复核关闭或退回补证，观察员可查看记录。关闭时复查源业务状态，处置历史保留每次操作。

- 采样断档补齐验收
- 异常后环境解除观察
- 馆长升级与文物放行

## 同厅异常归并与认领派单

夜班看到同厅多只展柜一起偏离时，常把一次空调故障拆成几起独立告警。异常页面新增「同厅归并待认领」看板，把这类告警收成一条候选，由值守认领后统一处置。

**归并口径**（实现于 `backend/src/anomaly_grouping.py`）

- 同一展厅 `display_cases.gallery`。
- 同向温湿偏移：温度与湿度相对展柜目标窗口中点的偏移方向一致，温度偏移 ≥ 0.2 °C、湿度偏移 ≥ 0.5 %。
- 时段重叠：成员采样时间跨度不超过 20 分钟，异常评分极差不超过 0.30。
- 至少覆盖 2 个不同展柜；门开干扰只做观察，不参与归并。
- 采样越过展柜温湿保护范围（目标窗口）的告警不参与归并，仍按单柜升级，看板单列展示。

**认领与派单**

- `POST /anomaly-groups/candidates/{id}/claim` 需携带读取到的 `revision` 与认领依据；带 `request_id` 时按请求号幂等。
- 处置单保存领单时的成员快照（`member_snapshot`）与成组依据（`evidence`），之后重算候选或关闭成员都不会改写它。
- 随后出分的展柜会进入新的候选，已认领候选保持原成员。
- 并发认领时 `handling_ticket_members.event_id` 唯一约束保证同一告警只进一张处置单。
- 派单分两步：先写 `dispatch_attempts` 意图（含成员与依据快照），再在独立事务中成单。进程中断后由启动钩子、worker 心跳与 `POST /anomaly-groups/dispatch/resume`（主管）恢复；`dispatch_key` 唯一约束保证重试不会重复开单。

**接口**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/anomaly-groups/candidates` | 待认领候选与单柜升级列表 |
| GET | `/anomaly-groups/candidates/{id}` | 单条候选与成组依据 |
| POST | `/anomaly-groups/candidates/{id}/claim` | 认领并派发处置单 |
| GET | `/anomaly-groups/tickets`、`/tickets/{id}` | 处置单与领单快照 |
| POST | `/anomaly-groups/dispatch/resume` | 主管手动恢复中断派单 |

`GET /anomalies` 与 `GET /anomalies/{id}` 会附带 `candidate`（同厅归并依据）与 `ticket`（处置单）字段，异常详情页据此展示成组依据和单柜升级。
