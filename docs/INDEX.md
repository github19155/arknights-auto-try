# 文档索引

每份文档只负责一个问题；跨文档只引用，不重复展开。

| 文档 | 负责回答 |
|---|---|
| [README](../README.md) | 安装、启动、工具清单、执行边界与安全约束 |
| [CONTRIBUTING](../CONTRIBUTING.md) | 贡献流程、离线测试与实机授权边界 |
| [CHANGELOG](../CHANGELOG.md) | 公开版本变更记录 |
| [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md) | 第三方代码、资源来源与再分发许可 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 代码模块、数据流与结果边界 |
| [ROADMAP.md](ROADMAP.md) | 当前优先级、验收重点与明确不做的事 |
| [VERIFICATION.md](VERIFICATION.md) | 真机验收前置条件、用例 A–E、逐项判据与结果记录表 |
| [kb/combat_core.md](kb/combat_core.md) | 排轴常识：部署顺序、再部署、技力与首放 |
| [kb/local_pitfalls.md](kb/local_pitfalls.md) | 本机 MAA 接口、OCR/截图、胜负判定与退出踩坑 |

## 验证口径

- 离线 `unittest` / CI 通过只说明合成数据下的回归通过，**不等于真机通过**。
- 真机验收按 `VERIFICATION.md` 逐项执行；没有真实设备画面与证据文件的记录一律标 TODO，不得写成已验证。
- 该清单只使用 `exercise`；`official` 需要用户单独授权，且不属于验收范围。
