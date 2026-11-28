# 国服角色、物品过滤器与物价

首次交互使用 CLI 时选择 **cn（国服）** 或 **international（国际服）**，可以随时运行 `uv run poe2 init cn` 或 `uv run poe2 init international` 切换。下载镜像与游戏区服分别设置。CLI 与 MCP 共用区服选择，各区服分别保存角色槽。

## 把自己的国服角色导入 PoB2

1. 在 WeGame 的《流放之路：降临》专区打开「数据 → 角色」，选择角色，用「一键分享」复制链接。[WeGame 游戏助手](https://www.wegame.com.cn/helper/poe2/)
2. 把链接粘进 PoE2DB 的「一键分享」输入框并提交。转换由第三方 PoE2DB 提供，页面会列出已转换的角色。打开自己的角色页，复制网址或点 **PoB Code** 旁的 **Copy**。[国服角色转换页](https://poe2db.tw/cn/account)
3. 在项目目录用下面的命令载入转换好的角色页（示例为已公开的角色），也可以传入完整的 PoB Code，再做配装审计。已有桌面 PoB2 存档也可用 `uv run poe2 pob import-local --list` 查找。

```sh
uv run poe2 init cn
uv run poe2 pob import-character "https://poe2db.tw/cn/qq_build?name=游侠005" --json
uv run poe2 pob audit --json
```

2026-11-27 用一个公开国服导出码实测，项目的本地引擎识别出 96 级锐眼、冰霜射击、1,416 生命和 1,172 能量护盾。同页的 `.build` 是规划器文件；在这个样本中，用它导入后护盾为 0，主技能也不同。做装备和伤害分析应选 **PoB Code**，并核对珠宝、装备和主技能。输入原始 WeGame 分享链接时，工具会提示先到 PoE2DB 转换。[测试角色的两种导出](https://poe2db.tw/cn/qq_build?name=%E6%B8%B8%E4%BE%A0005)

## 从现有过滤器开始

优先沿用你当前满意的 `.filter` 和配套音效。第一次使用可从 **NeverSink 的 PoE2 半严格版**开始。另存为自己的文件，再针对要保留的底材、想听的音效或隐藏规则逐项修改。国服中文名可用于解释规则；文件中的 `BaseType`、`Class` 保留底稿使用的内部名称。[NeverSink PoE2](https://github.com/NeverSinkDev/NeverSink-Filter-for-PoE2) · [国服过滤器作者的格式说明](https://github.com/panyanyu/Poe2FilterEditor)

```sh
uv run poe2 filter restyle semi_strict --base-only --out my-guofu.filter
uv run poe2 filter validate my-guofu.filter --json
```

用已有文件时，将 `semi_strict` 换成原 `.filter` 的路径，另取输出文件名。底稿本身可能来自国际服；保留原分级只表示保留作者的判断，不代表取得了国服价格。

在 WeGame「助手设置 → 游戏辅助」关闭「物品掉落过滤」，再从**国服客户端**的设置打开过滤器目录。把自己的 `.filter` 放到该目录顶层，按原相对路径放好自定义音效，在游戏里选择并重载副本。WeGame 会还原其管理的 `WeGameFilters` 目录；使用个人副本可以保留改动。命令行校验后，还需要在实际国服客户端确认载入和声音。[《流放之路：降临》官方过滤器助手说明（17173 转载）](https://news.17173.com/content/09122025/104535435.shtml)

## 国服物价能接到什么程度

| 来源 | 可用范围 | 产品接入建议 |
| --- | --- | --- |
| 腾讯官方市集 | 玩家可登录网页查找国服挂单。本次匿名访问被带到登录页；未找到公开授权给第三方应用读取国服挂单的接口文档。GGG 的角色 API 也未列出腾讯区服。 | 从工具打开官方市集，由玩家查看报价、复制装备。直接查询国服稀有装备挂单和账号角色，需要确认腾讯提供的接口和使用范围。[腾讯市集](https://poe.game.qq.com/trade2) · [GGG 开发文档](https://www.pathofexile.com/developer/docs/reference#characters) |
| POE 通货经济数据库 | 社区 API 的 `GET /api/summary?version=2` 提供国服 PoE2 交易所物品的中英文名、买卖价、单位、异常标记和北京时间。汇总缓存一小时。2026-11-27 匿名读取成功，返回 20 类、559 条，其中 151 条标有异常。 | 适合先验证国服通货过滤分级；保留赛季、采样时间和计价单位。异常、缺失或过期的数据应保留底稿分级。站方声明仅限个人非商业使用，托管网站展示、缓存或分发前需确认授权。该数据源不提供单件稀有装备的可买挂单。[API 文档](https://poecurrency.top/api.html) · [站方使用说明](https://poecurrency.top/) |

目前 poe2brain 的内置实时市集、经济数据和 poe.ninja 榜单来自国际服。国服玩家可以在本地解析中文装备、比较 PoB2 配装、编辑过滤器；价格建议应显示来源、赛季与时间。

如需托管国服行情，先向数据提供方确认可用字段、刷新频率、缓存期限与公开展示权。若要直接对接腾讯，可通过 WeGame 开发者联络渠道请求转介《流放之路：降临》团队，分别询问玩家主动分享的角色数据和只读市集数据授权。[数据提供方](https://poecurrency.top/) · [WeGame 开发者联系方式](https://developer.wegame.com/developer/static/faq_cn.html)
