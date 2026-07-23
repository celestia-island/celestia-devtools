#!/usr/bin/env python3
"""
Entelecheia Interactive Mock Server — shittim-chest WebUI.

Architecture:
  All user requests flow through HubRis task_decompose → dispatch to
  sub-agents → HubRis report_integrate → final report.

  Thinking strategies reference plana skill prompts:
    plana/res/prompts/agents/hubris/skills/
      industrial_discover.md  — auto-discovery of industrial devices
      infer_semantics.md      — register field inference
      generate_plc_program.md — IEC 61131-3 SCL generation
      alarm_response.md       — alarm escalation & response

  Account → workspace mapping in ../accounts/workspaces.toml:
    midori → hydrogen corridor digital twin (entelecheia git workspace)

Flow:
  1. Connect → past YOLO results already visible
  2. User sends message → HubRis thinks → decompose → dispatch
  3. Sub-agents execute → HubRis integrates → final report
  4. Multiple Q&A rounds, topology/SCADA always available

Usage:
  python3 server.py [--port 8428] [--speed 1.0]
"""

import asyncio, json, os, sys, time, math, random
from datetime import datetime, timezone
from typing import Any, Optional
import websockets
from websockets.asyncio.server import ServerConnection

HOST = os.getenv("PLAYBACK_HOST", "127.0.0.1")
PORT = int(os.getenv("PLAYBACK_PORT", "8428"))
SPEED = float(os.getenv("PLAYBACK_SPEED", "1.0"))

# Auth — matches the real scepter's WebSocket auth flow.
# entelecheia scepter checks: ?token=xxx | Authorization: Bearer xxx | X-API-Key: xxx
# The mock token defaults to the connection token used in dev deployments.
MOCK_TOKEN = os.getenv("MOCK_TOKEN", "mock-token-for-development")

JSON = dict[str, Any]
_ws: Optional[ServerConnection] = None
_round = 0

# ── Auth ───────────────────────────────────────────────────────────────

def _check_auth(ws) -> bool:
    """Simulate scepter WebSocket auth. Returns True if token matches."""
    try:
        path = getattr(ws.request, 'path', '')
        if '?' in path:
            import urllib.parse
            qs = urllib.parse.parse_qs(path.split('?')[1])
            token = qs.get('token', [None])[0]
            if token == MOCK_TOKEN:
                return True

        headers = dict(getattr(ws.request, 'headers', {}))
        auth = headers.get('authorization', '')
        if auth.lower().startswith('bearer ') and auth[7:] == MOCK_TOKEN:
            return True
        if headers.get('x-api-key', '') == MOCK_TOKEN:
            return True
    except Exception:
        pass
    return False

# ── Helpers ────────────────────────────────────────────────────────────

def _n(method: str, params: JSON) -> str:
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False)

def _r(msg_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False)

async def _send(ws, method: str, params: JSON):
    await ws.send(_n(method, params))

# ── Past YOLO Reports (visible immediately on connect) ──────────────────

YOLO_PAST = [
    {
        "report_type": "skill_terminal", "agent_type": "HubRis", "agent_id": "hubris-001",
        "title": "[YOLO #142] 新设备自动组网: h2-tank-mp-02",
        "content": (
            "## YOLO Cycle #142 — 自动组网\n\n"
            "**Time**: 2026-07-22 06:00 UTC\n\n"
            "### 发现\n"
            "Modbus 总线自动扫描检测到新站号 0x11, 协议探测确认: Fauckunksa H2-Tank-MP 中压储氢罐\n\n"
            "### 寄存器映射\n"
            "24 Holding Registers (HR.40001-40024) + 8 Input Registers (IR.30001-30008)\n"
            "  40001: 压力 (÷10 MPa), 40002: 温度 (÷10 °C), 40003: 液位 (÷10 %)\n"
            "  40004: 阀门状态, 40020: 泄压开度 (0-100%), 40024: 故障码\n\n"
            "### 已完成\n"
            "- ✅ 设备注册到 evernight: station_id=h2-tank-mp-02\n"
            "- ✅ PLC FB126 自动生成并在线下载到 S7-1500\n"
            "- ✅ SCADA 拓扑更新: Box 4 新增节点\n"
            "- ✅ 报警规则: H@70MPa, HH@75MPa, ROC@0.3MPa/min"
        ),
        "summary": "新储氢罐 h2-tank-mp-02 (站号 0x11) 自动组网完成, 24×Holding + 8×Input 寄存器已映射, FB126 已下载。",
        "timestamp": "2026-07-22T06:00:00Z",
        "preset_options": ["确认"], "recommended_options": ["确认"],
        "model_name": "deepseek-v4-flash", "token_usage": [1847, 32000],
    },
    {
        "report_type": "skill_terminal", "agent_type": "OreXis", "agent_id": "orexis-001",
        "title": "[YOLO #141] 报警根因分析: 高压罐压力漂移",
        "content": (
            "## YOLO Cycle #141 — 报警分析\n\n"
            "**Time**: 2026-07-22 05:30 UTC\n\n"
            "### 事件\n"
            "`h2-tank-hp` 压力从 62.0 升至 68.4 MPa (6h 内 +6.4 MPa), 上升速率 0.32 MPa/min\n\n"
            "### 根因链\n"
            "PEM-A 效率优化 → 产氢量 +23% → 压缩机连续运行 47min → 储氢压力持续上升\n\n"
            "### 处置\n"
            "- ✅ 已执行泄压 30% → 低压罐 (操作员审批通过)\n"
            "- ⚠️ 压缩机出口压力调节器响应延迟 → 已创建维护工单\n"
            "- 📝 PLC FB120 已热更新: H 报警自动泄压 20% (原: 仅告警不动作)"
        ),
        "summary": "高压罐压力漂移根因定位为压缩机调节器响应延迟, 已泄压 + 修复 PLC 逻辑。",
        "timestamp": "2026-07-22T05:30:00Z",
        "preset_options": ["确认"], "recommended_options": ["确认"],
        "model_name": "deepseek-v4-flash", "token_usage": [1203, 32000],
    },
    {
        "report_type": "skill_terminal", "agent_type": "KaLos", "agent_id": "kalos-001",
        "title": "[YOLO #140] aoba 代码审查: 串口资源泄漏修复",
        "content": (
            "## YOLO Cycle #140 — 代码审查\n\n"
            "**Time**: 2026-07-22 05:00 UTC\n\n"
            "### 发现\n"
            "`src/cli/actions.rs:472` — `serialport::new().open()` 无 RAII 包装, 异常路径下串口未关闭\n\n"
            "### 修复\n"
            "- ✅ 新增 `ModbusPort` RAII 包装 → `Drop` 自动关闭串口\n"
            "- ✅ 补充 3 处 `unsafe` 块的 Safety 注释\n"
            "- ✅ `cargo clippy -- -D warnings` 通过\n"
            "- ✅ `just test e2e` → 21/21 PASS\n\n"
            "### 自动创建 PR\n"
            "分支 `fix/serial-leak-and-unsafe-doc` → master"
        ),
        "summary": "串口泄漏已修复, 3 处 unsafe Safety 文档已补充, PR 已自动创建。",
        "timestamp": "2026-07-22T05:00:00Z",
        "preset_options": ["确认"], "recommended_options": ["确认"],
        "model_name": "deepseek-v4-flash", "token_usage": [782, 32000],
    },
]

# ── Response Templates (triggered by user message) ─────────────────────

# ── Response Templates ─────────────────────────────────────────────────
#
# All user-initiated requests flow through HubRis first:
#   User message → HubRis task_decompose → dispatch to sub-agents → integrate → final report
#
# YOLO daemon tasks (background cycles) can directly target specific agents.
# ────────────────────────────────────────────────────────────────────────

RESPONSES = [
    {
        "trigger": ["接入", "新设备", "组网", "注册", "发现", "扫描", "network", "discover", "probe", "探测"],
        "hubris_decompose": (
            "## 任务分解: 新设备自动组网\n\n"
            "### 1. Modbus 总线扫描与协议探测 → SkeMma\n"
            "- 扫描站号 0x01–0xF7, 识别未注册的新站号\n"
            "- 功能码 03/04 探测 Holding/Input Register\n"
            "- 写测试 FC06 验证可写性\n\n"
            "### 2. 设备识别与寄存器映射 → OreXis\n"
            "- 交叉验证寄存器特征与已知型号库\n"
            "- 推断传感器类型、量程、单位\n"
            "- 生成报警阈值建议\n\n"
            "### 3. PLC 梯形图生成与在线写入 → SkeMma\n"
            "- 根据寄存器映射自动生成 FB 块\n"
            "- TIA Portal 在线编译 + RUN-P 下载\n"
            "- Modbus 回读验证写入正确性\n\n"
            "### 4. evernight 注册与 SCADA 拓扑更新 → HubRis\n"
            "- 注册 station_id, 写入 register_map.toml\n"
            "- 更新 SCADA 拓扑, 自动创建仪表盘"
        ),
        "sub_agents": [
            {
                "agent_type": "SkeMma", "agent_id": "skemma-001",
                "tool": "modbus_discover",
                "thinking": "扫描 Modbus 总线, 站号 0x01..0xF7, 波特率 19200/8E1...",
                "report": "## SkeMma: Modbus 扫描结果\n\n```\n站号 0x01: S7-1500 PLC          → 已注册\n站号 0x03: RSOC 燃料电池         → 已注册\n站号 0x05: PEM-A 电解槽          → 已注册\n站号 0x11: *** NEW ***           → 待识别\n站号 0x14: 高压储氢罐(原)        → 已注册\n```\n\nFC03 @0x11 Holding[0..23]: 全部可读 ✅\nFC04 @0x11 Input[0..7]: 8 个有效 ✅\nFC06 @0x11 Holding[20]=0x01: 写入+回读验证通过 ✅",
                "summary": "发现新站号 0x11, 24 Holding + 8 Input, 支持写操作。"
            },
            {
                "agent_type": "OreXis", "agent_id": "orexis-001",
                "tool": "register_identify",
                "thinking": "交叉验证寄存器特征: Holding[0]=0x464B('FK'), Holding[1]=0x4832('H2'), Holding[2]=0x4D50('MP')...",
                "report": "## OreXis: 设备识别报告\n\n| 寄存器 | 地址 | 原始值 | 推断 |\n|--------|------|--------|------|\n| Holding[0] | HR.40001 | 0x464B | Vendor: Fauckunksa |\n| Holding[1] | HR.40002 | 0x4832 | Type: H2-Tank |\n| Holding[2] | HR.40003 | 0x4D50 | Model: MP (中压) |\n| Input[0] | IR.30001 | 684 | 压力 (÷10=68.4 MPa) |\n| Input[1] | IR.30002 | 312 | 温度 (÷10=31.2°C) |\n| Input[2] | IR.30003 | 870 | 液位 (÷10=87.0%) |\n| Input[3] | IR.30004 | 1 | 阀门状态 (开) |\n\n**报警阈值建议**: H@70MPa, HH@75MPa, L@5MPa, ROC@0.3MPa/min\n**匹配型号**: Fauckunksa H2-Tank-MP (置信度 98.7%)",
                "summary": "确认为 Fauckunksa H2-Tank-MP 中压储氢罐, 匹配 register_map.toml。"
            },
            {
                "agent_type": "SkeMma", "agent_id": "skemma-001",
                "tool": "plc_program",
                "thinking": "生成 FB126 梯形图: 三通道采集(压力/温度/液位), 双级泄压联锁(H/HH), ROC 变化率监控, ESD 互锁...",
                "report": "## SkeMma: PLC 编程报告\n\n**目标**: S7-1500 @192.168.10.40:102\n\n### FB126 \"H2_Tank_MP_Monitor\" (342 bytes)\n\n```ladder\nNETWORK 1: 三通道采集\n  L IW256; ITD; DTR; L 0.1; *R; T \"DB20\".FB126.Pressure\n  L IW258; ITD; DTR; L 0.1; *R; T \"DB20\".FB126.Temperature\n  L IW260; ITD; DTR; L 0.1; *R; T \"DB20\".FB126.Level\n\nNETWORK 2: H 报警自动泄压 20%\n  L \"DB20\".FB126.Pressure; L 70.0; >=R\n  S \"DB20\".FB126.Alarm_H\n  L 20.0; T \"DB20\".FB126.Valve_Open_Pct\n\nNETWORK 3: HH 紧急停机 + 全开泄压\n  L \"DB20\".FB126.Pressure; L 75.0; >=R\n  S \"DB20\".FB126.Alarm_HH; S \"DB20\".ESD_Tank_MP\n  L 100.0; T \"DB20\".FB126.Valve_Open_Pct\n```\n\n| 步骤 | 结果 |\n|------|------|\n| TIA Portal 编译 | ✅ 0 errors |\n| RUN-P 下载 | ✅ DB20 已更新 |\n| Modbus HR.40512 回读 | ✅ 0xAC (FB126 OK) |",
                "summary": "FB126 已生成并下载到 S7-1500, 三通道采集+双级泄压+ESD 互锁。"
            },
        ],
        "hubris_final": (
            "## HubRis: 新设备组网完成\n\n"
            "### 执行摘要\n\n"
            "新接入的中压储氢罐已自动完成组网:\n\n"
            "| 步骤 | Agent | 状态 |\n|------|-------|------|\n"
            "| Modbus 扫描 | SkeMma | ✅ 站号 0x11, 24H+8I |\n"
            "| 设备识别 | OreXis | ✅ Fauckunksa H2-Tank-MP (98.7%) |\n"
            "| PLC 编程 | SkeMma | ✅ FB126 已下载, 在线验证通过 |\n"
            "| 拓扑注册 | HubRis | ✅ station_id=h2-tank-mp-02 |\n\n"
            "### 新设备信息\n"
            "- **ID**: h2-tank-mp-02 (站号 0x11)\n"
            "- **型号**: Fauckunksa H2-Tank-MP (中压储氢罐)\n"
            "- **寄存器**: 24 Holding + 8 Input\n"
            "- **报警**: H@70, HH@75, L@5, ROC@0.3 MPa/min\n"
            "- **PLC**: DB20.FB126 (S7-1500, 在线验证通过)\n"
            "- **SCADA**: 已加入 Box 4 拓扑, 仪表盘已创建"
        ),
        "hubris_summary": "新储氢罐 h2-tank-mp-02 自动组网完成: Modbus 扫描→设备识别→PLC 编程→拓扑注册。4 个子任务全部通过。",
        "token_usage": [6247, 64000],
    },
    {
        "trigger": ["代码", "code", "bug", "fix", "review", "审查", "aoba", "aris", "仓库", "repo"],
        "hubris_decompose": (
            "## 任务分解: aoba 代码审查\n\n"
            "### 1. 静态分析 → KaLos\n"
            "- cargo clippy 全量扫描 (847 lint 项)\n"
            "- cargo check 编译验证\n\n"
            "### 2. 依赖审计 → KaLos\n"
            "- cargo metadata 解析依赖图\n"
            "- 检测循环依赖、版本冲突\n\n"
            "### 3. Modbus 协议实现审查 → KaLos\n"
            "- FC01-16 功能码覆盖\n"
            "- 异常码处理正确性\n"
            "- CRC16 自验逻辑\n\n"
            "### 4. 自动修复 PR → KaLos\n"
            "- 生成修复 commit\n"
            "- cargo test 验证通过后创建 PR"
        ),
        "sub_agents": [
            {
                "agent_type": "KaLos", "agent_id": "kalos-001",
                "tool": "cargo_clippy",
                "thinking": "运行 cargo clippy -- -W clippy::all, 扫描 847 个 lint...",
                "report": "## KaLos: 静态分析结果\n\n| 文件 | 行号 | 级别 | 问题 |\n|------|------|------|------|\n| `src/cli/actions.rs` | 472 | 🔴 Error | 串口资源泄漏 (无 RAII 包装) |\n| `src/protocol/modbus/frame.rs` | 89 | 🟡 Warning | unsafe 块缺少 Safety 注释 |\n| `src/api/modbus/core.rs` | 156 | 🟡 Warning | 未使用的 Result 返回值 |\n\n`cargo check -p aoba`: ✅ PASS (0 errors)",
                "summary": "1 严重(串口泄漏) + 2 警告, cargo check 通过。"
            },
            {
                "agent_type": "KaLos", "agent_id": "kalos-001",
                "tool": "code_fix",
                "thinking": "生成修复: RAII 包装 ModbusPort, 补充 unsafe Safety 文档...",
                "report": "## KaLos: 自动修复 PR\n\n### 分支: `fix/serial-leak-and-unsafe-doc`\n\n| Commit | 内容 |\n|--------|------|\n| `fix: add RAII ModbusPort wrapper` | `src/cli/actions.rs` → Drop 自动关闭串口 |\n| `docs: add Safety comments for unsafe blocks` | `src/protocol/modbus/frame.rs` → 3 处 Safety 注释 |\n\n**验证**: `cargo clippy -- -D warnings` ✅, `just test e2e` 21/21 PASS ✅",
                "summary": "PR 已自动创建, 所有检查通过。"
            },
        ],
        "hubris_final": (
            "## HubRis: aoba 代码审查完成\n\n"
            "### 执行摘要\n\n"
            "| 步骤 | Agent | 状态 |\n|------|-------|------|\n"
            "| 静态分析 | KaLos | ✅ 1 严重 + 2 警告 |\n"
            "| 自动修复 | KaLos | ✅ PR 已创建 |\n\n"
            "### 关键发现\n"
            "- 🔴 `src/cli/actions.rs:472` — 串口资源泄漏 (已修复)\n"
            "- 🟡 `src/protocol/modbus/frame.rs:89` — unsafe Safety 文档缺失 (已补充)\n\n"
            "### 修复 PR\n"
            "分支 `fix/serial-leak-and-unsafe-doc` → master, 所有 CI 检查通过。"
        ),
        "hubris_summary": "aoba 审查完成: 1 严重(已修复) + 2 警告(已修复)。PR fix/serial-leak-and-unsafe-doc 已自动创建。",
        "token_usage": [4156, 64000],
    },
    {
        "trigger": ["报警", "alarm", "告警", "warning", "pressure", "温度", "异常", "泄压", "阀门"],
        "hubris_decompose": (
            "## 任务分解: 高压罐压力报警分析\n\n"
            "### 1. 报警数据采集 → OreXis\n"
            "- 从 evernight 拉取 24h 报警事件流\n"
            "- 提取 h2-tank-hp 的时间序列数据\n\n"
            "### 2. 时序分析与根因定位 → OreXis\n"
            "- 线性回归分析压力趋势\n"
            "- 关联上游设备(PEM-A, h2-comp-1)运行状态\n"
            "- 构建根因链\n\n"
            "### 3. 处置方案生成 → OreXis\n"
            "- 方案对比: 泄压 vs 停机 vs 告警\n"
            "- WriteApprovalRequest 等待操作员审批"
        ),
        "sub_agents": [
            {
                "agent_type": "OreXis", "agent_id": "orexis-001",
                "tool": "alarm_analyze",
                "thinking": "拉取 24h 报警流, 提取 h2-tank-hp 压力时间序列, 关联 PEM-A 产氢率和压缩机运行状态...",
                "report": "## OreXis: 报警数据分析\n\n### 压力趋势\n```\n22:48  62.0 MPa  → 正常\n22:54  65.2 MPa  → 预警线\n23:12  68.4 MPa  → H 报警 (阈值 70)\n23:17  [预测] 75.0 MPa → HH 紧急停机\n```\n\n线性回归: P(t) = 62.0 + 0.32×t (MPa/min), R² = 0.97\n\n### 根因链\nPEM-A 效率优化(+23%产量) → h2-comp-1 连续运行 47min → 出口压力调节器响应延迟 → 储氢压力持续上升",
                "summary": "根因: 压缩机出口压力调节器响应延迟。预测 5min 后触发 HH 停机。"
            },
            {
                "agent_type": "OreXis", "agent_id": "orexis-001",
                "tool": "write_approval",
                "thinking": "生成处置方案, 发起 WriteApprovalRequest: 打开 valve-bypass 开度 30%...",
                "report": "## OreXis: 处置方案\n\n| 方案 | 操作 | 风险 | 耗时 |\n|------|------|------|------|\n| **A (推荐)** | 泄压 30% → 低压罐 | 低压罐承压安全 | 90s |\n| B | 停机压缩机 | 产氢中断 15min | 30s |\n| C | 仅告警 | 5min 后触发 HH 停机 | 0 |\n\n### 长期修复\n1. PLC FB120 热更新: H 报警自动泄压 20%\n2. 压缩机运行时间限制: >30min → 自动降功\n3. ROC 阈值从 0.5 → 0.3 MPa/min\n\n⏸ **WriteApprovalRequest 已发起 — 等待操作员审批**",
                "summary": "推荐方案 A(泄压 30%), WriteApprovalRequest 等待审批。"
            },
        ],
        "hubris_final": (
            "## HubRis: 报警分析完成\n\n"
            "### 执行摘要\n\n"
            "| 步骤 | Agent | 状态 |\n|------|-------|------|\n"
            "| 数据分析 | OreXis | ✅ 根因定位: 压缩机调节器延迟 |\n"
            "| 处置方案 | OreXis | ⏸ 方案 A 等待审批 |\n\n"
            "### 关键决策\n"
            "高压罐压力 68.4 MPa, 上升速率 0.32 MPa/min, 预测 5min 后触发 HH 停机。\n"
            "推荐立即泄压 30% 至低压罐 (方案 A)。\n\n"
            "### 长期措施\n"
            "已自动生成 PLC FB120 热更新补丁: H 报警时自动泄压 20% (原: 仅告警)。\n"
            "等待 WriteApprovalRequest 审批通过后自动执行。"
        ),
        "hubris_summary": "高压罐压力 H 报警根因定位: 压缩机调节器延迟。推荐泄压 30%。WriteApprovalRequest 等待审批。",
        "token_usage": [3847, 64000],
    },
    {
        "trigger": ["PLC", "梯形图", "ladder", "S7", "写入", "编程", "DB", "FB", "SCADA"],
        "hubris_decompose": (
            "## 任务分解: PLC 梯形图自动生成\n\n"
            "### 1. PLC 状态读取 → SkeMma\n"
            "- 连接 S7-1500 @192.168.10.40:102\n"
            "- 读取当前 DB 结构和已有 FB 块列表\n\n"
            "### 2. 梯形图生成 → SkeMma\n"
            "- 根据寄存器映射自动生成 FB 块\n"
            "- 包含三通道采集、泄压联锁、ESD 互锁\n\n"
            "### 3. 在线编译与下载 → SkeMma\n"
            "- TIA Portal 语法检查\n"
            "- RUN-P 模式在线编译 + 下载\n"
            "- Modbus 回读验证写入正确性\n\n"
            "### 4. 互锁验证 → SkeMma\n"
            "- 确保新 FB 与已有 FB120/FB122/FB124 的安全互锁"
        ),
        "sub_agents": [
            {
                "agent_type": "SkeMma", "agent_id": "skemma-001",
                "tool": "plc_read",
                "thinking": "连接 S7-1500 @192.168.10.40:102, 读取 DB20 结构, 列出已有 FB 块...",
                "report": "## SkeMma: PLC 状态\n\n**目标**: S7-1500 @192.168.10.40:102 (RUN-P)\n\n### 已有 FB 块\n| FB | 名称 | 大小 | 状态 |\n|----|------|------|------|\n| FB120 | 电解槽控制 | 284B | ✅ Active |\n| FB122 | 压缩机控制 | 196B | ✅ Active |\n| FB124 | CHP 监控 | 158B | ✅ Active |\n\n### OB1 扫描周期: 12.4ms\n### DB20 可用空间: 1,847 bytes → 足够容纳新 FB126",
                "summary": "PLC 在线, RUN-P 模式, DB20 有 1.8KB 可用空间。"
            },
            {
                "agent_type": "SkeMma", "agent_id": "skemma-001",
                "tool": "plc_program",
                "thinking": "生成 FB126 梯形图: 三通道采集+双级泄压+ROC监控+ESD互锁, TIA Portal 在线编译...",
                "report": "## SkeMma: FB126 生成与下载\n\n### FB126 \"H2_Tank_MP_Monitor\" (342 bytes)\n\n```ladder\nNETWORK 1: 压力/温度/液位 三通道采集 (IW256/258/260→REAL)\nNETWORK 2: H 报警 @70MPa → 自动泄压 20%\nNETWORK 3: HH 报警 @75MPa → ESD 紧急停机 + 全开泄压\nNETWORK 4: ROC 监控 @0.3MPa/min → 预警\n```\n\n| 步骤 | 结果 |\n|------|------|\n| TIA Portal 语法检查 | ✅ 0 errors, 0 warnings |\n| 在线编译 (RUN-P) | ✅ 342 bytes |\n| 下载到 PLC | ✅ DB20 已更新 |\n| Modbus HR.40512 回读 | ✅ 0xAC (FB126 实例 OK) |\n\n### 互锁\n- OB1 第 48 行: `CALL FB126, DB20`\n- FB120 NETWORK 5: `AND NOT DB20.FB126.ESD` (安全互锁)\n- FB122 NETWORK 3: `AND NOT DB20.FB126.Alarm_HH` (压缩机连锁停机)",
                "summary": "FB126 编译+下载成功, 已与 FB120/FB122/FB124 建立安全互锁。"
            },
        ],
        "hubris_final": (
            "## HubRis: PLC 编程完成\n\n"
            "### 执行摘要\n\n"
            "| 步骤 | Agent | 状态 |\n|------|-------|------|\n"
            "| PLC 状态读取 | SkeMma | ✅ RUN-P, 1.8KB 可用 |\n"
            "| 梯形图生成 | SkeMma | ✅ FB126 (342B) |\n"
            "| 编译+下载 | SkeMma | ✅ 在线验证通过 |\n| 互锁验证 | SkeMma | ✅ FB120/122/124 互锁完整 |\n\n"
            "### FB126 功能\n"
            "- 三通道采集: 压力(IW256) + 温度(IW258) + 液位(IW260)\n"
            "- H 报警自动泄压 20%, HH 全开泄压 + ESD\n"
            "- ROC 变化率监控 0.3 MPa/min\n"
            "- 与 FB120/122/124 安全互锁\n\n"
            "PLC 已更新, OB1 循环扫描中。"
        ),
        "hubris_summary": "PLC FB126 已生成并在线下载到 S7-1500, 三通道采集+双级泄压+ESD+互锁全部通过。",
        "token_usage": [4891, 64000],
    },
]

# ── Topology Data ──────────────────────────────────────────────────────

STATIONS = [
    {"id": "rsoc-1", "name": "RSOC 可逆燃料电池", "kind": "rsoc", "status": "Online", "x": 100, "y": 200},
    {"id": "pem-a", "name": "PEM 电解槽 A", "kind": "electrolyzer", "status": "Online", "x": 200, "y": 200},
    {"id": "pem-b", "name": "PEM 电解槽 B", "kind": "electrolyzer", "status": "Online", "x": 300, "y": 200},
    {"id": "aem-1", "name": "AEM 电解槽", "kind": "electrolyzer", "status": "Degraded", "x": 400, "y": 200},
    {"id": "h2-tank-hp", "name": "高压储氢罐", "kind": "storage", "status": "Online", "x": 150, "y": 320},
    {"id": "h2-tank-lp", "name": "低压储氢罐", "kind": "storage", "status": "Online", "x": 280, "y": 320},
    {"id": "h2-comp-1", "name": "氢气压缩机", "kind": "compressor", "status": "Online", "x": 380, "y": 320},
    {"id": "fc-10kw", "name": "燃料电池堆", "kind": "fuel_cell", "status": "Online", "x": 250, "y": 420},
    {"id": "chp-1", "name": "CHP 热电联产", "kind": "chp", "status": "Online", "x": 370, "y": 420},
    {"id": "synth-1", "name": "合成反应器", "kind": "synthesis", "status": "Online", "x": 200, "y": 420},
    {"id": "plc-central", "name": "S7-1500 PLC", "kind": "plc", "status": "Online", "x": 80, "y": 500},
    {"id": "gw-edge", "name": "OPC-UA 网关", "kind": "gateway", "status": "Online", "x": 420, "y": 500},
    {"id": "sensor-press", "name": "压力传感器", "kind": "sensor", "status": "Online", "x": 130, "y": 560},
    {"id": "valve-main", "name": "氢气主控阀", "kind": "valve", "status": "Online", "x": 340, "y": 600},
]

AGENTS = [
    {"agent_type": "HubRis", "agent_id": "hubris-001", "status": "Idle", "llm_working": False, "cpu_usage": 0.05, "memory_mb": 128, "model_name": "deepseek-v4-flash", "tier": "Deep"},
    {"agent_type": "KaLos", "agent_id": "kalos-001", "status": "Idle", "llm_working": False, "cpu_usage": 0.02, "memory_mb": 96, "model_name": "deepseek-v4-flash", "tier": "Deep"},
    {"agent_type": "ApoRia", "agent_id": "aporia-001", "status": "Idle", "llm_working": False, "cpu_usage": 0.08, "memory_mb": 156, "model_name": "deepseek-v4-flash", "tier": "Normal"},
    {"agent_type": "OreXis", "agent_id": "orexis-001", "status": "Idle", "llm_working": False, "cpu_usage": 0.03, "memory_mb": 84, "model_name": "deepseek-v4-flash", "tier": "Basic"},
    {"agent_type": "EpieiKeia", "agent_id": "epieikeia-001", "status": "Idle", "llm_working": False, "cpu_usage": 0.01, "memory_mb": 72, "model_name": "deepseek-v4-flash", "tier": "Basic"},
    {"agent_type": "PhiLia", "agent_id": "philia-001", "status": "Idle", "llm_working": False, "cpu_usage": 0.04, "memory_mb": 112, "model_name": "deepseek-v4-flash", "tier": "Deep"},
]

# ── Warm-up: send past YOLO results ────────────────────────────────────

async def warmup(ws):
    await _send(ws, "Sync.ServerVersion", {"version": "0.2.0-mock", "build_info": "entelecheia-scepter 0.2.0 [mock]"})
    await asyncio.sleep(0.2)
    await _send(ws, "Sync.HandshakeAck", {"ok": True, "session_id": "mock-session-001", "reconnect": False})
    await asyncio.sleep(0.3)
    await _send(ws, "Sync.ScepterIdentity", {"device_id": "mock-device-001"})
    await asyncio.sleep(0.5)

    # Agent roster
    await _send(ws, "Sync.AgentListResponse", {"agents": AGENTS})
    await asyncio.sleep(0.5)

    # Past YOLO reports
    for i, report in enumerate(YOLO_PAST):
        p = dict(report)
        now = datetime.now(timezone.utc).isoformat()
        p["timestamp"] = p.get("timestamp", now)
        await _send(ws, "Sync.AgentReport", p)
        await asyncio.sleep(0.8)

    print("  ✔ Warm-up complete: 3 YOLO reports + agent roster sent")

# ── Response playback: HubRis → dispatch → sub-agents → integrate ─────

async def play_response(ws, user_text: str):
    global _round
    _round += 1

    text_lower = user_text.lower()
    best = RESPONSES[0]
    best_score = 0
    for tmpl in RESPONSES:
        score = sum(1 for t in tmpl["trigger"] if t in text_lower)
        if score > best_score:
            best_score = score
            best = tmpl

    hubris_id = "hubris-001"
    print(f"  ▶ Round {_round}: HubRis ← \"{user_text[:50]}...\"")
    _d = 1.0 / SPEED

    # ═══ Phase 1: HubRis task decomposition ═══
    await _send(ws, "Sync.TaskCreated", {
        "task_id": f"task-{_round:03d}", "issue_id": f"issue-{_round:03d}",
        "title": f"用户请求 #{_round}: {user_text[:60]}",
        "assigned_agent": "HubRis",
    })
    await asyncio.sleep(0.2 * _d)
    await _send(ws, "Sync.TaskStatusUpdate", {"task_id": f"task-{_round:03d}", "status": "InProgress", "progress": 5})

    await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "status": "Thinking", "work_status": "Thinking", "current_model": "deepseek-v4-flash"})
    await asyncio.sleep(0.5 * _d)

    # HubRis thinking steps
    for label in ["解析用户需求 → 规划 skill chain...", "评估子任务依赖关系 → 确定执行顺序...", "task_decompose 完成, 准备分发到子 Agent..."]:
        await _send(ws, "Sync.AgentThinkingStep", {
            "agent_type": "HubRis", "agent_id": hubris_id,
            "step": {"id": f"hs-{_round}", "content": label, "status": "running", "timestamp": datetime.now(timezone.utc).isoformat()},
        })
        await asyncio.sleep(0.6 * _d)

    await _send(ws, "Sync.TaskStatusUpdate", {"task_id": f"task-{_round:03d}", "status": "InProgress", "progress": 20})

    # HubRis decompose report (streaming)
    await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "status": "StreamingResponse", "work_status": "StreamingResponse", "current_model": "deepseek-v4-flash"})
    await asyncio.sleep(0.2 * _d)

    decompose_words = best["hubris_decompose"].split()
    token_step = max(len(decompose_words) // 4, 1)
    for i, w in enumerate(decompose_words):
        await _send(ws, "Sync.AgentStreamingChunk", {
            "agent_type": "HubRis", "agent_id": hubris_id,
            "chunk": w + " ", "is_done": False, "chunk_kind": "Text", "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if i > 0 and i % token_step == 0:
            await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "token_usage": [int(i / len(decompose_words) * 800), best["token_usage"][1]]})
        await asyncio.sleep(0.03 * _d)
    await _send(ws, "Sync.AgentStreamingChunk", {
        "agent_type": "HubRis", "agent_id": hubris_id, "chunk": "", "is_done": True, "chunk_kind": "Text", "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    await _send(ws, "Sync.AgentReport", {
        "report_type": "skill_step", "agent_type": "HubRis", "agent_id": hubris_id, "agent_number": "001",
        "title": f"HubRis: 任务分解 #{_round}",
        "content": best["hubris_decompose"],
        "summary": f"任务已分解为 {len(best['sub_agents'])} 个子任务, 正在分发...",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "preset_options": ["确认", "修改分解方案"], "recommended_options": ["确认"],
        "model_name": "deepseek-v4-flash", "token_usage": [800, best["token_usage"][1]],
    })

    # ═══ Phase 2: Dispatch to sub-agents ═══
    for j, sub in enumerate(best["sub_agents"]):
        agent_type = sub["agent_type"]
        agent_id = sub["agent_id"]
        container_id = f"#{_round * 10 + j + 41:03d}"

        print(f"    └─ dispatch: {agent_type} ({sub['tool']})")

        # Container creation
        await _send(ws, "Sync.AgentToolCall", {
            "agent_type": "HubRis", "agent_id": hubris_id,
            "tool": "task_coordinate",
            "params": {"action": "create_container", "agent": agent_type},
            "result": None, "status": "started",
        })
        await asyncio.sleep(0.2 * _d)
        await _send(ws, "Sync.ContainerSnapshot", {"containers": [{
            "container_uuid": f"c-{_round}-{j}", "container_id": container_id,
            "agent_type": agent_type, "status": "Running", "workspace_path": "/workspace/h2-plant",
        }]})
        await asyncio.sleep(0.2 * _d)
        await _send(ws, "Sync.AgentToolCall", {
            "agent_type": "HubRis", "agent_id": hubris_id,
            "tool": "task_coordinate",
            "params": {"action": "create_container"},
            "result": {"container_id": container_id, "status": "Running"},
            "status": "completed",
        })

        # Sub-agent thinking
        await _send(ws, "Sync.AgentUpdate", {"agent_id": agent_id, "status": "Thinking", "work_status": "Thinking", "current_model": "deepseek-v4-flash"})
        await asyncio.sleep(0.3 * _d)
        await _send(ws, "Sync.AgentThinkingStep", {
            "agent_type": agent_type, "agent_id": agent_id,
            "step": {"id": f"sub-{_round}-{j}", "content": sub["thinking"], "status": "running", "timestamp": datetime.now(timezone.utc).isoformat()},
        })
        await asyncio.sleep(0.5 * _d)

        # Sub-agent report
        await _send(ws, "Sync.AgentUpdate", {"agent_id": agent_id, "status": "StreamingResponse"})
        await asyncio.sleep(0.2 * _d)

        sub_words = sub["report"].split()
        for i, w in enumerate(sub_words):
            await _send(ws, "Sync.AgentStreamingChunk", {
                "agent_type": agent_type, "agent_id": agent_id,
                "chunk": w + " ", "is_done": False, "chunk_kind": "Text", "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(0.02 * _d)
        await _send(ws, "Sync.AgentStreamingChunk", {
            "agent_type": agent_type, "agent_id": agent_id, "chunk": "", "is_done": True, "chunk_kind": "Text", "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await asyncio.sleep(0.2 * _d)

        await _send(ws, "Sync.AgentReport", {
            "report_type": "skill_terminal", "agent_type": agent_type, "agent_id": agent_id, "agent_number": container_id.strip("#"),
            "title": f"{agent_type}: {sub['tool']} 结果",
            "content": sub["report"], "summary": sub["summary"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "preset_options": ["确认"], "recommended_options": ["确认"],
            "model_name": "deepseek-v4-flash", "token_usage": [420 * (j + 1), best["token_usage"][1]],
        })

        await _send(ws, "Sync.AgentUpdate", {"agent_id": agent_id, "status": "Completed", "completion_outcome": "Reported"})
        await asyncio.sleep(0.3 * _d)
        await _send(ws, "Sync.AgentUpdate", {"agent_id": agent_id, "status": "Idle"})

        # Progress update
        pct = 20 + int(60 * (j + 1) / len(best["sub_agents"]))
        await _send(ws, "Sync.TaskStatusUpdate", {"task_id": f"task-{_round:03d}", "status": "InProgress", "progress": pct})

    # ═══ Phase 3: HubRis final integration ═══
    await _send(ws, "Sync.TaskStatusUpdate", {"task_id": f"task-{_round:03d}", "status": "InProgress", "progress": 85})
    await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "status": "Thinking", "work_status": {"Executing": {"skill_name": "report_integrate"}}, "current_model": "deepseek-v4-flash"})
    await asyncio.sleep(0.5 * _d)

    await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "status": "StreamingResponse", "work_status": "StreamingResponse"})
    await asyncio.sleep(0.2 * _d)

    final_words = best["hubris_final"].split()
    for i, w in enumerate(final_words):
        await _send(ws, "Sync.AgentStreamingChunk", {
            "agent_type": "HubRis", "agent_id": hubris_id,
            "chunk": w + " ", "is_done": False, "chunk_kind": "Text", "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if i > 0 and i % max(len(final_words) // 5, 1) == 0:
            used = int(best["token_usage"][0] * 0.85 + best["token_usage"][0] * 0.15 * i / len(final_words))
            await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "token_usage": [min(used, best["token_usage"][0]), best["token_usage"][1]]})
        await asyncio.sleep(0.03 * _d)
    await _send(ws, "Sync.AgentStreamingChunk", {
        "agent_type": "HubRis", "agent_id": hubris_id, "chunk": "", "is_done": True, "chunk_kind": "Text", "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    await asyncio.sleep(0.3 * _d)

    await _send(ws, "Sync.AgentReport", {
        "report_type": "skill_terminal", "agent_type": "HubRis", "agent_id": hubris_id, "agent_number": "001",
        "title": f"HubRis: 执行摘要 #{_round}",
        "content": best["hubris_final"], "summary": best["hubris_summary"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "preset_options": ["确认", "深入分析", "导出报告"], "recommended_options": ["确认"],
        "model_name": "deepseek-v4-flash", "token_usage": best["token_usage"], "skill_count": len(best["sub_agents"]) + 2, "mcp_count": len(best["sub_agents"]) * 2,
    })

    # Task complete
    await _send(ws, "Sync.TaskStatusUpdate", {"task_id": f"task-{_round:03d}", "status": "Completed", "progress": 100})
    await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "status": "Completed", "completion_outcome": "Reported", "token_usage": best["token_usage"]})
    await asyncio.sleep(0.5 * _d)
    await _send(ws, "Sync.AgentUpdate", {"agent_id": hubris_id, "status": "Idle"})

    print(f"  ✔ Round {_round} complete: HubRis + {len(best['sub_agents'])} sub-agents")


# ── Request Handlers ───────────────────────────────────────────────────

async def handle_request(ws: ServerConnection, text: str) -> Optional[str]:
    """Returns the user message text if this is a UserMessage, else None."""
    try:
        v = json.loads(text)
    except json.JSONDecodeError:
        return None

    method = v.get("method", "")
    msg_id = v.get("id")

    # Connection
    if method in ("Sync.ConnectHandshake", "Tui.ConnectHandshake"):
        await ws.send(_r(msg_id, {"ok": True, "version": "0.2.0-mock"}))
        return None

    # Workspace
    if method in ("workspace.list", "workspace.get"):
        await ws.send(_r(msg_id, [{
            "id": "mock-ws-001", "alias": "h2-plant", "kind": "personal",
            "connection_kind": "personal", "status": "active", "workspace_path": "/workspace/h2-plant",
        }]))
        return None
    if method == "workspace.listByGroup":
        await ws.send(_r(msg_id, {"personal": [{"id": "mock-ws-001", "alias": "h2-plant"}], "groups": []}))
        return None

    # Agents
    if method == "Tui.ListAgents":
        await ws.send(_r(msg_id, {"agents": AGENTS}))
        return None

    # Topology
    if method == "topology.list_stations":
        await ws.send(_r(msg_id, {"stations": STATIONS}))
        return None
    if method == "topology.box_detail":
        sid = v.get("params", {}).get("station_id", "")
        st = next((s for s in STATIONS if s["id"] == sid), None)
        if not st:
            await ws.send(_r(msg_id, None))
            return None
        await ws.send(_r(msg_id, {"station": st, "params": {
            "efficiency": f"{random.uniform(72,88):.1f}%", "temperature": f"{random.uniform(650,920):.0f}°C",
            "output": f"{random.uniform(45,95):.1f} kW", "uptime": f"{random.uniform(95,99.9):.2f}%",
        }}))
        return None
    if method == "topology.gauges":
        await ws.send(_r(msg_id, {"gauges": [
            {"widget_id": "g-pressure", "title": "高压罐压力", "value": 68.4, "unit": "MPa", "min": 0, "max": 100},
            {"widget_id": "g-temp", "title": "RSOC 温度", "value": 820, "unit": "°C", "min": 0, "max": 1000},
            {"widget_id": "g-flow", "title": "H₂ 流量", "value": 12.3, "unit": "Nm³/h", "min": 0, "max": 50},
            {"widget_id": "g-voltage", "title": "母线电压", "value": 48.2, "unit": "V", "min": 0, "max": 60},
        ]}))
        return None
    if method == "topology.trend_data":
        def _sine(n, amp, off, freq):
            return [round(off + amp * math.sin(2*math.pi*freq*i/n + random.random()), 2) for i in range(n)]
        await ws.send(_r(msg_id, {"trends": [
            {"widget_id": "t-pressure", "title": "压力趋势", "unit": "MPa", "values": _sine(60, 5, 65, 3)},
            {"widget_id": "t-temp", "title": "温度趋势", "unit": "°C", "values": _sine(60, 50, 800, 5)},
        ]}))
        return None
    if method == "topology.value_cards":
        await ws.send(_r(msg_id, {"cards": [
            {"card_id": "vc-p", "title": "压力", "value": "68.4", "unit": "MPa"},
            {"card_id": "vc-t", "title": "温度", "value": "820", "unit": "°C"},
            {"card_id": "vc-f", "title": "流量", "value": "12.3", "unit": "Nm³/h"},
            {"card_id": "vc-e", "title": "效率", "value": "78.3", "unit": "%"},
        ]}))
        return None
    if method == "topology.kanban":
        await ws.send(_r(msg_id, {"columns": [
            {"column_id": "backlog", "title": "待处理", "cards": [
                {"card_id": "k1", "title": "AEM 电解槽诊断", "priority": "high"},
                {"card_id": "k2", "title": "高压罐传感器校准", "priority": "medium"},
            ]},
            {"column_id": "in_progress", "title": "进行中", "wip": 3, "cards": [
                {"card_id": "k3", "title": "Modbus 网络扫描", "priority": "urgent"},
            ]},
            {"column_id": "done", "title": "已完成", "cards": [
                {"card_id": "k4", "title": "aoba 代码审查", "priority": "medium"},
                {"card_id": "k5", "title": "YOLO #142 记忆整合", "priority": "low"},
            ]},
        ]}))
        return None
    if method == "topology.pipeline":
        await ws.send(_r(msg_id, {
            "nodes": [
                {"node_id": "evernight", "title": "Evernight 采集", "kind": "source"},
                {"node_id": "scepter", "title": "Scepter 调度", "kind": "process"},
                {"node_id": "orexis", "title": "OreXis 报警", "kind": "process"},
                {"node_id": "hubris", "title": "HubRis 规划", "kind": "process"},
                {"node_id": "webui", "title": "WebUI 展示", "kind": "sink"},
            ],
            "edges": [
                {"from": "evernight", "to": "scepter"}, {"from": "scepter", "to": "orexis"},
                {"from": "orexis", "to": "hubris"}, {"from": "hubris", "to": "webui"},
            ]
        }))
        return None
    if method == "topology.alarms":
        await ws.send(_r(msg_id, {"alarms": [
            {"alarm_id": "a1", "station_id": "h2-tank-hp", "severity": "High",
             "parameter": "pressure", "value": 68.4, "unit": "MPa", "limit": 70.0,
             "timestamp": datetime.now(timezone.utc).isoformat(), "acknowledged": False},
        ]}))
        return None

    # Snapshot / status
    if method in ("Tui.RequestWorkspaceStatus", "Tui.RequestFullSnapshot", "Tui.RequestGlobalSnapshot"):
        await ws.send(_r(msg_id, {"ok": True}))
        return None

    # UserMessage — triggers playback
    if method in ("Tui.UserMessage", "Sync.UserMessage"):
        params = v.get("params", {})
        content = params.get("content", "")
        if content:
            return content
        return None

    # Default
    await ws.send(_r(msg_id, {"ok": True}))
    return None


# ── WebSocket Server ────────────────────────────────────────────────────

async def handler(ws: ServerConnection) -> None:
    global _ws, _round
    if _ws is not None:
        await ws.close(1008, "busy")
        return

    _ws = ws
    _round = 0
    try:
        addr = ws.request.remote_address
    except AttributeError:
        addr = "?"

    if not _check_auth(ws):
        print(f"  ✗ Auth rejected: {addr}")
        await ws.close(4001, "unauthorized")
        _ws = None
        return

    print(f"  ✔ Connected: {addr} (auth OK)")

    try:
        # Phase 1: warm-up — past YOLO reports
        await warmup(ws)

        # Phase 2: idle loop — wait for user messages
        async for msg in ws:
            if not isinstance(msg, str):
                continue
            user_text = await handle_request(ws, msg)
            if user_text:
                await play_response(ws, user_text)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _ws = None
        print("  ✗ Disconnected")


async def main():
    global SPEED
    import argparse
    p = argparse.ArgumentParser(description="Entelecheia Interactive Mock Server")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--host", default=HOST)
    p.add_argument("--speed", type=float, default=SPEED)
    args = p.parse_args()

    SPEED = args.speed

    print(f"🎬 Entelecheia Interactive Mock Server")
    print(f"   ws://{args.host}:{args.port}")
    print(f"   Speed:       {SPEED}x")
    print(f"   YOLO past:   {len(YOLO_PAST)} historical reports")
    print(f"   Templates:   {len(RESPONSES)} response patterns")
    print(f"   Topology:    {len(STATIONS)} stations")
    print(f"")
    print(f"   Flow: connect → see YOLO reports → send message → thinking → streaming → report")
    print(f"")

    async with websockets.serve(handler, args.host, args.port):
        print(f"  Ready — connect shittim-chest WebUI")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
