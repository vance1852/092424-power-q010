# 电厂调度与能源分析与机组分析准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录电力市场基准电价、电厂与变电站设施、送出线路、燃料批次、发电计划和负荷情景，并保留机组巡检传感器统计分析准入流程。系统面向电价连续波动、关键送电送出线路恢复、电量调度和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 电力市场基准电价按结算日和来源修订登记，历史版本不会被覆盖；
- 电厂、储罐、终端与储能站设施建档，送出线路保存日能力、在途时间和损耗规则；
- 送出线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 燃料批次保留电源类型、牌号、数量、单位成本和接收时间，可计算加权燃料库存成本；
- 交易方提名支持载荷级幂等、优先级分配、燃料库存扣减和在途交接；
- 负荷情景保存电价变化、送出线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

需求响应子域支撑谷段削峰邀约从发起到付款的完整闭环：

- 邀约事件按站点 IANA 时区保存响应窗口，跨日本地午夜自动切段，证据按段可还原；
- 基线取窗口前 N 个相似日同时刻均值，整天停机按停机区间剔除，缺失测点按槽位剔除，槽位样本不足标记不可用；
- 事件实测与基线逐槽位比较，部分响应（目标内）与超额响应（目标外）分别按不同费率计价，另有覆盖率门槛下的参与补贴；
- 生命周期为创建、确认、执行（基线/实测）、复核、出账、发布；复核人不能是事件发起人，角色权限强制分离；
- 实测上报按幂等键去重，同一输入重放同一版本，绝不重复付款；
- 账单发布后输入冻结，只允许通过更正单引入新计量版本：旧账单置 `void`，新版本账单继承审计链，差额可查；
- 事件详情 API 提供金额、证据摘要（测点来源/版本、输入摘要、计算版本、覆盖率）与哈希审计链。

机组分析准入子域位于 `plant_science` 包，负责机组巡检传感器的设备构建登记、不可变校准协议、测点分片导入、异常测点复核、统计任务租约、分析准入决定和审计报告。该子域不连接传感器硬件，只处理已经结构化的校准记录。

## 目录

- `src/power_dispatch/`：电价、设施、送出线路、燃料库存、提名、负荷情景、需求响应结算、HTTP API 与离线验收；
- `src/plant_science/`：机组巡检传感器校准与统计分析准入；
- `fixtures/`：机组分析准入演示协议和结构化测点；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m power_dispatch.acceptance --workspace .
```

该命令会在内存数据库中登记六个结算日的峰谷电价，创建电厂、终端和送出线路，完成燃料库存入账、提名分配、送电及负荷情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

机组分析准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m plant_science.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m power_dispatch.api --database power_dispatch.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖电价、设施、送出线路、停运事件、燃料批次、提名、能力分配、送电、负荷情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

需求响应新增两个角色：`marketer`（营销，发起事件与出账）和 `customer`（大用户，确认邀约与上报实测）；复核与更正审批由 `risk` 承担，执行与测点登记由 `dispatcher` 承担。接口如下：

- `POST /dr/meter-series`：登记不可变计量序列（`kind=baseline|event`、`metric=load|outage`、来源与版本号），同版本不同内容冲突；
- `POST /dr/events`：创建邀约（窗口、基线日数、栅格、目标削减、部分/超额费率、报送截止）；
- `POST /dr/events/{id}/confirm`、`/cancel`：大用户确认或营销撤回；
- `POST /dr/events/{id}/baseline`：报送截止后选定基线序列版本并生成基线版本；
- `POST /dr/events/{id}/measurements`：上报实测（载荷级幂等键，重复上报返回同一版本）；
- `POST /dr/events/{id}/reviews`：风险复核 `approved|rejected|resubmit`，发起人被拒绝；
- `POST /dr/events/{id}/settlement` 与 `/settlement/publish`：生成草稿账单并发布；
- `POST /dr/events/{id}/corrections` 与 `POST /dr/corrections/{id}/apply`：账单发布后的更正单，应用后旧账单置 `void`、产生新版本账单；
- `GET /dr/events/{id}`：金额、证据摘要与全部版本；`GET /dr/events/{id}/audit-trail`：事件审计链；`GET /dr/corrections`：更正单列表；`GET /dr/audit/chain`：全链完整性校验。
