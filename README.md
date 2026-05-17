# qdii-reference-history

用于归档 QDII 估值所需的参考资产日线快照。

当前先覆盖一条最关键的链：

- `XOP` ETF 日收盘价
- `^XOP-IV` 日内估值指数最新值
- `KWEB` 日收盘价、折溢价和推导净值
- `USCNY` 官方中间价

## 目录

- `snapshots/yahoo/xop/daily/YYYY/MM/YYYY-MM-DD.json`
  大白话：`XOP ETF` 的当日快照
- `snapshots/yahoo/xop_iv/daily/YYYY/MM/YYYY-MM-DD.json`
  大白话：`^XOP-IV` 的当日快照
- `raw/yahoo/chart/...`
  大白话：抓取时保存的原始 Yahoo chart 响应
- `snapshots/krane/kweb_nav/daily/YYYY/MM/YYYY-MM-DD.json`
  大白话：`KWEB` 的当日推导净值快照
- `raw/krane/product-json/kweb_premium_discount/daily/...`
  大白话：抓取时保存的 `Krane premium-discount` 原始响应
- `snapshots/chinamoney/uscny/daily/YYYY/MM/YYYY-MM-DD.json`
  大白话：`USCNY` 的当日官方中间价快照
- `raw/chinamoney/ccpr/...`
  大白话：抓取时保存的原始 `ccpr.json`

## 用法

本地运行：

```bash
python scripts/archive_xop_reference.py
python scripts/archive_kweb_reference.py
python scripts/archive_uscny_reference.py
```

如果本地需要代理：

```bash
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 python scripts/archive_xop_reference.py
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 python scripts/archive_kweb_reference.py
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 python scripts/archive_uscny_reference.py
```

## 设计原则

- 每天美股收盘后抓一次并归档
- 同时保存 `raw payload` 和归一化 `snapshot`
- 同一交易日重复运行时，只更新对应日期文件，不新增脏重复文件
- 归档仓库只负责保存快照
- 业务 API 应先同步到本地数据库，再由本地数据库对外提供回补能力
- `KWEB` 不归档别人页面已经算好的 `EST`，只归档 `收盘价 + 折溢价 + 推导净值`
- `KWEB` 的 `Krane pid` 不能写死，必须从官网产品页动态发现

## GitHub Actions

当前 workflow：

- 文件：
  `.github/workflows/archive-xop-reference.yml`
- 触发时点：
  - `20:10 UTC`
  - `20:30 UTC`
  - `20:50 UTC`
  - `21:10 UTC`

按夏令时看，大白话就是：

- 美东收盘后 `10 / 30 / 50 / 70` 分钟各抓一次

单次 workflow 内部还会再重试：

- 默认重试 `3` 次
- 默认回退等待：
  - `60s`
  - `180s`
  - `300s`

`KWEB` workflow：

- 文件：
  `.github/workflows/archive-kweb-reference.yml`
- 触发时点：
  - `20:10 UTC`
  - `20:30 UTC`
  - `20:50 UTC`
  - `21:10 UTC`

按夏令时看，大白话就是：

- 美东收盘后 `10 / 30 / 50 / 70` 分钟各抓一次

单次 workflow 内部同样会重试：

- 默认重试 `3` 次
- 默认回退等待：
  - `60s`
  - `180s`
  - `300s`

`KWEB` 的成功条件比 `XOP` 更严格：

1. Yahoo 要给出最新有效日线 close
2. Krane 要给出同一美东日期的 `premium-discount`
3. 两边日期必须一致
4. 才会把 `KWEB净值 = close × (1 - premium_discount)` 写入快照

另一个 `USCNY` workflow：

- 文件：
  `.github/workflows/archive-uscny-reference.yml`
- 触发时点：
  - `01:16 UTC`
  - `01:30 UTC`
  - `01:50 UTC`
  - `02:10 UTC`

按北京时间看，大白话就是：

- 上午 `09:16 / 09:30 / 09:50 / 10:10` 各抓一次

单次 workflow 内部同样会重试：

- 默认重试 `3` 次
- 默认回退等待：
  - `60s`
  - `180s`
  - `300s`

## GitHub Secrets

你需要在仓库里设置：

- `WECOM_WEBHOOK_URL`
  大白话：企业微信机器人 webhook 地址

通知规则：

- 成功归档：发送成功通知
- 失败：只在当天最后一个定时窗口发送失败通知，避免前面几个窗口失败时反复刷屏
- `workflow_dispatch` 手工触发：失败也会直接通知
