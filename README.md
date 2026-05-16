# qdii-reference-history

用于归档 QDII 估值所需的参考资产日线快照。

当前先覆盖一条最关键的链：

- `XOP` ETF 日收盘价
- `^XOP-IV` 日内估值指数最新值

## 目录

- `snapshots/yahoo/xop/daily/YYYY/MM/YYYY-MM-DD.json`
  大白话：`XOP ETF` 的当日快照
- `snapshots/yahoo/xop_iv/daily/YYYY/MM/YYYY-MM-DD.json`
  大白话：`^XOP-IV` 的当日快照
- `raw/yahoo/chart/...`
  大白话：抓取时保存的原始 Yahoo chart 响应

## 用法

本地运行：

```bash
python scripts/archive_xop_reference.py
```

如果本地需要代理：

```bash
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 python scripts/archive_xop_reference.py
```

## 设计原则

- 每天美股收盘后抓一次并归档
- 同时保存 `raw payload` 和归一化 `snapshot`
- 同一交易日重复运行时，只更新对应日期文件，不新增脏重复文件
- 归档仓库只负责保存快照
- 业务 API 应先同步到本地数据库，再由本地数据库对外提供回补能力
