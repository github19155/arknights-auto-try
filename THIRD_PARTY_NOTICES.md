# 第三方来源与权利说明

Copyright (C) 2026 arknights-auto-try contributors.

本项目源码采用 [GNU AGPL-3.0](LICENSE)。该许可证不改变下列第三方项目、游戏内容、外部数据或用户输入各自的权利归属。

## MAA

- 项目：[MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)
- 许可证：[AGPL-3.0](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev-v2/LICENSE)
- 用途：通过用户自行安装的 MaaCore 与 Python 绑定执行任务，复用其 Copilot 协议和原生识别任务。

仓库不分发 MaaCore、MAA 安装包或 MAA 图像模板。`tools/maa-resources/` 提供与本机 MAA 配合使用的增量任务定义；运行时从用户自行安装的 MAA 加载暂停按钮、主线难度等原生资源。分发 MAA 或其修改版时，应保留相应许可证、来源和版权声明，并履行适用的源码提供义务。

## 《明日方舟》游戏内容与界面素材

《明日方舟》的名称、图形、游戏界面及其他游戏内容的权利属于鹰角网络及相关权利人。本项目与游戏官方无隶属或背书关系。

仓库不分发真实游戏截图、游戏界面模板、原画或立绘。`tests/assets/` 中的离线图像为项目生成的合成图；测试用暂停与播放控件由测试代码绘制，见 [测试素材说明](tests/assets/NOTICE.md)。用户自行准备的暂停模板和运行产生的截图、录屏留在本地，不随项目代码授予许可。

## 按需访问的外部数据

- [ArknightsGameData](https://github.com/Kengxxiao/ArknightsGameData)：游戏数据表和关卡数据。
- [PRTS Wiki](https://prts.wiki/)：机制说明、词条及敌人视觉资料。
- [MAA 作业站](https://prts.maa.plus/)：参考 Copilot 作业。
- [MAA 文档](https://docs.maa.plus/)：协议与接入说明。

这些服务的完整数据集、下载缓存及第三方作业没有随仓库提供。运行时获取的内容遵循其原始条款和作者权利；引用、修改或转发作业时请保留原作者和来源，不要视为本项目原创内容。

## Python 依赖

Python 依赖由 `requirements.txt` 声明，通过包管理器单独安装，遵循各自许可证。本仓库不捆绑 Python 环境或第三方依赖包。
