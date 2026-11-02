# Pinnacle 比分盘 · GitHub 中转采集

国内直连 `guest.api.arcadia.pinnacle.com` 被 SNI 级拦截（同一 Cloudflare IP 换 SNI 就通，
说明不是限流）。这个目录用 GitHub 的境外 runner 绕过这一层：**本地推赛程、runner 抓盘、仓库存、本地读**。

```
本地 push_fixtures.py（每天跑 1–2 次）
  └─► fixtures/{业务日}.json ──git push──► 仓库
                                            │
GitHub Actions（每 30 分钟）                  │
  fetch_cs.py 读 fixtures/ + 直连 Pinnacle ──┴─► data/2026-11-02/1727.json
                                                        │
                                          api.github.com（实测可达）
                                                        ▼
本地 pinnacle_import.py
  ──► archive/pinnacle/{date}_open.json / _close.json / _drift.json
  ──► archive/{date}_pinnacle比分.csv      31 档 × pin / jc / EV
  ──► archive/{date}_比分漂移.csv          初盘→临盘 逐档漂移
  ──► Pinnacle比分_累计.csv                长表，一行一档
```

## 一次性配置（已完成）

仓库：https://github.com/grandleon118/pinnacle-cs （public）

1. 本地已填好 `config.json`（`repo: grandleon118/pinnacle-cs`）。
2. `PIN_KEY` 未设置，用脚本内置 key；失效时自动轮换，全部失效才报错。
3. 验证：

   ```bash
   python tools/pinnacle_import.py --list
   python tools/pinnacle_import.py --date 2026-11-02
   ```

## 日常用法

```bash
python tools/push_fixtures.py                       # 导出今明两天竞彩赛程并推送（先跑这个）
python tools/daily_archive.py                       # auto：有中转配置走中转，否则直连
python tools/daily_archive.py --pin-source relay    # 强制走中转
python tools/pinnacle_import.py --date 2026-11-02   # 单独补某一天
python tools/pinnacle_import.py --list              # 看仓库里攒了哪些天
```

## 为什么赛程要从本地推（重要）

最初的设计是让 runner 自己调竞彩官方接口确定场次，**实测不通**：

```
webapi.sporttery.cn  →  runner（Azure 机房 IP）  返回 HTTP 567
```

567 是它的风控验证页。同一请求从本机发出是 200，差别只在来源 IP。
所以职责切开：**本机负责赛程（fixtures/），runner 只负责抓 Pinnacle**。

runner 端仍然保留「仓库里没有当天赛程就直连官方接口」的兜底，但机房 IP 上基本拿不到，
见到 `竞彩赛程抓取失败：HTTP 567` 就是 fixtures 没推或已过期。

配对本身不依赖中英队名对照表：

- 主键是「开赛时间精确到分钟」，同分钟多场时用去水后的 1X2 概率做组内全局贪心；
- 配对成功后把「中文队名 → 英文队名」记进 `data/cache/teams.json`，下次直接命中；
- 1X2 价格缓存在 `data/cache/moneyline.json`，同一场次跨 30 分钟复用，不重复请求。

## 已知限制

- **`had` 暂缓开售的场次**（胜平负没开、比分盘已开）没有 1X2 可比，若同一分钟还有别的
  Pinnacle 场次，会配不上，记 `状态: no_match`。等它开售或等队名字典学到就能配上。
- **Actions 定时不准**：免费 runner 高峰期可能延迟 5–15 分钟，`*/30` 实际间隔会抖，
  但漂移分析要的是「时间序列」不是「精确间隔」，不影响。
- **盘面截断**：Pinnacle 比分盘通常只挂到 4~6 球，尾部按比例摊回 31 档，同时记
  `覆盖率`；低于 0.97 的场次在分析时应剔除。
- **数据保留**：`--prune-days 45` 由 workflow 自动清理，稳态约 20–30 MB。

## 兜底：守候模式（不依赖 GitHub）

runner 不可用时，还能靠「等一个能穿透的窗口」碰运气（Pinnacle 并非永久封锁，
实测偶发可通）：

```bash
python tools/pinnacle_cs.py --date 2026-11-02 --stamp open  --watch 10 --hours 3
python tools/pinnacle_cs.py --date 2026-11-02 --stamp close --watch 10 --hours 5
```
