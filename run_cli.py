"""命令行冒烟：走一遍完整调度流程并打印关键结果。

场景：节假日暴雨，BASIN-A 积水严重且管段水位超临界，BASIN-B 中度积水；
演示方案生成、审批、发令、回执、迟到数据触发新版本、超时升级与恢复次序验证。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from drainage_dispatch.cli import main as cli_main

DATA = tempfile.mkdtemp(prefix="dd_smoke_")
T0 = "2026-10-03T09:00:00+00:00"


def quiet(*argv):
    """执行命令但不打印中间结果。"""
    _capture(*argv)


def main():
    quiet("open-storm", "--storm", "ST-DEMO", "--rainfall", "72.5",
          "--basins", "BASIN-A,BASIN-B", "--at", T0)
    quiet("ingest", "--storm", "ST-DEMO", "--report-id", "R-WL-1", "--kind", "waterlogging",
          "--basin", "BASIN-A", "--subject", "WL-中山路口", "--observed-at", T0, "--at", T0,
          "--metric", "depth_m=0.45", "--metric", "area_m2=1800")
    quiet("ingest", "--storm", "ST-DEMO", "--report-id", "R-PIPE-1", "--kind", "pipe_level",
          "--basin", "BASIN-A", "--subject", "PIPE-A7", "--observed-at", T0, "--at", T0,
          "--metric", "level_m=3.6")
    quiet("ingest", "--storm", "ST-DEMO", "--report-id", "R-WL-2", "--kind", "waterlogging",
          "--basin", "BASIN-B", "--subject", "WL-解放大道", "--observed-at", T0, "--at", T0,
          "--metric", "volume_m3=900")
    quiet("alert", "--storm", "ST-DEMO", "--alert-id", "AL-1", "--basin", "BASIN-A",
          "--severity", "critical", "--message", "中山路口积水 0.45m，管段水位超临界", "--at", T0)
    dup = json.loads(_capture("alert", "--storm", "ST-DEMO", "--alert-id", "AL-1", "--basin", "BASIN-A",
                              "--severity", "critical", "--message", "重复转发同一条告警(应幂等)", "--at", T0))

    quiet("plan", "--storm", "ST-DEMO", "--at", "2026-10-03T09:05:00+00:00")
    plans = json.loads(_capture("plans", "--storm", "ST-DEMO"))["data"]
    for action in plans["actions"]:
        if action["status"] == "proposed":
            quiet("approve", "--action", action["action_id"], "--commander", "值班长-王",
                  "--at", "2026-10-03T09:06:00+00:00")
    quiet("dispatch-plan", "--storm", "ST-DEMO", "--at", "2026-10-03T09:07:00+00:00")

    queues = json.loads(_capture("queues", "--at", "2026-10-03T09:08:00+00:00"))["data"]
    first_cmd = queues["pending_receipts"][0]["command_id"]
    quiet("receipt", "--receipt-id", "RC-1", "--command", first_cmd, "--kind", "accepted",
          "--at", "2026-10-03T09:10:00+00:00")
    dup_rc = json.loads(_capture("receipt", "--receipt-id", "RC-1", "--command", first_cmd,
                                 "--kind", "accepted", "--at", "2026-10-03T09:10:00+00:00"))

    # 超时升级：其余命令 15 分钟未确认（显式推进时刻，无需真实等待）
    quiet("tick", "--at", "2026-10-03T09:30:00+00:00")

    # 迟到数据：观测时刻早于已冻结快照，只能触发新版本方案
    quiet("ingest", "--storm", "ST-DEMO", "--report-id", "R-WL-1B", "--kind", "waterlogging",
          "--basin", "BASIN-A", "--subject", "WL-中山路口", "--observed-at", "2026-10-03T08:55:00+00:00",
          "--at", "2026-10-03T09:31:00+00:00",
          "--metric", "depth_m=0.5", "--metric", "area_m2=1800", "--auto-plan")

    summary = {
        "场景": "节假日暴雨联合调度冒烟",
        "幂等验证": {"重复告警被去重": dup["data"]["duplicate"], "重复回执被去重": dup_rc["data"]["duplicate"]},
        "队列@09:30": json.loads(_capture("queues", "--at", "2026-10-03T09:30:00+00:00"))["data"],
        "恢复次序验证": json.loads(_capture("verify-recovery", "--storm", "ST-DEMO"))["data"],
        "决策链": json.loads(_capture("replay", "--storm", "ST-DEMO"))["data"]["chain"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _capture(*argv) -> str:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli_main(["--data-dir", DATA, *argv])
    assert code == 0, f"命令失败: {argv}"
    return buf.getvalue()


if __name__ == "__main__":
    main()
