# 暴雨排涝联合调度

面向区防汛值班团队的排涝联合调度后端。把一次降雨过程冻结成可追踪的态势快照，
依据汇水分区容量、设备约束和道路风险生成建议动作，支持指挥员审批、改派、撤销与现场回执，
全程事件溯源，可重放、可恢复、可验证。

## 核心设计

| 需求 | 机制 |
| --- | --- |
| 态势快照可追踪 | 每次生成方案时冻结 `FrozenSnapshot`（含数据截止时刻），方案/动作/命令逐级引用 |
| 建议动作 | 规则化方案器：R1 超能力、R2 常规排水、R3 防倒灌、R4 恢复停机、R5 重力辅助、R6 水位警戒 |
| 审批/改派/撤销/回执 | 动作状态机 `proposed→approved→dispatched→acknowledged→completed/failed`，改派与撤销仅限发令前 |
| 迟到数据 | 观测时刻早于已冻结快照即标记 `late`，只更新工作快照并触发新版本方案；已发命令不可改写 |
| 幂等 | 上报 `report_id`、告警 `alert_id`、回执 `receipt_id` 按业务键去重，重复提交返回原结果 |
| 冲突拦截 | 发令前检查设备占用（同设备相反命令）与联锁组（同组不得同时启动），冲突不产生任何事件 |
| 超时升级 | `tick --at <时刻>` 扫描未确认命令逐级升级（值班长→区防指→市防指），虚拟时钟驱动，不依赖真实等待 |
| 重启恢复 | 全部状态变更追加写入 `events.jsonl`，进程重启重放事件恢复待执行/待回执队列 |
| 重放与验证 | `replay` 输出完整决策链；`verify-recovery` 校验各分区恢复次序是否符合优先级 |

## 目录结构

```
src/drainage_dispatch/
  contracts.py   # 基础契约（CommandAction / StormSnapshot / DrainageDevice）
  clock.py       # 可注入时钟：SystemClock / VirtualClock
  registry.py    # 分区与设备注册表（容量、道路风险、联锁组、时限配置）
  models.py      # 上报、告警、快照、方案、动作、命令、回执
  events.py      # 事件类型与 JSONL 事件日志
  state.py       # 事件折叠为应用状态（重启即重放）
  planner.py     # 规则化方案生成器
  service.py     # 核心服务：全部业务规则与幂等、冲突、升级逻辑
  api.py         # 标准库 HTTP 接口
  cli.py         # 命令行
config/registry.json  # 示例注册表（3 分区 / 6 设备）
```

## 快速开始

```bash
# 运行测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 命令行冒烟（端到端场景：上报→方案→审批→发令→回执→升级→迟到数据→重放）
python3 run_cli.py
```

## 命令行示例

```bash
export PYTHONPATH=src
D=dd_data

# 开启降雨过程并接入上报（积水点/管段水位/泵站/闸门）
python3 -m drainage_dispatch.cli --data-dir $D open-storm --storm ST-01 --rainfall 72.5 --basins BASIN-A,BASIN-B
python3 -m drainage_dispatch.cli --data-dir $D ingest --storm ST-01 --report-id R1 --kind waterlogging \
  --basin BASIN-A --subject WL-中山路口 --observed-at 2026-10-03T09:00:00Z --metric depth_m=0.45 --metric area_m2=1800

# 冻结快照并生成方案 → 审批 → 发令
python3 -m drainage_dispatch.cli --data-dir $D plan --storm ST-01
python3 -m drainage_dispatch.cli --data-dir $D approve --action ST-01-PLAN-v1-A01 --commander 值班长
python3 -m drainage_dispatch.cli --data-dir $D dispatch-plan --storm ST-01

# 现场回执（重复提交同一 receipt-id 自动幂等）
python3 -m drainage_dispatch.cli --data-dir $D receipt --receipt-id RC-1 --command CMD-ST-01-PLAN-v1-A01 --kind accepted

# 超时升级：显式推进时刻，无需真实等待
python3 -m drainage_dispatch.cli --data-dir $D tick --at 2026-10-03T09:30:00Z

# 迟到数据只能触发新版本方案
python3 -m drainage_dispatch.cli --data-dir $D ingest --storm ST-01 --report-id R1B --kind waterlogging \
  --basin BASIN-A --subject WL-中山路口 --observed-at 2026-10-03T08:55:00Z --metric volume_m3=900 --auto-plan

# 重放决策链 / 验证分区恢复次序 / 查看队列
python3 -m drainage_dispatch.cli --data-dir $D replay --storm ST-01
python3 -m drainage_dispatch.cli --data-dir $D verify-recovery --storm ST-01
python3 -m drainage_dispatch.cli --data-dir $D queues

# 启动 HTTP 接口（默认 127.0.0.1:8080）
python3 -m drainage_dispatch.cli --data-dir $D serve --port 8080
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/storms` | 开启降雨过程 |
| POST | `/storms/{id}/reports` | 接入现场上报（幂等，支持 `auto_plan`） |
| POST | `/storms/{id}/alerts` | 上报告警（幂等） |
| POST | `/storms/{id}/plans` | 冻结快照并生成新版本方案 |
| GET | `/storms/{id}/plans` | 方案版本与动作列表 |
| GET | `/storms/{id}/snapshot` | 冻结快照 + 工作快照 + 过期标记 |
| POST | `/actions/{id}/approve` `/reassign` `/revoke` `/dispatch` | 指挥员操作与发令 |
| POST | `/storms/{id}/dispatch-plan` | 按优先级批量发令，冲突逐条拦截 |
| POST | `/receipts` | 现场回执（幂等） |
| POST | `/tick` | 推进时刻并扫描超时升级 |
| GET | `/queues` | 待执行 / 待回执 / 已升级队列 |
| GET | `/storms/{id}/replay` | 重放完整决策链 |
| POST | `/storms/{id}/verify-recovery` | 验证分区恢复次序 |

错误映射：`ValidationError→400`、`NotFoundError→404`、`ConflictError/DeviceConflictError/StateTransitionError→409`。

## 关键不变量

- 已发出的命令不可改写：终态命令收到新回执返回 409；撤销/改派仅限发令前。
- 同一时刻只有最新方案版本可执行：新版本生成时，旧版本未发令动作自动取代（`superseded`）。
- 设备占用冲突在 `CommandDispatched` 事件产生前被拦截，冲突不会留下任何状态变更。
- 超时升级是事件流与注入时刻的纯函数，重放结果确定，可在任意虚拟时刻验证。
