# 当前架构

本项目把单关情报、作业校验、MAA 执行和证据复核暴露给客户端。战术选择由客户端或用户完成，执行器不保证作业能通关。没有关卡浏览式自动选关；首次执行须手动到达目标关卡面板。

## 模块与数据流

1. [mcp_server.py](../mcp_server.py) 提供 FastMCP 工具，默认 stdio，也支持回环 HTTP；[cli.py](../cli.py) 提供 CLI。`campaign` 读取关卡画像并调用单关执行，不选关、不连续推图。导入时即把 fd 1 改道 stderr：stdout 只走 JSON-RPC，库路径的缓存/告警日志同样只写 stderr，失败抛异常而非 `SystemExit`。
2. [intel.py](../src/planner/intel.py)、[intel_access.py](../src/planner/intel_access.py) 与 [prts_stage_client.py](../src/planner/prts_stage_client.py) 读取/下载关卡数据，解析难度、地图、路线、波次、敌人和参考作业。`stage_profile` 统一关卡身份、零理智与系统编队等属性；同名变体按具体身份区分缓存。
3. [validator.py](../src/engine/validator.py) 检查 Copilot 作业的站位、射程、部署约束及名单一致性。[runner.py](../src/engine/runner.py) 在设备连接前再次强制校验作业与请求关卡身份及部署合法性，不依赖调用方先执行校验工具。
4. 执行器加载独立安装的 MAA、[原生任务 overlays](../tools/maa-resources/)，连接设备后运行 Copilot，并采样画面、生命变化及原生事件。漏怪、超时或异常时尝试停止和放弃；仅有退出证据才允许面板恢复。
5. [recorder.py](../src/engine/recorder.py) 解析事件并生成失败证据；[battle_review.py](../src/engine/battle_review.py) 从可选录像按时间抽帧，关联动作及时间偏差。客户端读取结构化结果与图像后决定是否重试。

## 辅助模块

- [navigator.py](../src/engine/navigator.py)：当前面板探测、战后/剧情归位及截图/点击；靠识别关卡代号文本打开目标面板，不浏览关卡列表，失败即报错。
- [tile_projection.py](../src/engine/tile_projection.py)：战场格与屏幕坐标正逆投影。
- [teach_recorder.py](../src/engine/teach_recorder.py)：只读监听 ADB 触控并截图，生成待复核的示教动作；推算格子不是已验证部署。
- [doc_fetcher.py](../src/planner/doc_fetcher.py)：读取 [combat_core](kb/combat_core.md)、[local_pitfalls](kb/local_pitfalls.md)，按需缓存 MAA 协议及 PRTS 词条。
- [special_modes.py](../src/engine/special_modes.py)：特殊玩法的 MAA 桥接，不属于当前单关可靠性验收范围。

## 数据与结果边界

`data/` 是本地输入和运行产物，不是随仓库分发的个人资料。`data/operators.json` 提供拥有列表，由用户放入自己的 MAA 导出文件，不实时扫描账号；缺失时列表为空。可通过参数注入系统预设编队等上下文。实际技能等级、专精、模组保持未知；理论 `max` 与账号实际值分列。

关卡表、地图、文档和词条按需联网下载后缓存在 `data/`。每次执行的事件、报告、截图及可选视频保存在 `data/captures/`。`review-manifest.json` 记录视频、战斗和漏怪时间锚点；缺失或估算的时间依据不能冒充准确观测。

实际作战的成功需要程序完成及原生胜利证据；`formation` 的完成只代表编队处理。`MarkBattleWon` 只说明结算页出现：只有 `not_battle` 当前帧的三枚蓝色奖章（`win_evidence="verified"`）才判 `won`；画面离开战斗但无奖章时划掉标记、按 `ended` 处理；抓帧失败/画面未知时保留标记待人工复核，`verified_native_battle` 仍把它降为 `unknown`，绝不把通用结算页当胜利。结算 OCR 同时认「行动结束」与「行动结果」。原生 `won`/`failed` 优先于漏怪/放弃记账：`aborted_on_leak`、`abandon_failed` 只在没有原生胜负时出现，`ended`/`unknown` 一律不判胜。退出未确认、放弃失败或未知胜负均不是成功。放弃失败时 `failure.reason="abandon_failed"`，漏怪事实仍保留在 top-level `leak`；结算后自动归位失败不丢战报，`failure.reposition_error` 记录原因并补写 `RepositionError` 事件（补写失败只进 `cleanup_errors`，不再抛出），没有更强失败时 `program_status="REPOSITION_FAILED"`、`ok=false`，`battle` 为 `unknown`/`ended` 时同样适用。漏怪四宫格与结算标记互不依赖：`MarkBattleWon`/`MarkBattleFailed` 不会让证据生成被跳过；拼图只尝试一次，失败原因记入报告的 `leak_evidence_error`，不重复产出事件或孤儿 PNG。MCP 仅在有剧情证据时尝试一次剧情恢复，不自动重跑作业。当前退出/归位路径仍需实机验证，不能据此承诺…
