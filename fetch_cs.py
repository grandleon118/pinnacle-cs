# -*- coding: utf-8 -*-
"""
Pinnacle 全网格比分盘 · GitHub Actions 采集端
================================================
跑在 GitHub 的境外 runner 上（不受国内 SNI 封锁影响），每 30 分钟一次，
把「当天竞彩对阵」的 Pinnacle Correct Score 全网格价格落成快照并 commit 回仓库。
本地用 pinnacle_import.py 通过 api.github.com / raw.githubusercontent.com 读回。

为什么 runner 端能自己确定抓哪几场：
  竞彩官方赛程接口 webapi.sporttery.cn 在境外可直连，返回 businessDate / 编号 /
  开赛时间 / had 1X2 赔率，足以和 Pinnacle 的赛程做配对，不需要本地推送清单。

配对（不依赖中英队名对照表）：
  1) 开赛时间精确到分钟 —— 同一分钟有多场时进第 2 步
  2) 去水后 1X2 概率最近邻（组内全局贪心，避免逐场把候选抢走）
  3) 队名字典（data/cache/teams.json，从历次配对成功里学到的 中文→英文 映射）优先命中，
     命中即零网络开销
  4) 1X2 价格缓存在 data/cache/moneyline.json，同一场次跨 runs 复用，不重复请求

用法（本地调试）：
  python fetch_cs.py --selftest            # 离线自检
  python fetch_cs.py --dry-run             # 只配对不同步，打印结果
  python fetch_cs.py --date 2026-11-02     # 只处理指定业务日
  python fetch_cs.py --prune-days 45       # 顺带清理 45 天前的快照
"""
import os, re, sys, json, time, math, gzip, datetime, urllib.request, urllib.error

BASE = 'https://guest.api.arcadia.pinnacle.com'
# 内置候选 key：环境变量 PIN_KEY 优先，其余按顺序在 401/403 时自动轮换
DEFAULT_KEY = 'CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R'
_ALT1 = 'CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R'
ALT_KEYS = [_ALT1]
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
CACHE = os.path.join(DATA, 'cache')

SP_URL = ('https://webapi.sporttery.cn/gateway/jc/football/'
          'getMatchCalculatorV1.qry?poolCode=hhad,had&channel=c')

# 竞彩比分 31 档
HOME = ['1:0', '2:0', '2:1', '3:0', '3:1', '3:2', '4:0', '4:1', '4:2', '5:0', '5:1', '5:2']
DRAW = ['0:0', '1:1', '2:2', '3:3']
AWAY = ['0:1', '0:2', '1:2', '0:3', '1:3', '2:3', '0:4', '1:4', '2:4', '0:5', '1:5', '2:5']
OTHER = ['胜其它', '平其它', '负其它']
ALL31 = HOME + DRAW + AWAY + OTHER

CST = datetime.timezone(datetime.timedelta(hours=8))


# ---------------------------------------------------------------- 传输层
def http_json(url, tries=4, timeout=60, headers=None, quiet=False):
    """带退避的 JSON GET，自动解 gzip。返回 (data, err)。"""
    hdr = {'User-Agent': UA, 'Accept': 'application/json',
           'Accept-Encoding': 'gzip, deflate',
           'Accept-Language': 'en-US,en;q=0.9'}
    if headers:
        hdr.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get('Content-Encoding') or '').lower()
            if raw[:2] == b'\x1f\x8b' or 'gzip' in enc:
                raw = gzip.decompress(raw)
            return json.loads(raw), None
        except urllib.error.HTTPError as e:
            last = 'HTTP %s' % e.code
            if e.code in (401, 403, 404, 204):
                break
        except Exception as e:
            last = '%s: %s' % (type(e).__name__, str(e)[:100])
        if i < tries - 1:
            if not quiet:
                print('    retry %d/%d (%s)' % (i + 1, tries, last))
            time.sleep(3 * (1 + i * 0.6))
    return None, last


_KEYS = []
_KEY_I = 0


def _keys():
    global _KEYS
    if not _KEYS:
        env = (os.environ.get('PIN_KEY') or '').strip()
        _KEYS = ([env] if env else []) + [DEFAULT_KEY] + list(ALT_KEYS)
    return _KEYS


def pin_get(path, **kw):
    """带 API key 自动轮换的请求：401/403 视为 key 失效，换下一个再试。"""
    global _KEY_I
    last = None
    for _ in range(len(_keys())):
        key = _keys()[_KEY_I % len(_keys())]
        raw = path
        d, err = http_json(BASE + raw, headers={
            'X-API-Key': key, 'Referer': 'https://www.pinnacle.com/'}, **kw)
        if d is not None:
            k = key[:6] + '…'
            if not os.environ.get('_PIN_KEY_OK') and _KEY_I:
                print('    API key 轮换生效，当前使用 %s' % k)
            os.environ['_PIN_KEY_OK'] = key
            return d, None
        last = err
        if err and err.startswith('HTTP 40'):
            _KEY_I += 1
            continue
        return None, err
    return None, 'API key 全部失效（%s）' % last


# ---------------------------------------------------------------- 数值
def to_decimal(price):
    """Pinnacle guest 默认美式赔率；兼容已是小数的值。"""
    if price is None:
        return None
    try:
        p = float(price)
    except Exception:
        return None
    if abs(p) >= 100:
        return round(p / 100.0 + 1, 4) if p > 0 else round(100.0 / -p + 1, 4)
    if p <= 1.0:
        return None
    return round(p, 4)


def devig(dec_map):
    inv = {k: 1.0 / v for k, v in dec_map.items() if v and v > 1}
    s = sum(inv.values())
    if s <= 0:
        return {}, 0.0
    return {k: v / s for k, v in inv.items()}, s


def norm31(grid):
    """全网格概率 -> 竞彩 31 档。"""
    out = {k: 0.0 for k in ALL31}
    for sc, p in grid.items():
        if ':' not in sc:
            continue
        try:
            h, a = [int(x) for x in sc.split(':')]
        except Exception:
            continue
        k = '%d:%d' % (h, a)
        if k in out:
            out[k] += p
        elif h > a:
            out['胜其它'] += p
        elif h == a:
            out['平其它'] += p
        else:
            out['负其它'] += p
    return out


def grid_to_1x2(grid):
    """从比分网格反推 1X2 与期望总进球（用于核对配对是否指对场次）。"""
    h = d = a = tot = 0.0
    for sc, p in grid.items():
        if ':' not in sc:
            continue
        x, y = [int(v) for v in sc.split(':')]
        tot += (x + y) * p
        if x > y:
            h += p
        elif x == y:
            d += p
        else:
            a += p
    s = h + d + a
    if s <= 0:
        return None
    return {'h': round(h / s, 6), 'd': round(d / s, 6), 'a': round(a / s, 6),
            'goals': round(tot / s, 4)}


# ---------------------------------------------------------------- 竞彩赛程
def jc_matches(quiet=False):
    d, err = http_json(SP_URL, tries=4, timeout=40,
                       headers={'Referer': 'https://www.sporttery.cn/'}, quiet=quiet)
    if not d:
        return None, err
    out = []
    for day in (d.get('value') or {}).get('matchInfoList') or []:
        for x in day.get('subMatchList') or []:
            had = x.get('had') or {}
            try:
                jc_had = {'h': float(had['h']), 'd': float(had['d']), 'a': float(had['a'])}
            except Exception:
                # 少数场次比分盘已开售但胜平负暂缓（had={}），不能丢——只是无法用 1X2 消歧，
                # 交给「时间唯一」或队名字典去配。
                jc_had = None
            bd = (x.get('businessDate') or '').strip()
            md = (x.get('matchDate') or '').strip()
            mt = (x.get('matchTime') or '').strip()[:5]
            if not bd or not md or len(mt) != 5:
                continue
            try:
                kick = datetime.datetime.strptime('%s %s' % (md, mt), '%Y-%m-%d %H:%M')
            except Exception:
                continue
            out.append({
                '编号': x.get('matchNumStr') or '',
                '业务日': bd,
                '开赛': '%s %s' % (md, mt),
                '联赛': x.get('leagueAbbName') or '',
                '主队': x.get('homeTeamAbbName') or '',
                '客队': x.get('awayTeamAbbName') or '',
                'had': jc_had,
                '状态': x.get('matchStatus') or '',
                '_kick': kick,
            })
    return out, None


def jc_1x2(mt):
    if not mt.get('had'):
        return None
    p, _ = devig({'h': mt['had']['h'], 'd': mt['had']['d'], 'a': mt['had']['a']})
    return p or None


# ---------------------------------------------------------------- Pinnacle 赛程
def pin_fixtures(hours=96, quiet=False):
    d, err = pin_get('/0.1/sports/29/matchups', tries=4, timeout=180, quiet=quiet)
    if d is None:
        return None, err
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for m in d:
        if m.get('type') != 'matchup' or m.get('parent') or m.get('isLive'):
            continue
        st = m.get('startTime')
        if not st:
            continue
        try:
            t = datetime.datetime.fromisoformat(st.replace('Z', '+00:00'))
        except Exception:
            continue
        if not (now - datetime.timedelta(hours=4) <= t <= now + datetime.timedelta(hours=hours)):
            continue
        parts = m.get('participants') or []
        home = next((p['name'] for p in parts if p.get('alignment') == 'home'), None)
        away = next((p['name'] for p in parts if p.get('alignment') == 'away'), None)
        if not home or not away:
            continue
        bj = (t + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
        out.append({'id': m['id'], 'bj': bj, 'utc': st, 'home': home, 'away': away,
                    'league': (m.get('league') or {}).get('name', '')})
    return out, None


# ---------------------------------------------------------------- 缓存
def _load(name, default):
    p = os.path.join(CACHE, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding='utf-8'))
    except Exception:
        return default


def _save(name, obj):
    os.makedirs(CACHE, exist_ok=True)
    json.dump(obj, open(os.path.join(CACHE, name), 'w', encoding='utf-8'),
              ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------- 队名映射 / 1X2
def _same_team(a, b):
    a = (a or '').strip().lower()
    b = (b or '').strip().lower()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta = set(re.findall(r'[a-z]+', a))
    tb = set(re.findall(r'[a-z]+', b))
    return len(ta & tb) > 0


def _dist(p1, p2):
    if not p1 or not p2:
        return 9.9
    return abs(p1['h'] - p2['h']) + abs(p1['d'] - p2['d']) + abs(p1['a'] - p2['a'])


class Pairer:
    """竞彩场次 <-> Pinnacle matchup 配对器（带队名字典与 1X2 缓存）。"""

    def __init__(self, ml_cache, teams, budget=120, quiet=False):
        self.ml = ml_cache            # {str(pin_id): {'h','d','a'}}
        self.teams = teams            # {中文队名: 英文队名}
        self.budget = budget
        self.fetched = 0
        self.quiet = quiet

    def ml_of(self, pid):
        k = str(pid)
        if k in self.ml:
            return self.ml[k]
        if self.budget <= 0:
            return None
        d, err = pin_get('/0.1/matchups/%d/markets/related/straight' % pid,
                         tries=2, timeout=30, quiet=True)
        self.budget -= 1
        self.fetched += 1
        time.sleep(0.3)
        if not d:
            return None
        for mk in d:
            if mk.get('type') != 'moneyline' or mk.get('period') != 0:
                continue
            if mk.get('matchupId') not in (None, pid):
                continue
            dec = {}
            for pr in mk.get('prices') or []:
                des, dv = pr.get('designation'), to_decimal(pr.get('price'))
                if des in ('home', 'draw', 'away') and dv:
                    dec[des] = dv
            if len(dec) == 3:
                p, _ = devig(dec)
                self.ml[k] = {'h': p['home'], 'd': p['draw'], 'a': p['away']}
                return self.ml[k]
        return None

    def name_hits(self, mt, cand):
        """用已学到的队名字典判断候选是否就是这场；返回 (命中数, 已知数)。"""
        hits = known = 0
        for cn, en in ((mt['主队'], cand['home']), (mt['客队'], cand['away'])):
            mapped = self.teams.get(cn)
            if not mapped:
                continue
            known += 1
            if _same_team(mapped, en):
                hits += 1
        return hits, known

    def learn(self, mt, cand):
        for cn, en in ((mt['主队'], cand['home']), (mt['客队'], cand['away'])):
            if cn and en:
                self.teams.setdefault(cn, en)


def pair_fixtures(jc_list, pin_list, pairer, max_dist=0.12):
    """返回 {编号: (pin_matchup, dist, 方式)}"""
    by_bj = {}
    for p in pin_list:
        by_bj.setdefault(p['bj'], []).append(p)

    groups = {}
    for mt in jc_list:
        groups.setdefault(mt['开赛'], []).append(mt)

    out, used = {}, {}

    # ---- 第一轮：同一开赛分钟
    for bj, fx in groups.items():
        cands = [c for c in (by_bj.get(bj) or []) if c['id'] not in used]
        if not cands:
            continue
        if len(cands) == 1 and len(fx) == 1:
            c = cands[0]
            out[fx[0]['编号']] = (c, 0.0, '时间唯一')
            used[c['id']] = fx[0]['编号']
            pairer.learn(fx[0], c)
            continue
        # 队名字典优先：零网络开销
        rest = []
        for mt in fx:
            best = None
            for c in cands:
                if c['id'] in used:
                    continue
                hits, known = pairer.name_hits(mt, c)
                if known and hits == known:
                    best = c
                    break
            if best is not None:
                out[mt['编号']] = (best, 0.0, '队名字典')
                used[best['id']] = mt['编号']
                pairer.learn(mt, best)
            else:
                rest.append(mt)
        if not rest:
            continue
        # 1X2 最近邻，组内全局贪心
        pairs = []
        for mt in rest:
            jp = jc_1x2(mt)
            for c in cands:
                if c['id'] in used:
                    continue
                pairs.append((_dist(jp, pairer.ml_of(c['id'])), mt['编号'], c))
        pairs.sort(key=lambda x: x[0])
        for d, num, c in pairs:
            if num in out or c['id'] in used or d > max_dist:
                continue
            out[num] = (c, round(d, 4), '时间+1X2')
            used[c['id']] = num
            pairer.learn(next(m for m in rest if m['编号'] == num), c)
        # 剩余一一对应时兜底（常见于 had 暂缓开售、无从比 1X2 的场次）
        left_fx = [m for m in fx if m['编号'] not in out]
        left_c = [c for c in cands if c['id'] not in used]
        if len(left_fx) == 1 and len(left_c) == 1:
            out[left_fx[0]['编号']] = (left_c[0], 0.0, '时间兜底')
            used[left_c[0]['id']] = left_fx[0]['编号']
            pairer.learn(left_fx[0], left_c[0])

    # ---- 第二轮：±90 分钟补漏
    for mt in jc_list:
        if mt['编号'] in out:
            continue
        jp = jc_1x2(mt)
        if not jp:
            continue
        best, bd = None, 9.9
        for c in pin_list:
            if c['id'] in used:
                continue
            try:
                t0 = datetime.datetime.strptime(mt['开赛'], '%Y-%m-%d %H:%M')
                t1 = datetime.datetime.strptime(c['bj'], '%Y-%m-%d %H:%M')
            except Exception:
                continue
            gap = abs((t1 - t0).total_seconds()) / 60.0
            if gap > 90 or gap == 0:
                continue
            hits, known = pairer.name_hits(mt, c)
            if known and hits == known:
                best, bd = c, 0.0
                break
            d = _dist(jp, pairer.ml_of(c['id'])) + gap / 1000.0
            if d < bd:
                best, bd = c, d
        if best is not None and bd <= max_dist + 0.05:
            out[mt['编号']] = (best, round(bd, 4), '邻近时间+1X2')
            used[best['id']] = mt['编号']
            pairer.learn(mt, best)
    return out


# ---------------------------------------------------------------- 比分盘
_SCORE_RE = re.compile(r'^(.*?)\s+(\d+)\s*,\s*(.*?)\s+(\d+)$')


def find_cs_special(mid, quiet=True):
    d, err = pin_get('/0.1/matchups/%d/related' % mid, tries=3, timeout=30, quiet=quiet)
    if d is None:
        return None, err
    for it in d:
        if it.get('type') != 'special':
            continue
        desc = ((it.get('special') or {}).get('description') or '').strip().lower()
        if desc == 'correct score':
            return it, None
    for it in d:
        desc = ((it.get('special') or {}).get('description') or '').strip().lower()
        if 'correct' in desc and 'score' in desc and 'half' not in desc:
            return it, None
    return None, '无 Correct Score 盘'


def fetch_cs(special, parent_home, parent_away, quiet=True):
    sid = special.get('id')
    pid2name = {p.get('id'): p.get('name', '') for p in (special.get('participants') or [])}
    d, err = pin_get('/0.1/matchups/%d/markets/related/straight' % sid,
                     tries=3, timeout=30, quiet=quiet)
    if not d:
        d, err2 = pin_get('/0.1/matchups/%d/markets/straight' % sid,
                          tries=2, timeout=30, quiet=True)
        err = err or err2
    if not d:
        return None, err or '比分盘无市场'
    rows = [m for m in d if m.get('matchupId') == sid] or \
           [m for m in d if m.get('matchupId') in (None, sid)]
    if not rows:
        return None, '无对应市场'
    rows.sort(key=lambda m: (m.get('type') != 'moneyline', m.get('period') != 0))
    dec = {}
    for pr in rows[0].get('prices') or []:
        name = pid2name.get(pr.get('participantId'), '')
        dv = to_decimal(pr.get('price'))
        if not name or not dv:
            continue
        mm = _SCORE_RE.match(name.strip())
        if not mm:
            continue
        th, hg, ta, ag = mm.group(1), int(mm.group(2)), mm.group(3), int(mm.group(4))
        if _same_team(th, parent_home) or _same_team(ta, parent_away):
            h, a = hg, ag
        elif _same_team(th, parent_away) or _same_team(ta, parent_home):
            h, a = ag, hg
        else:
            h, a = hg, ag
        dec['%d:%d' % (h, a)] = dv
    if not dec:
        return None, '比分档解析为空'
    probs, over = devig(dec)
    grid = {k: {'dec': dec[k], 'p': round(probs.get(k, 0.0), 6)} for k in dec}
    return {'grid': grid, 'overround': round(over, 4), 'n': len(dec)}, None


# ---------------------------------------------------------------- 主流程
def snapshot(date_str=None, hours=96, max_ml=120, quiet=False):
    """抓一轮快照，返回 {业务日: payload}。"""
    now_cst = datetime.datetime.now(CST)
    jcs, err = jc_matches(quiet=quiet)
    if jcs is None:
        return None, '竞彩赛程抓取失败：%s' % err
    # 只保留未开赛的（开赛后 Pinnacle 会把比分盘下架）
    jcs = [m for m in jcs
           if m['_kick'] > now_cst.replace(tzinfo=None) + datetime.timedelta(minutes=2)]
    if date_str:
        jcs = [m for m in jcs if m['业务日'] == date_str]
    if not jcs:
        return {}, None
    if not quiet:
        print('  竞彩待售 %d 场（业务日 %s）'
              % (len(jcs), ','.join(sorted({m['业务日'] for m in jcs}))))

    pins, err = pin_fixtures(hours=hours, quiet=quiet)
    if pins is None:
        return None, 'Pinnacle 赛程抓取失败：%s' % err
    if not quiet:
        print('  Pinnacle 在售足球 %d 场（未来 %d 小时）' % (len(pins), hours))

    ml_cache = _load('moneyline.json', {})
    teams = _load('teams.json', {})
    pairer = Pairer(ml_cache, teams, budget=max_ml, quiet=quiet)
    pairs = pair_fixtures(jcs, pins, pairer)
    if not quiet:
        print('  配对 %d / %d 场（新增 1X2 请求 %d 次）'
              % (len(pairs), len(jcs), pairer.fetched))

    out = {}
    ok = 0
    for mt in jcs:
        rec = {'编号': mt['编号'], '联赛': mt['联赛'], '主队': mt['主队'],
               '客队': mt['客队'], '开赛': mt['开赛'], 'jc_had': mt['had'],
               '状态': 'no_match'}
        pr = pairs.get(mt['编号'])
        if pr:
            pm, dist, how = pr
            rec.update({'状态': 'no_cs', 'pin_id': pm['id'], 'pin_league': pm['league'],
                        'pin_home': pm['home'], 'pin_away': pm['away'],
                        'pin_start': pm['bj'], 'pair_dist': dist, 'pair_how': how})
            sp, e1 = find_cs_special(pm['id'], quiet=True)
            time.sleep(0.4)
            if sp:
                cs, e2 = fetch_cs(sp, pm['home'], pm['away'], quiet=True)
                time.sleep(0.4)
                if cs:
                    grid_p = {k: v['p'] for k, v in cs['grid'].items()}
                    p31 = norm31(grid_p)
                    cov = sum(p31.values())
                    if cov > 0:
                        p31 = {k: v / cov for k, v in p31.items()}
                    rec.update({'状态': 'ok', 'cs_n': cs['n'],
                                'overround': cs['overround'],
                                '覆盖率': round(cov, 4),
                                'grid': cs['grid'],
                                'p31': {k: round(v, 6) for k, v in p31.items()},
                                'pin_1x2': grid_to_1x2(grid_p)})
                    ok += 1
                else:
                    rec['note'] = e2
            else:
                rec['note'] = e1
        out.setdefault(mt['业务日'], []).append(rec)

    payloads = {}
    for bd, recs in out.items():
        payloads[bd] = {
            'date': bd,
            'snapshot': now_cst.strftime('%Y-%m-%d %H:%M'),
            'snapshot_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source': 'guest.api.arcadia.pinnacle.com CS markets + webapi.sporttery.cn 赛程',
            '口径': 'Pinnacle 全网格按比例去水后聚合竞彩 31 档；覆盖率<0.97 表示盘面截断',
            'jc_total': len(recs),
            'paired': sum(1 for r in recs if r['状态'] in ('ok', 'no_cs')),
            'cs_ok': sum(1 for r in recs if r['状态'] == 'ok'),
            'matches': recs,
        }

    # 只回写被这次扫到的场次的缓存，避免缓存无限膨胀
    live_ids = {str(c['id']) for c in pins}
    ml_cache = {k: v for k, v in ml_cache.items() if k in live_ids}
    _save('moneyline.json', ml_cache)
    _save('teams.json', teams)
    return payloads, None


def write_snapshot(payloads, dry_run=False):
    if dry_run:
        for bd, pl in payloads.items():
            print('  [dry-run] %s → data/%s/*.json  竞彩 %d / 配对 %d / 比分盘 %d'
                  % (bd, bd, pl['jc_total'], pl['paired'], pl['cs_ok']))
        return []
    written = []
    hhmm = datetime.datetime.now(CST).strftime('%H%M')
    for bd, pl in payloads.items():
        d = os.path.join(DATA, bd)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, '%s.json' % hhmm)
        json.dump(pl, open(p, 'w', encoding='utf-8'), ensure_ascii=False,
                  separators=(',', ':'))
        written.append(p)
        print('  %s → 竞彩 %d / 配对 %d / 比分盘 %d  (%.1f KB)'
              % (os.path.relpath(p, ROOT), pl['jc_total'], pl['paired'], pl['cs_ok'],
                 os.path.getsize(p) / 1024.0))
    return written


def prune(days=45):
    if not os.path.isdir(DATA):
        return 0
    cut = (datetime.datetime.now(CST).date() - datetime.timedelta(days=days)).isoformat()
    n = 0
    for name in sorted(os.listdir(DATA)):
        p = os.path.join(DATA, name)
        if not os.path.isdir(p) or not re.match(r'^\d{4}-\d{2}-\d{2}$', name):
            continue
        if name < cut:
            for f in os.listdir(p):
                os.remove(os.path.join(p, f))
            os.rmdir(p)
            n += 1
    if n:
        print('  清理 %d 个过期日期目录（早于 %s）' % (n, cut))
    return n


# ---------------------------------------------------------------- 自检
def selftest():
    # 1) 美式赔率
    assert abs(to_decimal(250) - 3.5) < 1e-9
    assert abs(to_decimal(-209) - 1.4785) < 1e-3
    assert to_decimal(7.69) == 7.69 and to_decimal(None) is None
    # 2) 去水
    p, s = devig({'h': 2.0, 'd': 4.0, 'a': 4.0})
    assert abs(sum(p.values()) - 1) < 1e-9 and abs(s - 1.0) < 1e-9
    # 3) 31 档
    g = {'1:0': 0.1, '0:0': 0.08, '0:1': 0.09, '6:0': 0.01, '6:6': 0.005, '0:7': 0.004}
    p31 = norm31(g)
    assert len(p31) == 31
    assert abs(p31['1:0'] - 0.1) < 1e-9 and abs(p31['胜其它'] - 0.01) < 1e-9
    assert abs(p31['平其它'] - 0.005) < 1e-9 and abs(p31['负其它'] - 0.004) < 1e-9
    # 4) 网格反推 1X2 + 期望进球
    x = grid_to_1x2({'1:0': 0.3, '0:0': 0.2, '0:1': 0.5})
    assert abs(x['h'] - 0.3) < 1e-9 and abs(x['d'] - 0.2) < 1e-9 and abs(x['a'] - 0.5) < 1e-9
    assert abs(x['goals'] - (1 * 0.3 + 0 * 0.2 + 1 * 0.5)) < 1e-9
    # 5) 比分档名解析
    mm = _SCORE_RE.match('Aston Villa 1, Manchester United 0')
    assert mm and mm.group(2) == '1' and mm.group(4) == '0'
    assert _same_team('Man United', 'Manchester United')
    assert not _same_team('Aston Villa', 'Manchester United')
    # 6) 队名字典命中
    pl = Pairer({}, {'国际图尔': 'Inter Turku'})
    hits, known = pl.name_hits({'主队': '国际图尔', '客队': '赫尔火花'},
                              {'home': 'Inter Turku', 'away': 'HJK'})
    assert (hits, known) == (1, 1), (hits, known)
    # 7) 配对：同分钟两场，靠 1X2 消歧
    jl = [{'编号': 'A', '开赛': '2026-11-03 04:00', 'had': {'h': 1.5, 'd': 4.0, 'a': 6.0},
           '主队': 'x', '客队': 'y'},
          {'编号': 'B', '开赛': '2026-11-03 04:00', 'had': {'h': 4.0, 'd': 3.6, 'a': 1.9},
           '主队': 'z', '客队': 'w'}]
    pn = [{'id': 1, 'bj': '2026-11-03 04:00', 'home': 'H1', 'away': 'A1', 'league': 'L'},
          {'id': 2, 'bj': '2026-11-03 04:00', 'home': 'H2', 'away': 'A2', 'league': 'L'}]
    pr = Pairer({'1': {'h': 0.62, 'd': 0.22, 'a': 0.16},
                 '2': {'h': 0.22, 'd': 0.26, 'a': 0.52}}, {})
    res = pair_fixtures(jl, pn, pr)
    assert res['A'][0]['id'] == 1 and res['B'][0]['id'] == 2, res
    print('   selftest: 7 组断言全部通过')
    return 0


def main(argv):
    a = argv[:]
    date_str = None
    for i, x in enumerate(a):
        if x == '--date' and i + 1 < len(a):
            date_str = a[i + 1]
    hours = 96
    if '--hours' in a:
        hours = int(a[a.index('--hours') + 1])
    max_ml = 120
    if '--max-ml' in a:
        max_ml = int(a[a.index('--max-ml') + 1])

    if '--selftest' in a:
        print('== 离线自检 ==')
        return selftest()

    prune_days = 45
    if '--prune-days' in a:
        prune_days = int(a[a.index('--prune-days') + 1])

    print('== Pinnacle CS 快照 %s ==' % datetime.datetime.now(CST).strftime('%Y-%m-%d %H:%M'))
    payloads, err = snapshot(date_str=date_str, hours=hours, max_ml=max_ml)
    if payloads is None:
        print('  失败:', err)
        return 1
    if not payloads:
        print('  今天没有待售竞彩场次，跳过')
    else:
        write_snapshot(payloads, dry_run='--dry-run' in a)
        tot = sum(p['cs_ok'] for p in payloads.values())
        print('  本轮共取到 %d 场全网格比分盘' % tot)
        if tot == 0:
            print('  ⚠ 一场都没抓到——检查 PIN_KEY 是否失效（secrets.PIN_KEY）')
    if '--dry-run' not in a:
        prune(days=prune_days)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
