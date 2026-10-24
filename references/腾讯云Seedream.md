# 腾讯云 TokenHub 上的 Seedream：规矩和更新

2026-10-24 按腾讯云官方文档和实测整理。价格、型号、状态会变：**每次以 `--models` 查到的和官方文档为准**，这页对不上时照新的来，并提醒用户。

## 官方文档（要核对时先看这几页）

| 看什么 | 链接 |
| --- | --- |
| Seedream 生图调用指南（接口、参数） | https://cloud.tencent.com/document/product/1823/136609 |
| 图像生成模型一览（型号名、同步还是异步） | https://cloud.tencent.com/document/product/1823/130080 |
| 价格 | https://cloud.tencent.com/document/product/1823/130055 |
| 快速入门（开通、实名、API Key） | https://cloud.tencent.com/document/product/1823/130058 |
| 控制台：在线推理（开后付费）、API Key 管理 | https://console.cloud.tencent.com/tokenhub |

## 型号（2026-10-24）

| 型号 | 状态 | 一张多少钱 | 实测 |
| --- | --- | --- | --- |
| `seedream-image-v5.0-pro` | 正常 | 竖版 1728×2304 是 60000 tokens＝0.6 元；261 万像素以内 0.3 元；多一张参考图加 2000 tokens（0.02 元，第一张免费） | 字最准，默认用它；70—130 秒 |
| `seedream-image-v5.0-flash` | 正常 | 12000 tokens＝0.12 元 | 30—40 秒，版式接近 Pro，8 张里 2 张把星期写错；只用来试风格，发出去前逐字核对 |
| `seedream-image-v5.0-lite` | 快下线了（模型广场标 11 月 24 日下线） | 0.22 元 | 不要用 |

查最新型号和状态：`python scripts/seedream_tokenhub.py --models`（免费，不出图）。状态三种：online 正常、pre-offline 快下线了、discontinued 已经停了。

## 接口要点

- 地址 `POST https://tokenhub.tencentmaas.com/v1/wand/si-image/generation`，同步返回图，不用查进度。请求头 `Authorization: Bearer 钥匙`。
- 尺寸：Pro、Flash 写 `1K`、`1.5K`、`2K` 或 `宽x高`（不能混写）；竖版用 `1728x2304`，横版 `2304x1728`，公众号封面 `2560x1088`。
- 参考图最多 10 张，整个请求不超过 20MB；参考图会被当作要学的样子，所以样张、人脸参考的说法要写清楚（出图程序会自动写）。
- 文档写提示词不超过 600 字，实测 2175 字照常出图，不用压缩。
- 返回图片我们用 base64 直接存盘；如果改用网址返回，网址 12 小时后失效。

## 开通要走的步骤（老板自己办钥匙时）

微信扫码登录 → 实名（选微信扫码认证）→ 开通 TokenHub（两个框打勾、同意授权）→ 充值 → 模型广场找到 Seedream-Image-v5.0-pro → 创建 API Key → **在线推理 → 视觉模型 → 这一行点「启用后付费」**（不开就画不出来，开完等一两分钟）。创建的钥匙默认是“全部模型和服务”，建议到 API Key 管理把可访问范围改成只勾 Seedream。

## 报错对照

| 看到 | 意思 | 怎么办 |
| --- | --- | --- |
| 401 | 钥匙不对 | 让用户整串重新复制钥匙 |
| 402，提示“未开启后付费”（code 401007） | 没开后付费，或刚开还没生效 | 照上面最后一步开，等一两分钟再出 |
| 提示余额不足 | 钥匙所在账户没钱了 | 让用户去腾讯云充值；用的是课程钥匙就截图发群 |
| 提示模型不存在、已下线 | 型号被下了 | 跑 `--models` 看现在有哪些，跟用户说清楚再换 |
