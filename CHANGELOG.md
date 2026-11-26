# 更新记录

## 开源首发清理

- 首发快照不包含旧提交、运行记录、个人配置或游戏界面模板。难度与活战斗识别复用本机 MAA 资源，暂停模板由使用者本地配置。
- 离线暂停识别测试用代码绘制控件，不再依赖任何实机截图的像素。

## 真机验收：用例 A 通过（2026-11-18）

- 在真机演习（FC-EX-7，v19 作业，内部私有）按 `docs/VERIFICATION.md` 用例 A 验证首波漏怪中断并通过：约 117s 检测到漏怪，`LeakDetected` / `LeakEvidence` 产出四宫格 PNG 与同名 sidecar；放弃链等待结算渲染 2.5s 后，`ResultWinCheck` 以 OCR「行动结束」0.990 确认战斗已结束，`AbandonActionEnded` 记录 `exit_allowed=true`，未落成 `ABANDON_FAILED`，也无 `AbandonProbeTimeout`。
- 战报关键字段：`ok=false`、`battle_result=aborted_on_leak`、`program_status=completed`、`exit_confirmed=true`、`failure.abandon_failed=false`、`failure.reason=leak`、`failure.reposition_error=null`、`win_evidence=rejected`、`native_battle=unknown`、`leak.estimated=false`；运行结束后回到关卡详情面板。
- 本次没有录屏，`RunnerWarning` 子项未覆盖。B–E 仍未执行，退出/恢复链不得据此视为整体通过。

## 可复现依赖安装

- 新增 `requirements.lock`：由 `uv pip compile requirements.txt -o requirements.lock --python-version 3.13 --python-platform windows` 生成，固定 4 个直接依赖与 29 个传递依赖（共 33 个包）的版本。
- CI 与 README / CONTRIBUTING 改按 `requirements.lock` 安装；`requirements.txt` 保留为直接依赖声明（锁文件的输入源），不作为安装入口。
- 全新 Python 3.13 虚拟环境从锁文件安装后，离线套件 274 项通过、1 项跳过。

## 初始公开版本

- 提供关卡情报、Copilot 作业校验、MAA 单关执行与战斗证据复核的 MCP / CLI 入口。
- 本地配置、干员数据、缓存、个人作业和录制记录与公开源码分离。
- 提供独立的离线回归测试、配置示例与第三方来源说明。
- 当前面向人工监督使用，退出与恢复可靠性尚未完成实机验收。
- 修复结算页「行动结束」被误当作活战斗、继续盲点暂停而导致 ABANDON_FAILED；现在只确认战斗结束与退出，不伪造胜负。
- 结算页「行动结束」不再直接判胜：需三枚蓝色奖章视觉证据；无证据时仅确认战斗结束，battle 保持 unknown。
- 原生 MarkBattleWon/MarkBattleFailed 优先于漏怪/放弃记账；归位失败以 REPOSITION_FAILED/failure.reposition_error 暴露，不再吞掉战报。

## 可靠性与公开验收基线

- HTTP 绑定门禁：`--http` 默认只绑回环地址；绑定非回环地址必须显式加 `--allow-remote`，否则拒绝启动并返回退出码 2。放行时打印「无应用层认证」警告；该开关不提供认证、TLS 或来源限制。
- 收尾警告单独成列：新增 `cleanup_warnings`（与 `cleanup_errors` 同一清单），录屏拉取、复核清单/sidecar 写入、停止 MAA 等收尾失败只写 `RunnerWarning`，不参与 `program_status`，已复核的胜利不会因丢证据被判成 `failed`。
- 漏怪证据 sidecar 保留：四宫格 PNG 旁落同名 `leak-evidence-*.json`，含 `version`、`png`（仅文件名）、`stage_code`、`time_basis`、`leak`、`frames`（每格含 `sampled_at`，首扣血帧标记 `first_change_pin`）。sidecar 写失败只记 `sidecar_error` / `leak_evidence_error`，不重复拼图，也不产出孤儿 PNG。
- MCP stdout 隔离：stdout 只保留 JSON-RPC；原生 MAA 日志与 `src/planner/intel` 的缓存/告警日志只写 stderr。库路径失败抛异常而非 `SystemExit`，由 MCP 工具或 CLI 统一转成结构化错误。
- 合成素材：离线识别与回归测试使用项目自行生成的几何图像，不含游戏原画、界面截图、立绘、账号或设备信息。
- CI：GitHub Actions `windows-latest` + Python 3.13，在 push 与 pull request 上运行 `unittest discover`，固定 `INTEL_ACCESS_ONLINE=0`；CI 不安装 MAA、不连接设备、不启动服务。
- 集成测试：新增 `run_copilot` 端到端离线集成测试（假 Asst + 临时目录 + 合成截图），覆盖漏怪中断与收尾 warning、三星胜利复核、灰章/抓帧失败对照，以及主动停止引发的 `TaskChainError` 只降级为诊断事件。
- 放弃失败后的结算页复查改为「先等渲染、先试胜利页」：放弃链在「行动结束」渲染完成前就启动，旧实现立刻跑失败优先的结果探测，往往只发出 `ResultFailedCheck`（未命中），第二条 `ResultWinCheck` 还会因上一条链未停而 append 被拒。现在先等 `ABANDON_RESULT_RENDER_WAIT`（2.5s），先跑 `ResultWinCheck`（25s，命中经 `_won_verdict` 复核奖章但只作退出），未命中再跑 `ResultFailedCheck`（20s）；两次探测之间确认上一条链已停并留短间隔，`AbandonProbeAppend` / `AbandonProbeHit` / `AbandonProbeStop` / `AbandonProbeTimeout` 记录真实 append 的 taskid 与超时，便于复盘时序。
- 验收清单：`docs/VERIFICATION.md` 定义 A–E 五条真机用例（漏怪中断、三星胜利、非三星、归位失败、HTTP 门禁）及判据、产物表和记录模板；当时状态为未执行 TODO，离线测试与 CI 通过不等于真机通过（2026-11-18 用例 A 已通过，见上）。
