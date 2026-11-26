# tests/assets 素材说明

本目录中的 PNG 是为离线回归测试生成的几何合成图，不含真实游戏画面。

## 合成图片

- 战斗暂停与播放控件由 `tests/battle_control_fixtures.py` 在测试中绘制，不随运行资源加载。
- `battle-snow-1.png`、`battle-snow-2.png`
- `result-cleared.png`、`result-failed.png`、`result-ended-grey.png`
- `life-1.png`、`life-2.png`、`life-3.png`
- `battle-operator-panel.png`

这些图像只有几何形状、色块与合成占位元素（生命计数、干员卡片外框、结算标题和奖章圆点等），不含游戏原画、立绘、账号或设备信息。

## 权利声明

- 本目录图片为本项目原创合成素材，随本项目代码以 AGPL-3.0 提供。
- 游戏名称、界面、图形及其他内容的权利属于鹰角网络及相关权利人；本项目不主张任何所有权。合成图片只复现测试所需的界面布局，不包含游戏美术资源。

代码与文档的许可见仓库根目录的 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`。
