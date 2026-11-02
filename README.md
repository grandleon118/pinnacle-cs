# Pinnacle 比分盘 · GitHub 中转采集

国内直连 `guest.api.arcadia.pinnacle.com` 被 SNI 级拦截（同一 Cloudflare IP 换 SNI 就通，
说明不是限流）。这个目录用 GitHub 的境外 runner 绕过这一层：**runner 抓、仓库存、本地读**。

```
GitHub Actions（每 30 分钟）
  fetch_cs.py ──► data/2026-11-02/0945.json
                        │
              api.github.com（实测可达）
                        ▼
本地 pinnacle_import.py
  ──► archive/pinnacle/{date}_open.json / _close.json / _drift.json
  ──► archive/{date}_pinnacle比分.csv      31 档 × pin / jc / EV
  ──► archive/{date}_比分漂移.csv          初盘→临盘 逐档漂移
  ──► Pinnacle比分_累计.csv                长表，一行一档
```

## 一次性配置（5 分钟）

1. 在 GitHub 新建一个 **public** 仓库（公开仓库的 Actions 分钟免费且不限），名字随意，
   例如 `pinnacle-cs`。不要勾选 README / .gitignore / license。
2. 把本目录内容推上去（在 `github_relay/` 里执行）：

   ```bash
   git init -b main
   git add -A
   git -c user.name=me -c user.email=me@example.com commit -m "init"
   git remote add origin https://github.com/<你的账号>/pinnacle-cs.git
   git push -u origin main
   ```

   `.github/workflows/pinnacle-cs.yml` 必须在仓库根目录，push 后 Actions 会自动启用。

3. （可选）仓库 → Settings → Secrets and variables → Actions → New repository secret，
   名字 `PIN_KEY`，值是 Pinnacle 的 guest API key。不设置就用脚本内置的默认 key。

4. 仓库 → Actions → `pinnacle-cs` → Run workflow，手动跑一次确认能出数据。
   跑完仓库里应出现 `data/<今天>/<HHMM>.json`。

5. 本地填仓库地址（二选一）：

   ```bash
   # 方式 A：写进配置文件
   cp github_relay/config.example.json github_relay/config.json
   # 然后编辑 config.json，把 repo 改成 "你的账号/pinnacle-cs"

   # 方式 B：环境变量
   set PIN_GH_REPO=你的账号/pinnacle-cs
   ```

6. 验证：

   ```bash
   python tools/pinnacle_import.py --list
   python tools/pinnacle_import.py --date 2026-11-02
   ```

## 日常用法

```bash
python tools/daily_archive.py                 # auto：有中转配置走中转，否则直连
python tools/daily_archive.py --pin-source relay    # 强制走中转
python tools/pinnacle_import.py --date 2026-11-02   # 单独补某一天
python tools/pinnacle_import.py --list              # 看仓库里攒了哪些天
```

## 为什么 runner 能自己知道该抓哪几场

`webapi.sporttery.cn` 的官方赛程接口在境外可直连，返回 `businessDate` / `编号` /
开赛时间 / 胜平负赔率。所以：

- **不需要**你本地推送清单，runner 自己就能确定当天竞彩有哪些场次；
- 配对用「开赛时间精确到分钟」，同分钟多场时用去水后的 1X2 概率做组内全局贪心；
- 队名从不用中英对照表——配对成功后把「中文队名 → 英文队名」记进
  `data/cache/teams.json`，下次直接命中，零网络开销；
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
