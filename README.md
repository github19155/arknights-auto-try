# arknights-auto-try

将《明日方舟》的关卡情报、Copilot 作业校验、MAA 执行与战斗证据复核封装为 MCP 工具，也提供 CLI。

**实验性项目**：面向人工监督的单关执行，不承诺任意关卡自动通关或无人值守连续运行。常规执行不提供关卡浏览式自动选关；`probe_live` 只做有限的实机状态探测，可能点击关卡/难度条目，但不会按「开始行动」。执行前须手动进入目标关卡面板，核对关卡代号、难度与「开始行动」。

## 环境与安装

当前开发基线是 **Windows x64 + Python 3.13**。设备连接按 MAA 的 `MuMuEmulator12` 配置和 MaaTouch 初始化；其他模拟器未经验证，不视为已支持。

另行安装：

- [MAA](https://github.com/MaaAssistantArknights/MaaAssistantArknights)：需要 MaaCore、游戏资源及 `Python/asst` 绑定，不包含在本仓库中。`MAA_DIR` 应指向同时包含 `Python/` 与 `resource/` 的安装根目录。
- ADB：使用与你的模拟器兼容的可执行文件，确认设备已连接且可访问。
- 可选的 `ffmpeg` / `ffprobe`：用于外部转码、检查或人工视频复核；内置抽帧使用 OpenCV，并不要求这两个命令。

在项目根目录用 PowerShell 创建独立环境，按锁文件安装完整依赖集：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

`requirements.lock` 由 [uv](https://docs.astral.sh/uv/) 生成，固定直接依赖及其全部传递依赖的版本，用于可复现安装；CI 也按它安装。`requirements.txt` 只声明直接依赖，是锁文件的输入源，不作为安装入口：

```powershell
uv pip compile requirements.txt -o requirements.lock --python-version 3.13 --python-platform windows
```

改动 `requirements.txt` 后需重新生成 `requirements.lock` 并一并提交。

## 配置与启动

显式设置环境变量；本项目**不会自动加载 `.env`**。以下值均需替换为自己的安装路径和 ADB 设备标识：

```powershell
$env:MAA_DIR = 'C:/path/to/MAA'
$env:MAA_ADB = 'C:/path/to/adb.exe'
$env:MAA_SERIAL = '<adb-device-serial>'
.\.venv\Scripts\python.exe mcp_server.py
```

未设置时分别回退到占位值 `maa`、`adb` 与 `127.0.0.1:5555`，通常不能直接连接你的设备；[examples/mcp.stdio.json](examples/mcp.stdio.json) 会显式覆盖这些值。

默认使用 stdio。MCP 客户端配置见 [examples/mcp.stdio.json](examples/mcp.stdio.json)，其中 Python、服务脚本与环境变量均应使用自己的绝对路径。stdout 留给 JSON-RPC，原生 MAA 日志与 `src/planner/intel` 的缓存/告警日志都只写 stderr；库路径失败抛异常（不用 `SystemExit`），由 MCP 工具或 CLI 统一转成结构化错误。

需要 HTTP 时：

```powershell
.\.venv\Scripts\python.exe mcp_server.py --http --host 127.0.0.1 --port 8765
```

端点为 `http://127.0.0.1:8765/mcp`。`--http` 默认只绑回环地址；绑定非回环地址（`0.0.0.0`、局域网 IP、主机名等）必须显式加 `--allow-remote`，否则服务拒绝启动并返回退出码 2：

```powershell
.\.venv\Scripts\python.exe mcp_server.py --http --host 0.0.0.0 --allow-remote --port 8765
```

服务未提供应用层认证：任何能连上该端口的人都能调用全部 MCP 工具，包括截图、点击与实机执行。`--allow-remote` 只解除绑定门禁，不提供鉴权、TLS 或来源限制；只有在自行配置访问控制并理解设备操作权限后，才考虑改变监听地址。不确定时保持默认回环绑定。

### 实机执行的本地暂停模板

演习和正式作战使用 MAA 安装目录的 `resource/template/Battle/BattleFlag/BattleOfficiallyBegin.png` 识别活战斗。另需一个本地暂停模板：从自己的 **1280×720 暂停战斗截图**中，在右上角 `(1165, 20, 75, 65)` 区域内裁出播放三角及紧邻背景（参考大小 44×36），保存到：

```text
tools/maa-resources/abandon-battle/resource/template/BattlePauseMenu.png
```

该目录已被 Git 忽略。缺少模板时，执行器会在连接设备前报出路径；仅编队、情报查询、静态校验和录像抽帧不需要它。主线难度探测直接使用本机 MAA 的原生难度模板。若要取本地画面，可用 `see_screen`；不要提交自己的截图。

### 个人干员数据

拥有列表来自本地 `data/operators.json` 的 `own_opers`，由你自行放入自己的 MAA 导出文件，最小结构见 [examples/operators.json](examples/operators.json)。读取逻辑只读该文件，不实时扫描或同步账号；本项目不新增 Box 采集功能。公共仓库不附带个人 Box。缺少文件时按空 Box 处理（不报错），默认 `owned_only` 查询为空，**不代表识别到了空账号**；返回的 `box.source` 只标记是否采用关卡预设名单，不表示在线同步或数据新鲜度；`sync_time` 只是该导出文件的同步时间，不能当作当前账号状态。关卡情报和文档查询可能按需联网下载并写入 `data/` 缓存。

Copilot 静态校验还依赖 `data/gamedata_cache/range_table.json`。本地缺少该表时，`validate_copilot_plan` 与执行器连接设备前的校验会从 [Kengxxiao/ArknightsGameData](https://github.com/Kengxxiao/ArknightsGameData) 的 GitHub 原始地址下载射程表并写入缓存，因此首次 `validate` / `execute` 可能联网；离线环境需自行预先放入该文件，否则校验会直接报下载失败。`query_operator_specs` 在缺表时留空，不触发下载。

`query_operator_specs` 的 `actual` 只使用已有数据中的精英、等级和潜能；实际技能等级、专精与模组未知。`max` 是理论末级效果，不代表账号配置。实际技能、专精、模组采集不在计划内。

## 使用

| 工具 | 用途 |
| --- | --- |
| `get_stage_brief` / `query_stage_intel` | 摘要及按需地图、路线、波次、敌人、机制、干员与参考作业 |
| `get_stage_status_and_intel` | 综合情报；`probe_live=True` 时连接设备并点击关卡/难度条目做原生探测（**不是纯只读**：会产生点击与界面跳转，但不会按「开始行动」） |
| `get_enemy_visual` | 按敌人键或准确名称取 PRTS 立绘/头像，不做模糊相似匹配 |
| `get_next_stage` | 解析同区域后继及解锁依赖，不自动选关或推定通关 |
| `get_box_and_references` / `query_operator_specs` | 本地拥有列表、参考适配与理论机制 |
| `validate_copilot_plan` | 作业身份、站位、射程及动作名单等静态检查 |
| `execute_stage_plan` | 执行用户提供的 Copilot 文件，默认演习 |
| `see_screen` / `tap_text` / `tap_point` / `skip_story_phase` | 截图、原生 OCR 点击、坐标点击与剧情处理 |
| `review_battle_frames` / `request_human_demo` | 录像取帧复核；监听人类触控示范（不注入触摸，但收尾会终止设备上的 `getevent` 进程，并非严格零副作用） |
| `lookup_knowledge` | 本地知识卡、MAA 协议与 PRTS 词条 |

特殊玩法另有 `run_roguelike`、`run_sss_copilot`、`run_paradox_copilot` 桥接工具，但不属于当前单关可靠性的验收范围。MCP 还提供 `arknights_stage_sop` prompt，按人工监督顺序串起情报、校验、演习与证据复核。

### CLI 示例

在项目根目录执行。`$Stage` 必须与**自己提供的有效 Copilot 文件**中的 `stage_name` 对应；示例不依赖仓库内的个人作业：

```powershell
$Stage = 'YOUR-STAGE'
$Copilot = 'C:/path/to/your/copilot.json'
.\.venv\Scripts\python.exe cli.py intel $Stage
.\.venv\Scripts\python.exe cli.py validate $Stage $Copilot
# 先手动到达目标关卡面板，再执行：
.\.venv\Scripts\python.exe cli.py run $Stage $Copilot --mode exercise
.\.venv\Scripts\python.exe cli.py doc combat_core --section 技力
```

`cli.py campaign` 是另一个单关执行入口：读取关卡画像、按模式调用执行器并输出结构化结果；它不选关，也不连续推图。`campaign --mode official` 对非零理智关卡还要求 `--authorize-official`，零理智关卡可直接作战。各入口参数见 `cli.py --help` 及对应子命令的 `--help`。

## 执行边界与安全

- 默认 `exercise`；MCP 的非零理智 `official` 必须显式传入 `authorize_official=True`，CLI `run --mode official` 本身即为显式正式请求。不要让客户端代替用户推定授权。
- 零理智关卡的实际作战可转为 `official`；`formation` 始终只编队，系统预设编队可能返回 `not_started`，不是通关。
- 执行器在连接设备前校验作业与请求关卡身份及静态部署约束；这不能证明战术能成功，也不能替代人工核对当前面板。
- `--http` 默认只绑回环；非回环绑定必须显式 `--allow-remote`，否则拒绝启动（退出码 2）。该开关不提供认证，暴露到不可信网络等于交出实机控制权。
- 归位只是有限恢复：从结算、剧情、地图或详情界面识别关卡代号文本并尝试打开目标面板，失败即报错停止，不浏览主界面或关卡列表。
- 默认 `allow_leak=False`：检测到漏怪后尽快停止 Copilot、保存证据并尝试原生放弃。**理智返还以游戏结算为准**，采样与识别的延迟、放弃时机都会影响结果，因此既不保证一定返还，也不是绝不漏怪或绝不消耗资源的承诺。不自动使用理智药或源石回体。
- 停止自动化不等于退出战斗。退出/恢复链中用例 A（漏怪中断、证据留存、退出确认与归位）已真机通过，B–E 仍待验证；`ABANDON_FAILED`、`EXIT_UNCONFIRMED` 或未知结果都需要查看新截图并人工处理，不能假定已安全归位。可复现的真机验收步骤与判据见 [docs/VERIFICATION.md](docs/VERIFICATION.md)（A 已真机通过，B–E 待验证）。
- Copilot 链完成不等于胜利；实际作战成功需要已完成状态和原生胜利证据。`abandoned_on_*`、`failed`、`unknown` 不算通关。胜负判定依赖 MAA 原生 OCR/模板；结算 OCR 同时识别「行动结束」和「行动结果」两种标题，二者都只表示战斗结束，不能单独当作三星胜利证据。`MarkBattleWon` 只有 `not_battle` 当前帧的三枚蓝色奖章复核通过（`win_evidence="verified"`）才判 `won`；画面无奖章时划掉标记按 `ended` 处理；抓帧失败/画面未知时保留标记供人工复核，但战报仍降为 `unknown`。剧情恢复后若返回 `needs_retry`，也不会自动重跑。
- 原生 `won`/`failed` 优先于漏怪、放弃记账：`aborted_on_leak`、`abandon_failed` 只在没有原生胜负时出现。放弃失败时 `failure.reason="abandon_failed"`，漏怪证据仍在 `leak` 对象里；结算后归位失败不会吞掉战报，`failure.reposition_error` 记录原因（补写事件失败只进 `cleanup_errors`），无更强失败时状态为 `REPOSITION_FAILED` 且 `ok=false`，`battle` 为 `unknown`/`ended` 时同样适用。
- 漏怪四宫格与结算标记互不依赖：`MarkBattleWon`/`MarkBattleFailed` 不会让证据生成被跳过；拼图只尝试一次，失败原因记入战报的 `leak_evidence_error`，不重复产出事件或孤儿 PNG。
- 可选 `record_video=True` 使用 ADB `screenrecord`；录像失败会记录警告。事件、截图、视频和战报写入 `data/captures/`。执行结果里的 `events_path`、`review_manifest`、`evidence_png` 以及 `maa-run.json` 的 `summary.maa_user` 都是绝对本地路径（含盘符与用户名），分享录像或证据前需连同这些路径一起脱敏。

## 文档与许可

- [文档索引](docs/INDEX.md)
- [架构](docs/ARCHITECTURE.md)
- [路线图](docs/ROADMAP.md)
- [实机验收清单](docs/VERIFICATION.md)：用例与判据已定义；用例 A 已真机通过（2026-11-18），B–E 尚未执行，不能作为整体或 B–E 的通过证据
- [贡献与离线测试](CONTRIBUTING.md)

Copyright (C) 2026 arknights-auto-try contributors。

本项目代码以 **GNU Affero General Public License v3.0（AGPL-3.0）** 发布，见 [LICENSE](LICENSE)。分发或修改本项目时须遵守 AGPL-3.0 的条款，包括向网络交互用户提供对应源码。MAA、游戏资源及其他第三方材料不因此改变许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
