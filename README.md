# 暴雨排涝联合调度

面向区防汛值班团队的排涝联合调度后端：把一次降雨过程冻结成可追踪的态势快照，
依据汇水分区容量、设备约束与道路风险生成建议动作，支撑指挥员审批、改派、撤销
与现场回执的完整指挥链。

## 设计要点

- **事件溯源**：所有状态变化先追加到 SQLite 事件库（`store.py`）再应用到内存投影
  （`service.py`），进程重启后按序重放即可恢复待发令与待回执队列。
- **快照与方案版本**：`freeze` 把当前上报冻结成不可变快照；迟到数据（观测时间早于
  最近快照冻结时刻）只进入工作集，触发新版本方案，已发令命令永远不被改写。
- **设备占用冲突拦截**：审批即预占设备，改派与发令前都会硬校验，同一设备不会被
  并行动作下达重复/相反命令。
- **超时升级**：回执期限按升级阶梯（默认 900/1800/3600 秒）逐级升级；由注入时钟
  驱动，`tick` 推进虚拟时间即可触发，不依赖真实等待，偏移量持久化、跨进程有效。
- **幂等**：相同上报、回执、降雨过程、设备登记重复提交不产生重复效果；
  内容冲突的同 id 提交返回 409。

## 运行测试与检查

```bash
python3 -m unittest discover -s tests -v   # 运行测试
python3 -m compileall -q src tests run_cli.py  # 编译检查
python3 run_cli.py                          # 命令行冒烟
```

## 命令行用法

```bash
python3 run_cli.py --db dispatch.db declare-storm --storm-id ST-01 --basin BASIN-A --basin BASIN-B
python3 run_cli.py --db dispatch.db register-device --device-id PUMP-1 --basin-id BASIN-A --kind pump --capacity 30
python3 run_cli.py --db dispatch.db ingest-report --storm-id ST-01 --report-id R-1 \
    --kind waterlogging --basin-id BASIN-A --observed-at 2026-10-04T07:50:00+00:00 \
    --payload '{"depth_m":0.5,"area_m2":1200}'
python3 run_cli.py --db dispatch.db freeze --storm-id ST-01
python3 run_cli.py --db dispatch.db plan --storm-id ST-01
python3 run_cli.py --db dispatch.db approve --action-id ST-01-PLAN-v1-A01 --by 指挥员
python3 run_cli.py --db dispatch.db dispatch --action-id ST-01-PLAN-v1-A01 --by 值班长
python3 run_cli.py --db dispatch.db tick --seconds 1000        # 推进虚拟时钟，触发超时升级
python3 run_cli.py --db dispatch.db receipt --receipt-id RC-1 --action-id ST-01-PLAN-v1-A01 --status done --by 现场
python3 run_cli.py --db dispatch.db replay --storm-id ST-01           # 重放完整决策链
python3 run_cli.py --db dispatch.db recovery-order --storm-id ST-01   # 各分区恢复次序
python3 run_cli.py --db dispatch.db serve --port 8080                 # 启动 HTTP 服务
```

上报类型：`waterlogging`（积水点）、`pump_status`（泵站）、`gate_status`（闸门）、
`pipe_level`（管段水位）、`road_risk`（道路风险）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/storms` | 宣布降雨过程 |
| GET | `/storms/{id}` | 降雨过程态势（快照、方案版本、动作状态统计） |
| POST | `/devices` / GET `/devices` | 登记/列出排涝设备 |
| POST | `/storms/{id}/reports` | 接收上报（幂等，迟到数据标记 `late`） |
| POST | `/storms/{id}/freeze` | 冻结态势快照 |
| POST | `/storms/{id}/plans` | 生成新版本方案 |
| GET | `/storms/{id}/plans` · `/plans/{id}` | 查询方案 |
| POST | `/actions/{id}/approve` · `/revoke` · `/reassign` · `/dispatch` | 审批/撤销/改派/发令 |
| POST | `/plans/{id}/dispatch` | 批量发令（单条冲突被拦截并记录） |
| POST | `/receipts` | 登记现场回执（幂等） |
| POST | `/tick` | 推进虚拟时钟并检查超时升级 |
| GET | `/storms/{id}/queues` · `/queues` | 待发令/待回执队列 |
| GET | `/storms/{id}/replay` | 重放完整决策链 |
| GET | `/storms/{id}/recovery-order` | 各汇水分区恢复次序 |

错误统一返回 `{"error": {"code", "message"}}`，常见错误码：`DEVICE_CONFLICT`
（设备占用冲突）、`ALREADY_DISPATCHED`（已发令不可撤销）、`REPORT_CONFLICT` /
`RECEIPT_CONFLICT`（同 id 内容冲突）、`NO_NEW_REPORTS`、`ACTION_NOT_DISPATCHED`。
