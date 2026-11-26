# 实机验收清单

> **状态：A 已通过（2026-11-18），B–E 未执行（TODO）。** 本文定义在真实设备上可重复执行的验收步骤与判据。
> 用例 A 是此前的真机演习记录，脱敏结果见第 9 节；首发版的模板配置变更只做离线验证。
> B–E 尚未执行，一律不得写成「已验证」；仓库的离线测试、CI 和静态检查通过也**不等于**真机通过。
> 其余用例保持 TODO，不得由 A 的结果外推。

## 1. 范围

覆盖正常关卡的 `exercise` 单关执行，共五个用例：

| 用例 | 验证目标 |
|---|---|
| A | 首波漏怪 → 中断、证据、退出确认、归位 |
| B | 三星胜利 → `battle_result="won"` 且三枚蓝色奖章复核通过 |
| C | 非三星/灰色行动结果 → 绝不判 `won` |
| D | 归位失败 → `REPOSITION_FAILED` 且战报仍落盘 |
| E | HTTP 非回环绑定门禁与无鉴权警告 |

不做的事：

- 不验证自动选关、连续推图、肉鸽/保全/悖论等特殊玩法。
- 不证明作业战术、账号安全或封禁风险。
- 不把「链完成」「停止自动化」「出现结算标题」当成胜利或退出。
- 不提交 `data/captures/` 产物；证据路径含盘符、用户名等，分享前需脱敏。

## 2. 前置条件

1. **环境**：Windows x64 + Python 3.13 独立环境，已安装 `requirements.lock`；CLI 从项目根目录执行。
2. **MAA/ADB**：按 [README](../README.md#实机执行的本地暂停模板) 配置 MAA、ADB 和本地暂停模板；先用 `see_screen` 确认截图正常。
3. **关卡与作业**：
   - 使用**非零理智的正常关**（`zero_sanity=false`）。零理智关卡在 `exercise` 下会被执行器自动升格为 `official`（不耗理智但仍属正式作战），会混淆模式语义，不要用于本清单。
   - 作业由执行者自备，先跑 `validate_copilot_plan` 或用 `cli.py validate` 确认站位/射程/部署约束通过；战术能否通关不由校验保证。
   - 执行前**手动停在目标关卡的详情面板**，核对关卡代号、难度与「开始行动」按钮。执行器不会自动选关。
4. **模式**：只允许 `exercise`（演习，不消耗理智）。禁止 `official`、`authorize_official=True`、`--mode official`、`campaign --mode official`；不得使用理智药或源石（执行器固定 `use_sanity_potion=False`）。
5. **录屏**：用例 A/B/C 建议 `record_video=True`，用于人工复核结算画面与漏怪时间锚点；D/E 不需要。
6. **备份与隔离**：
   - 每次运行使用独立输出目录，不覆盖历史产物。
   - 运行前备份 `data/captures/`（该目录已被 gitignore，只在本地保留）：

```powershell
New-Item -ItemType Directory -Force .\data\captures | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item .\data\captures ".\data\captures.bak-$stamp" -Recurse
```

7. **顺序**：一次只跑一个用例；每个用例结束后人工确认画面状态，再开始下一个。

## 3. 通用运行方式

### 3.1 MCP

`execute_stage_plan` 支持 `record_video` 与 `allow_leak`，用例 A/B/C 用它。参数示例：

```json
{
  "copilot_json_path": "C:/path/to/your/job.json",
  "mode": "exercise",
  "stage_code": "<STAGE>",
  "timeout": 600,
  "record_video": true,
  "allow_leak": false
}
```

不要传 `authorize_official`。省略输出目录时，结果写入 `data/captures/<stage>-<mode>-<时间戳>/`。

### 3.2 CLI

CLI 没有 `record_video` / `allow_leak` 参数；需要这两项时改用 MCP。

```powershell
.\.venv\Scripts\python.exe cli.py run <STAGE> C:\path\to\job.json --mode exercise --outdir .\data\captures\verify-A --timeout 600
```

### 3.3 产物与读取

每次运行的目标目录（下称 `<run>`）应包含：

| 文件 | 产生条件 | 用途 |
|---|---|---|
| `maa-events.jsonl` | 进入执行阶段后总是 | 原生回调 + runner 事件，逐行 `{"message": <int>, "details": "<JSON 字符串>"}` |
| `maa-run.json` | 进入执行阶段后总是（含失败/归位失败） | 结构化战报；本次验收的主要判据 |
| `review-manifest.json` | 进入执行阶段后总是 | 视频/战斗/漏怪时间锚点与 warnings |
| `battle.mp4` | `record_video=true` 且拉取成功 | 人工复核结算画面 |
| `leak-evidence-*.png` | 检测到漏怪并成功拼图 | 2×2 四宫格 |
| `leak-evidence-*.json` | 同上且 sidecar 写入成功 | 与 PNG 同名的 sidecar |
| `evidence-*.png` | 失败/未判胜且存在合格帧 | 失败证据四宫格 |
| `abandon-verify.png` | 放弃链复核第一帧 | 退出画面证据 |
| `abandon-failed-last.png` / `exit-unknown-last.png` | 放弃失败 / 退出未确认 | 最后画面 |
| `battle-exit-last.png` | 超时或运行期异常且退出未确认（非放弃失败、非 `EXIT_UNCONFIRMED`） | 最后画面；无 frozen 变体 |
| `abandon-failed-frozen-last.png` / `exit-unknown-frozen-last.png` | 放弃失败 / 退出未确认且无可用 last_screenshot | 冻结画面；`battle-exit-last` 没有对应变体 |
| `story-unconfirmed-last.png` | 战后剧情未推进 | 最后画面 |
| `review_frames/` | 调用 `review_battle_frames` 后 | 抽帧结果 |

读取事件时注意 `details` 是嵌套 JSON 字符串：

```powershell
$run = '.\data\captures\<run-dir>'
Get-Content "$run\maa-events.jsonl" | ForEach-Object {
  $e = $_ | ConvertFrom-Json
  $d = $e.details | ConvertFrom-Json
  '{0,6}  {1,-22} {2}' -f $e.message, $d.what, $d.why
}
```

读取战报关键字段：

```powershell
Get-Content "$run\maa-run.json" -Raw | ConvertFrom-Json |
  Select-Object ok,battle_result,program_status,win_evidence,native_battle,exit_confirmed,
                allow_leak,execution,sanity_guard,failure,leak,cleanup_warnings
```

字段口径：

- `battle_result`：只有 `won` 表示胜利；`abandoned_on_*` 只是中断/放弃记账；`unknown`、`failed` 都不算通关。`ended` 不是 `battle_result` 的取值，它只是退出/结算页裁决，见用例 C。
- `program_status`：运行状态。`completed` 只表示程序完成，`ok` 仍可能为假。
- `win_evidence`：`MarkBattleWon` 的三星复核结论，`verified` / `rejected` / `unverified`；只有 `verified` 才允许 `won`。
- `cleanup_warnings` 与 `cleanup_errors`：同一个收尾失败列表的两种名字；只代表证据/清单落盘问题，不参与 `program_status`，不得把已复核的胜利变成 `failed`。
- `failure.reposition_error`：归位失败原因；非空且无更强失败时状态应为 `REPOSITION_FAILED`。

## 4. 用例 A：首波漏怪中断

### 4.1 准备

- 在可稳定运行的作业基础上，让首波必经路线**无人阻挡**；保留至少一个后续动作（例如在 90s 对已部署干员使用技能或撤退），使首波到达时 Copilot 链仍在运行，否则链可能先结束、进入退出判断而不是漏怪中断。
- `mode="exercise"`，`allow_leak=false`（默认），`record_video=true`。
- 手动停在目标关卡详情面板。

### 4.2 命令

MCP 参数：

```json
{
  "copilot_json_path": "C:/path/to/leak-job.json",
  "mode": "exercise",
  "stage_code": "<STAGE>",
  "timeout": 600,
  "record_video": true,
  "allow_leak": false
}
```

CLI 等价（无录屏）：

```powershell
.\.venv\Scripts\python.exe cli.py run <STAGE> C:\path\to\leak-job.json --mode exercise --outdir .\data\captures\verify-A --timeout 600
```

### 4.3 预期事件（`maa-events.jsonl`）

| 事件 | 期望 |
|---|---|
| `CopilotAction` | 至少一条，证明战斗已开始 |
| `LeakDetected` | `message=-2`，`details.hp_before != details.hp_after` |
| `LeakEvidence` | `message=2`，`path` 与 `timeline` 非空，`frames` 为 4 格 |
| `AbandonVerify` | `verdict="exited"`；或 `AbandonResultPage` / `AbandonActionEnded` 且 `exit_allowed=true`（战斗其实已结束，不再放弃） |
| `RunnerWarning` | 允许出现；不得改变 `program_status` |
| `RepositionError` | 允许出现，但表示自动归位失败：按用例 D 记录并人工恢复；不得当成自动归位通过 |

### 4.4 `maa-run.json` 关键字段

| 字段 | 通过条件 |
|---|---|
| `ok` | `false`（漏怪中断不是胜利） |
| `battle_result` | `aborted_on_leak`；若原生失败页先出现而记为 `failed`，必须在记录中注明，且仍要求 `exit_confirmed=true` 与完整漏怪证据 |
| `program_status` | 不是 `ABANDON_FAILED`、`EXIT_UNCONFIRMED`、`failed`；`completed` 或 `unknown` 均可，以退出与漏怪事实为准 |
| `failure.reason` | `leak` |
| `failure.abandon_failed` | `false` |
| `failure.exit_confirmed` | `true` |
| `failure.reposition_error` | 通常为 `null`；非空表示自动归位失败，按用例 D 记录并人工恢复，不得当成自动归位通过 |
| `leak` | 非空；`hp_before` 与 `hp_after` 不同 |
| `leak.estimated` | `false`；为 `true` 说明缺 `life_total`，需在记录中说明 |
| `allow_leak` | `false` |
| `execution` / `allow_official` | `exercise` / `false` |
| `win_evidence` | `null`；若结算页已出现并被复核，则为 `rejected`（绝不 `verified`） |
| `cleanup_warnings` | 允许非空，但 `program_status` 不得因它变成 `failed` |

### 4.5 证据文件

- `leak-evidence-*.png`：2×2 四宫格，四格都应有画面。
- 同名 `leak-evidence-*.json`：至少含 `version`、`png`（仅文件名）、`stage_code`、`time_basis`、`leak`、`frames`；`frames` 有 4 项，含 `sampled_at`，且首扣血帧标记为 `first_change_pin`。
- `LeakEvidence.timeline` 必须指向该 sidecar；`leak_evidence_error` 为 `null`。
- 若放弃链走到画面复核，`abandon-verify.png` 是复核第一帧，用于确认「战斗确实已结束」；若走结算页分支，则没有该文件。
- 若 `record_video=true`，`review-manifest.json` 的 `leak_time` / `leak_battle_s` 与 `battle.mp4` 可用于按时间复核首扣血现场。

### 4.6 通过 / 失败判据

通过必须同时满足：

1. `exit_confirmed=true`，且 `failure.abandon_failed=false`。
2. `program_status` 从未落成 `ABANDON_FAILED` / `EXIT_UNCONFIRMED` / `failed`。
3. `LeakEvidence`、四宫格 PNG、同名 sidecar 全部存在且可解析。
4. 退出后归位到关卡详情面板（`see_screen` 或人工观察）；若自动归位失败（`failure.reposition_error` 非空），按 D 记录并人工恢复，恢复子项记为「人工恢复」，不得写成自动归位通过。
5. 若本次产生了 `RunnerWarning`，`program_status` 仍不是 `failed`；若未产生，本子项记「未覆盖（TODO）」，A 的其余判据照常记录，但不得把未覆盖写成通过。

失败：任一条不满足；尤其是漏怪后仍卡在战斗、需要人工强退、证据缺失，或用 `cleanup_warnings` 把状态判成 `failed`。

> 回归重点：旧缺陷是在战斗其实已结束（失败页/结算页）时仍把它当成活战斗，盲目点暂停后报 `ABANDON_FAILED`。本次只要出现 `program_status="ABANDON_FAILED"` 或 `failure.abandon_failed=true`，即判该用例失败，不因后续人工退出而改写。

## 5. 用例 B：三星胜利

### 5.1 准备

- 使用一份已在演习中稳定三星的作业；先 `validate`。
- `mode="exercise"`，`allow_leak=false`，`record_video=true`。
- 手动停在目标关卡详情面板。

### 5.2 命令

```json
{
  "copilot_json_path": "C:/path/to/stable-3star.json",
  "mode": "exercise",
  "stage_code": "<STAGE>",
  "timeout": 600,
  "record_video": true,
  "allow_leak": false
}
```

### 5.3 预期事件

| 事件 | 期望 |
|---|---|
| `LeakDetected` | 不存在 |
| `BattleWonEvidence` | `verdict="verified"`、`three_star=true`、`scene="not_battle"`、`frame=true` |
| `BattleWonUnproven` | 不存在 |
| `BattleWonUnverified` | 不存在 |
| `MarkBattleWon` | 在原生回调中出现（位于 `details.task`），且被当前帧三枚蓝色奖章复核 |

### 5.4 `maa-run.json` 关键字段

| 字段 | 通过条件 |
|---|---|
| `ok` | `true` |
| `battle_result` | `won` |
| `program_status` | `completed` |
| `win_evidence` | `verified` |
| `native_battle` | `won` |
| `exit_confirmed` | `true` |
| `failure` | `null` |
| `reposition_error` | `null` |
| `allow_leak` | `false` |
| `execution` / `allow_official` | `exercise` / `false` |
| `evidence_png` | `null`（胜利不生成失败证据图，这是预期） |

### 5.5 通过 / 失败判据

通过：

1. 上表全部成立，且 `BattleWonEvidence.three_star=true`。
2. `battle.mp4` 结尾的结算页人工可见**三枚蓝色奖章**；建议用 `review_battle_frames(run_dir=<run>, start_s=<结算附近战斗秒>)` 抽帧留证，或直接播放录像截图。若用工具抽帧，结算战斗秒可由 `review-manifest.json` 的 `battle_start`（视频秒）与录像时长换算：`start_s ≈ 视频时长 - battle_start - 1`，再按返回的 `request_delta_s` 核对偏差。
3. 运行结束后回到关卡详情面板。

失败：`ok=false`、`win_evidence != "verified"`、出现 `LeakDetected`，或结算页奖章不足三枚。注意：如果战斗已判 `won` 但战后归位失败，`program_status` 会变成 `REPOSITION_FAILED`、`ok=false`——胜负记录仍在，但本用例按 B 失败记录，并单独按 D 记录归位问题。

## 6. 用例 C：非三星 / 灰色行动结果

### 6.1 准备

- 在稳定三星作业基础上，撤掉或不再部署**一条支路**的阻挡，让 1–2 个敌人漏过，其他输出仍能清场；先演习确认结算为 2 星。
- 必须设置 `allow_leak=true`，否则执行器会在首次扣血时中断，变成用例 A。
- `mode="exercise"`，`record_video=true`。
- 若实际跑成 3 星或直接失败，属于用例搭建不合格：调整作业后重跑，不要拿这次结果当 C 的通过证据。

### 6.2 命令

```json
{
  "copilot_json_path": "C:/path/to/two-star.json",
  "mode": "exercise",
  "stage_code": "<STAGE>",
  "timeout": 600,
  "record_video": true,
  "allow_leak": true
}
```

### 6.3 预期事件

| 事件 | 期望 |
|---|---|
| `LeakDetected` | 存在（`allow_leak=true` 只记录、不中断） |
| `BattleWonEvidence` | `verdict="rejected"`、`three_star=false`、`scene="not_battle"` |
| `BattleWonUnproven` | 存在，`why` 说明只认定战斗结束，不判胜利 |
| `BattleWonUnverified` | 不存在 |
| `MarkBattleWon` | 可能出现，但最终必须被降级 |

`ended` 可能只出现在 `ExitDecision.result_page`，也可能因为 `MarkBattleWon` 已在战斗标记里而被直接降级、完全不写 `ExitDecision`。无论哪条路径，`maa-run.json` 里都没有 `battle_result="ended"` 这个取值；以 `battle_result=unknown`、`win_evidence=rejected` 与 `BattleWonUnproven` 为准。

### 6.4 `maa-run.json` 关键字段

| 字段 | 通过条件 |
|---|---|
| `ok` | `false` |
| `battle_result` | `unknown`（绝不 `won`） |
| `program_status` | `completed`（若归位失败则为 `REPOSITION_FAILED`，单独记录） |
| `win_evidence` | `rejected` |
| `native_battle` | `unknown` |
| `exit_confirmed` | `true` |
| `failure.reason` | 以 `battle_unknown` 开头（不是 `leak`） |
| `leak` | 非空（本用例的搭建方式会漏怪） |
| `evidence_png` | 指向 `evidence-*.png` |
| `execution` / `allow_official` | `exercise` / `false` |

### 6.5 通过 / 失败判据

通过：

1. `battle_result != "won"`，`win_evidence != "verified"`，`native_battle != "won"`，`ok=false`。
2. `BattleWonUnproven` 存在，且 `BattleWonEvidence.three_star=false`。
3. `evidence-*.png` 存在；录像中结算页为 1/2 星或灰色奖章，与判定一致。
4. 战斗已结束且 `exit_confirmed=true`，未卡在活战斗。

失败：任何路径产出 `won`；把 `ended` 写成 `battle_result`；无证据却报完成；或把 `allow_leak=true` 当作推荐运行方式留在常驻配置中。

## 7. 用例 D：归位失败

### 7.1 准备（可控方式：错误面板）

- 把游戏停在**主界面**或其他不含目标关卡代号/「开始行动」的界面，不要停在关卡详情面板或关卡列表（后者可能被归位链恢复，无法稳定制造失败）。
- 保持正常设备连接；执行器会在开战前的 `BattleReadyCheck` 发现不在面板，触发 `AutoReposition`，归位链无法确认面板后报 `RepositionError`。
- 本次不会开战，不会消耗理智。
- 「故意遮挡」变体必须在**模拟器画面内**（例如游戏内公告/弹层盖住关卡标题与按钮）；桌面透明窗口不会进入 ADB 截图，不能用于本用例。

### 7.2 命令

```powershell
.\.venv\Scripts\python.exe cli.py run <STAGE> C:\path\to\job.json --mode exercise --outdir .\data\captures\verify-D --timeout 600
```

MCP 等价：`mode="exercise"`，`record_video` 可省略。

### 7.3 预期事件

| 事件 | 期望 |
|---|---|
| `AutoReposition` | `message=-2`，`why` 为「前置检查未确认关卡详情面板」 |
| `RepositionError` | `message=-1`，`why` 含「自动归位到关卡 <STAGE> 详情面板失败」与「最后截图：...」 |
| `CopilotAction` | 不存在（战斗没有开始） |
| `LeakDetected` / `AbandonVerify` | 不存在 |

### 7.4 `maa-run.json` 关键字段

| 字段 | 通过条件 |
|---|---|
| `program_status` | `REPOSITION_FAILED` |
| `ok` | `false` |
| `battle_result` | `unknown` |
| `failure.reason` | `reposition_failed` |
| `failure.reposition_error` | 非空，包含最后截图路径与归位日志路径 |
| `failure.exit_confirmed` | `false` |
| `failure.abandon_failed` | `false` |
| `runtime_error` | `null`（归位失败是独立事实，不重复记成通用异常） |
| `report` | 非空；`report.reason` 以 `battle_unknown` 开头；无可用帧时带 `no_valid_frames` |
| `evidence_png` | 允许为 `null`（未开战没有战斗帧），不伪造证据 |
| `events_path` / `report_path` | 均存在且可读 |

### 7.5 通过 / 失败判据

通过：

1. `maa-run.json` 已落盘且可解析，`program_status="REPOSITION_FAILED"`。
2. `failure.reposition_error` 非空，能据此找到最后截图；把该截图复制进本次 `<run>` 便于留档。
3. `ok=false`，没有把归位失败写成 `completed`，也没有吞掉 `maa-events.jsonl`。
4. 人工从最后截图确认设备当前画面，按需手动回到关卡面板。

失败：异常直接冒泡导致没有 `maa-run.json`；状态落成 `failed` 或 `completed`；`failure.reposition_error` 为空；或声称已成功归位。

> 战后归位失败（战斗已结束、结算后归位报错）是同一判据的变体，但时窗很短、难以从外部稳定制造；本次不将其列为必跑项，留作 TODO。D 用例先用开战前归位失败证明「战报不丢 + 状态可见」。

## 8. 用例 E：HTTP 非回环绑定门禁

### 8.1 拒绝绑定（无 `--allow-remote`）

```powershell
.\.venv\Scripts\python.exe mcp_server.py --http --host 0.0.0.0 --port 18765
$LASTEXITCODE
Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue
```

期望：

- stderr 出现「拒绝以 --http 绑定非回环地址 '0.0.0.0'：HTTP 模式没有应用层认证……确需远程访问请显式加 --allow-remote」。
- 退出码为 `2`。
- 没有监听套接字，`Get-NetTCPConnection` 无输出。

### 8.2 放行并观察无鉴权警告

```powershell
.\.venv\Scripts\python.exe mcp_server.py --http --host 0.0.0.0 --allow-remote --port 18765
```

期望：

- 启动前出现 WARNING：「HTTP 绑定非回环地址 0.0.0.0 且无应用层认证：任何可达者都能调用全部 MCP 工具」。
- 随后出现「starting streamable-http on 0.0.0.0:18765/mcp」，进程保持运行。
- 端口可连接且不需要任何凭据；本步只验证「无鉴权」这一事实，不从不可信网络发起调用。
- 检查完立即 `Ctrl+C`，确认端口关闭。

安全边界：该测试会短暂暴露实机控制入口。只在受信内网或在本机防火墙阻断外部访问的情况下进行；不要绑定到公网、不要使用公司/校园共享网络、不要留在后台运行。

### 8.3 通过 / 失败判据

通过：

1. 无 flag 时退出码为 2，且未监听端口；错误文案明确指出缺少 `--allow-remote`。
2. 带 flag 时启动成功，并明确打印「无应用层认证」警告。
3. 测试结束后端口已关闭。

失败：无 flag 也能启动；带 flag 没有警告；错误信息没有说明风险；或测试后残留监听进程。

### 8.4 记录内容

本用例不产生 `maa-run.json`、游戏事件流或游戏截图。记录以下内容即可：

```powershell
New-Item -ItemType Directory -Force .\data\captures\verify-E | Out-Null
# 拒绝绑定：把 stderr 连同退出码写入本地记录
.\.venv\Scripts\python.exe mcp_server.py --http --host 0.0.0.0 --port 18765 2>&1 |
  Tee-Object .\data\captures\verify-E\reject.txt
"exit=$LASTEXITCODE" | Tee-Object .\data\captures\verify-E\reject.txt -Append
Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue |
  Tee-Object .\data\captures\verify-E\reject-listen.txt
```

放行启动时同样把 stderr 重定向到 `data/captures/verify-E/allow.txt`，检查完 `Ctrl+C` 后再确认一次端口已关闭。记录中写明：命令、退出码、警告原文、监听检查结果、启动与停止时间。

## 9. 结果记录

每条记录至少包含：命令/工具调用、`<run>` 路径、`maa-run.json` 关键字段、关键事件、证据文件路径、通过/失败。**未执行就写 TODO，不得预填「通过」。**

| 用例 | 日期/执行人 | 关卡 + 作业（脱敏） | 命令/工具调用 | `<run>` | `maa-run.json` 关键字段 | 关键事件 | 证据路径 | 结果 |
|---|---|---|---|---|---|---|---|---|
| A 漏怪中断 | 2026-11-18 / 真机演习 | FC-EX-7 | `cli.py run FC-EX-7 <job.json> --mode exercise --timeout 320`（`allow_leak=false`，`record_video=false`） | 本地记录 | `ok=false`、`battle_result=aborted_on_leak`、`program_status=completed`、`exit_confirmed=true`、`failure.reason=leak`、`failure.abandon_failed=false`、`failure.reposition_error=null`、`win_evidence=rejected`、`native_battle=unknown` | `LeakDetected` → `LeakEvidence` → `AbandonResultProbeWait` → `ResultWinCheck` → `AbandonActionEnded`，无 `AbandonProbeTimeout` | 四宫格及同名 `.json`、`maa-events.jsonl`、`maa-run.json`、`review-manifest.json`（私有留存） | PASS（`RunnerWarning` 未覆盖） |
| B 三星胜利 | TODO | | | | | | | TODO |
| C 非三星 | TODO | | | | | | | TODO |
| D 归位失败 | TODO | | | | | | | TODO |
| E HTTP 门禁 | TODO | | | | | | | TODO |

补充记录建议：

- 记录实际使用的 MAA/游戏版本与设备序列号（内部留存，公开前脱敏）。
- 记录每次运行的 `wall_s`、`timeout` 与实际耗时，便于区分超时和战术失败。
- 若出现 `RunnerWarning`，原样记录 `why`；不要为了「干净」删除证据或手工改 `maa-run.json`。

## 10. 判读原则

1. **离线 ≠ 真机**：`unittest`、CI、静态校验通过都不能替代本文任一用例；没有真实设备、真实画面和证据文件的记录，不算验收。
2. **三样齐全才算一次有效记录**：`maa-run.json` + `maa-events.jsonl` + 对应截图/录像；缺一项就标 TODO 或失败。
3. **不美化状态**：`unknown`、`ended`、`failed`、`abandoned_on_*` 都不是胜利；`completed` 也不等于 `ok`。
4. **不掩盖归位失败**：`REPOSITION_FAILED` 必须保留 `failure.reposition_error` 与最后截图，不用重跑覆盖。
5. **收尾警告只丢证据**：`RunnerWarning` / `cleanup_warnings` 不得导致 `program_status=failed`；若发生，按缺陷记录。
6. **不扩大到正式作战**：本清单全部在 `exercise` 下执行；任何 `official` 都需要用户另行明确授权，且不属于本清单。
