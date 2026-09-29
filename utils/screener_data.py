"""
选股数据源聚合层（业务层）

背景
----
原来 5 个选股模块全部依赖 iwencai（经 pywencai）。该接口已对程序化请求
一律返回 403，无法在应用内修复。

本模块用【已验证可用】的公开源重建同一套数据，并且**刻意保持原 iwencai
的列名契约**，使下游 `main_force_analysis.py` / `*_ui.py` 无需改动。

数据源分工
----------
    资金流排行（全市场）  同花顺 data.10jqka.com.cn（经 akshare）
                          —— 与原 iwencai 同源，语义最接近
    行情（市值/PE/PB）    腾讯 qt.gtimg.cn
    财务（ROE/毛利/增长） 东财 datacenter-web
    资产负债率/行业       东财 RPT_DMSK_FN_BALANCE
    解禁 / 增减持         东财 RPT_LIFT_STAGE / RPT_SHARE_HOLDER_INCREASE
    公告                  东财 np-anotice-stock
    （可选加速）          东财 push2 —— 实测常被线路级阻断，仅作机会性尝试

为什么不用东财 push2 做主力
--------------------------
实测 push2 整个域（push2 / push2his / 1.push2 / 7.push2 / push2delay /
82.push2）会同时连接被重置，且持续数分钟以上；`curl` 同样失败，故不是
TLS 指纹问题，也不是速率限流。而上述其它源在同一时刻全部正常。
详见 utils/eastmoney_client.py 的模块注释。

注意：本模块可脱离 Streamlit 独立运行（缓存用模块级 dict，不用 st.cache_data）。
"""

import re
import sys
import io
import time
import json
import threading
import warnings
from datetime import datetime, timedelta

import pandas as pd
import requests

from utils.eastmoney_client import EastmoneyClient, EM_MAX_DATACENTER_PAGE

warnings.filterwarnings('ignore')


def _setup_stdout_encoding():
    """把 stdout 设为 UTF-8，避免 GBK 控制台在 print emoji 时崩溃。

    ⚠️ 这里**不**沿用「能 import streamlit 就跳过」的老写法：Streamlit 应用
    同样可能从 GBK 控制台启动，跳过等于保护失效。用 errors='replace' 后，
    重新配置在两种环境下都只会放宽而不会收紧，故总是执行。

    优先 `reconfigure`：`sys.stdout` 可能已被包装过而没有 `.buffer`
    属性（此时 `TextIOWrapper(sys.stdout.buffer, ...)` 会 AttributeError，
    然后被 except 吞掉 —— 等于这个保护静默失效）。
    """
    if sys.platform != 'win32':
        return

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        return
    except Exception:
        pass
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass


_setup_stdout_encoding()


# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# 腾讯单次批量上限。实测 400 只可返回，取 200 留冗余。
TENCENT_BATCH = 200

# 东财 datacenter 的 in 批量上限（实测 3 只正常；取 100 保守）
EM_IN_BATCH = 100

# 缓存 TTL（秒）
TTL_QUOTE = 600      # 行情快照 10 分钟
TTL_FINANCE = 3600   # 财报 1 小时
TTL_FUNDFLOW = 600   # 资金流 10 分钟

# 同花顺资金流周期 → akshare symbol
THS_PERIOD_MAP = {
    '今日': '即时',
    '3日': '3日排行',
    '5日': '5日排行',
    '10日': '10日排行',
    '20日': '20日排行',
}

_client = EastmoneyClient()

# ----------------------------------------------------------------------------
# 模块级 TTL 缓存
# ----------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl, fn):
    """带 TTL 的模块级缓存。fn 抛异常时不写缓存，向上传播。"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]

    value = fn()

    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def clear_cache():
    """清空缓存（UI 上的「刷新」按钮用）"""
    with _cache_lock:
        _cache.clear()


# ----------------------------------------------------------------------------
# 通用解析
# ----------------------------------------------------------------------------

def parse_cn_amount(text):
    """解析中文金额：'9.12亿' -> 9.12e8，'-2330.29万' -> -2.33e7，'--' -> None

    返回【元】，与 iwencai 的输出单位一致 —— 这样 main_force_selector
    里现成的「>100000 判为元」逻辑继续有效。
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)

    s = str(text).strip().replace(',', '').replace(' ', '')
    if s in ('', '-', '--', 'None', 'nan', 'null'):
        return None

    sign = -1.0 if s.startswith('-') else 1.0
    s = s.lstrip('+-')

    mult = 1.0
    if s.endswith('万亿'):
        mult, s = 1e12, s[:-2]
    elif s.endswith('亿'):
        mult, s = 1e8, s[:-1]
    elif s.endswith('万'):
        mult, s = 1e4, s[:-1]
    elif s.endswith('元'):
        s = s[:-1]

    try:
        return sign * float(s) * mult
    except ValueError:
        return None


def parse_cn_percent(text):
    """解析百分比：'653.16%' -> 653.16，'--' -> None"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace('%', '').replace(',', '')
    if s in ('', '-', '--', 'None', 'nan', 'null'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_number(text):
    """宽松数值解析"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(',', '')
    if s in ('', '-', '--', 'None', 'nan', 'null'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_code(code):
    """统一成 6 位字符串。同花顺返回的代码会丢前导零（000001 -> 1）。"""
    if code is None:
        return None
    s = str(code).strip()
    if s.endswith('.0'):
        s = s[:-2]
    s = re.sub(r'\D', '', s)
    if not s:
        return None
    return s.zfill(6)


# ----------------------------------------------------------------------------
# 板块 / ST 判定
# ----------------------------------------------------------------------------

def board_of(code):
    """按代码前缀判定板块"""
    code = normalize_code(code) or ''
    if code.startswith('688') or code.startswith('689'):
        return '科创板'
    if code.startswith('300') or code.startswith('301'):
        return '创业板'
    if code.startswith(('8', '4', '9')):
        return '北交所'
    return '主板'


def is_st(name):
    """名称含 ST / *ST / 退市 视为风险股"""
    if not name:
        return False
    n = str(name).upper().replace(' ', '')
    return ('ST' in n) or ('退' in n)


def exchange_of(code):
    """交易所：'sh' / 'sz' / 'bj'"""
    code = normalize_code(code) or ''
    if code.startswith('6'):
        return 'sh'
    if code.startswith(('0', '3')):
        return 'sz'
    return 'bj'


def market_prefix(code):
    """腾讯/新浪的代码前缀"""
    code = normalize_code(code) or ''
    if code.startswith('6'):
        return 'sh'
    if code.startswith(('0', '3')):
        return 'sz'
    return 'bj'


# ----------------------------------------------------------------------------
# 同花顺：全市场资金流排行
# ----------------------------------------------------------------------------

# 同花顺资金流：板块路径
_THS_BOARD = {
    '今日': '',
    '3日': 'board/3/',
    '5日': 'board/5/',
    '10日': 'board/10/',
    '20日': 'board/20/',
}
_THS_PAGE_URL = ('http://data.10jqka.com.cn/funds/ggzjl/'
                 '{board}field/zdf/order/desc/page/{page}/ajax/1/free/1/')
# ⚠️ 10jqka 对抓取速率很敏感，实测结论：
#   * 5 并发           -> 105 页里 45 页返回空，全市场少 1000 只
#   * 串行 0.05s/页    -> 同样大面积失败（限流有累积性，一旦触发会持续）
#   * 串行 0.3s/页     -> 30/30 页稳定成功
# 丢数据的代价远大于多花几十秒，故串行 + 0.3s。
_THS_WORKERS = 1
_THS_DELAY = 0.3      # 串行时每页间隔（秒）

# 模块级 ths.js 上下文（MiniRacer 非线程安全，token 只在主线程生成一次后共享）
_ths_js = None
_ths_js_lock = threading.Lock()


def _ths_token():
    """生成 10jqka 的 hexin-v token（只 eval 一次 ths.js，之后调用很廉价）。

    ⚠️ akshare 的 stock_fund_flow_individual 每页都重新 eval 一遍 ths.js，
    105 页要 100 秒；这里复用单个 JS 上下文，同样的抓取只需约 25 秒。
    """
    global _ths_js
    with _ths_js_lock:
        if _ths_js is None:
            import py_mini_racer
            from akshare.stock_feature.stock_fund_flow import _get_file_content_ths
            _ths_js = py_mini_racer.MiniRacer()
            _ths_js.eval(_get_file_content_ths('ths.js'))
        return _ths_js.call('v')


def _ths_headers(token):
    return {
        'Accept': 'text/html, */*; q=0.01',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'hexin-v': token,
        'Host': 'data.10jqka.com.cn',
        'Referer': 'http://data.10jqka.com.cn/funds/hyzjl/',
        'User-Agent': UA,
        'X-Requested-With': 'XMLHttpRequest',
    }


def _ths_parse_page(html):
    """按表头名解析一页（表头名在两种口径下不同，故不按列序号取）。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    ths = soup.select('thead th') or soup.select('tr th')
    heads = [t.get_text(strip=True) for t in ths]
    if not heads:
        return []
    rows = soup.select('tbody tr')
    out = []
    for tr in rows:
        tds = [td.get_text(strip=True) for td in tr.find_all('td')]
        if len(tds) < len(heads):
            continue
        out.append(dict(zip(heads, tds)))
    return out


def _ths_rows_to_df(rows):
    """表头名 -> 契约列名。两种口径的列名不同，这里统一。"""
    if not rows:
        return pd.DataFrame()

    def pick(d, *names):
        for n in names:
            for k in d:
                if k.startswith(n):
                    return d[k]
        return None

    recs = []
    for r in rows:
        recs.append({
            '股票代码': normalize_code(pick(r, '股票代码')),
            '股票简称': (pick(r, '股票简称') or '').strip(),
            '最新价': parse_number(pick(r, '最新价')),
            '区间涨跌幅': parse_cn_percent(pick(r, '涨跌幅', '阶段涨跌幅')),
            '换手率': parse_cn_percent(pick(r, '换手率', '连续换手率')),
            '区间主力资金流向': parse_cn_amount(pick(r, '净额', '资金流入净额')),
            '成交额': parse_cn_amount(pick(r, '成交额')),
        })

    df = pd.DataFrame(recs)
    df = df[df['股票代码'].notna()].drop_duplicates(subset=['股票代码'])
    return df.reset_index(drop=True)


def _ths_fetch_direct(period):
    """直连 10jqka 抓取全市场（token 复用 + 小并发）。失败返回空 DataFrame。"""
    import concurrent.futures as cf

    board = _THS_BOARD.get(period, '')
    url = _THS_PAGE_URL.format(board=board, page='{}')
    token = _ths_token()

    r = requests.get(url.format(1), headers=_ths_headers(token), timeout=20)
    if r.status_code != 200 or 'tbody' not in r.text:
        print(f'[资金流] 第1页异常 status={r.status_code}')
        return pd.DataFrame()

    rows = _ths_parse_page(r.text)
    if not rows:
        print('[资金流] 第1页解析为空（可能被要求验证）')
        return pd.DataFrame()

    total_pages = 1
    try:
        from bs4 import BeautifulSoup
        info = BeautifulSoup(r.text, 'lxml').find('span', attrs={'class': 'page_info'})
        if info and '/' in info.text:
            total_pages = int(info.text.split('/')[1].strip())
    except Exception:
        pass
    print(f'[资金流] 同花顺 {period}: 共 {total_pages} 页，并发 {_THS_WORKERS} 抓取...')

    all_rows = list(rows)
    failed = []

    def _one(p):
        if _THS_WORKERS <= 1 and _THS_DELAY:
            time.sleep(_THS_DELAY)
        try:
            rr = requests.get(url.format(p), headers=_ths_headers(token), timeout=20)
            if rr.status_code != 200:
                return p, []
            return p, _ths_parse_page(rr.text)
        except Exception:
            return p, []

    if total_pages > 1:
        if _THS_WORKERS <= 1:
            for p in range(2, total_pages + 1):
                p, rs = _one(p)
                if rs:
                    all_rows.extend(rs)
                else:
                    failed.append(p)
        else:
            with cf.ThreadPoolExecutor(max_workers=_THS_WORKERS) as ex:
                for p, rs in ex.map(_one, range(2, total_pages + 1)):
                    if rs:
                        all_rows.extend(rs)
                    else:
                        failed.append(p)

    # 失败页重试：换新 token + 逐步放慢（限流有累积性，硬重试只会更糟）
    if failed:
        print(f'[资金流] {len(failed)} 页失败，换 token 并放慢重试')
        token = _ths_token()
        still = []
        for p in failed:
            time.sleep(_THS_DELAY * 2)
            try:
                rr = requests.get(url.format(p), headers=_ths_headers(token), timeout=20)
                rs = _ths_parse_page(rr.text) if rr.status_code == 200 else []
            except Exception:
                rs = []
            if rs:
                all_rows.extend(rs)
            else:
                still.append(p)

        # 仍失败的页，再等一轮（限流通常几十秒内解除）
        if still:
            print(f'[资金流] 仍缺 {len(still)} 页，等待 30s 后补抓')
            time.sleep(30)
            token = _ths_token()
            for p in still:
                time.sleep(_THS_DELAY)
                try:
                    rr = requests.get(url.format(p), headers=_ths_headers(token), timeout=20)
                    rs = _ths_parse_page(rr.text) if rr.status_code == 200 else []
                    if rs:
                        all_rows.extend(rs)
                except Exception:
                    pass

    return _ths_rows_to_df(all_rows)


def _ths_fetch_akshare(period):
    """兜底：走 akshare（慢约 4 倍，但实现简单）"""
    import akshare as ak
    raw = ak.stock_fund_flow_individual(symbol=THS_PERIOD_MAP.get(period, '即时'))
    if raw is None or raw.empty:
        return pd.DataFrame()
    recs = []
    for _, row in raw.iterrows():
        d = {str(k): v for k, v in row.items()}
        recs.append(d)
    return _ths_rows_to_df(recs)


def ths_fund_flow(period='今日'):
    """全市场个股资金流排行（同花顺）。

    Args:
        period: '今日' / '3日' / '5日' / '10日' / '20日'

    Returns:
        DataFrame，列：
            股票代码(6位str) 股票简称 最新价 区间涨跌幅 区间主力资金流向
            换手率 成交额
        失败返回空 DataFrame。
    """
    def _fetch():
        try:
            df = _ths_fetch_direct(period)
        except Exception as e:
            print(f'[资金流] 直连异常: {type(e).__name__}: {e}')
            df = pd.DataFrame()
        if df.empty:
            print('[资金流] 直连不可用，回退 akshare')
            try:
                df = _ths_fetch_akshare(period)
            except Exception as e:
                print(f'[资金流] akshare 兜底也失败: {type(e).__name__}')
                df = pd.DataFrame()
        return df

    return _cached(f'ths_ff_{period}', TTL_FUNDFLOW, _fetch)


# ----------------------------------------------------------------------------
# 新浪：全市场资金流（仅今日口径，但快且稳，且含超大单=真主力）
# ----------------------------------------------------------------------------

_SINA_FF_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
                'MoneyFlow.ssl_bkzj_ssggzj'
                '?page={page}&num=1000&sort=netamount&asc=0&bankuai=&shangzhang=')
_SINA_MAX_PAGES = 12   # 安全上限（全市场约 7 页）


def _sina_is_a_share(symbol):
    """Sina 的 symbol 带市场前缀，需剔掉 ETF / B股 / 转债，只留沪深A股。"""
    return symbol.startswith(('sh6', 'sz0', 'sz3'))


def sina_fund_flow_today():
    """新浪全市场个股资金流（今日）。

    为什么它比同花顺更适合做今日口径：
        * 全市场只需 7 个请求（同花顺要 105 个），且实测不限流
        * 返回 `r0_net` 超大单净额 —— 语义上就是"主力资金净流入"

    Returns:
        DataFrame，列：
            股票代码 股票简称 最新价 区间涨跌幅 区间主力资金流向 换手率 成交额
    """
    def _fetch():
        rows = []
        for page in range(1, _SINA_MAX_PAGES + 1):
            try:
                r = requests.get(_SINA_FF_URL.format(page=page), timeout=30,
                                 headers={'User-Agent': UA,
                                          'Referer': 'https://finance.sina.com.cn/'})
                data = json.loads(r.text)
            except Exception as e:
                print(f'[资金流] 新浪第 {page} 页失败: {type(e).__name__}')
                data = []
            if not data:
                break
            rows.extend(data)
            time.sleep(0.3)

        if not rows:
            return pd.DataFrame()

        recs = []
        for x in rows:
            sym = str(x.get('symbol', ''))
            if not _sina_is_a_share(sym):
                continue
            recs.append({
                '股票代码': normalize_code(sym[2:]),
                '股票简称': str(x.get('name', '')).strip(),
                '最新价': parse_number(x.get('trade')),
                # changeratio 是小数（0.00017 = 0.017%），要 ×100
                '区间涨跌幅': (lambda v: None if v is None else round(v * 100, 4))(
                    parse_number(x.get('changeratio'))),
                # turnover 的单位是 0.01%（实测与腾讯换手率÷100 吻合：
                # 万科A 824.439 -> 8.244% vs 腾讯 8.260%）
                '换手率': (lambda v: None if v is None else round(v / 100, 4))(
                    parse_number(x.get('turnover'))),
                '区间主力资金流向': parse_number(x.get('r0_net')),
                '成交额': parse_number(x.get('amount')),
            })

        df = pd.DataFrame(recs)
        df = df[df['股票代码'].notna()].drop_duplicates(subset=['股票代码'])
        print(f'[资金流] 新浪今日: {len(df)} 只 A 股')
        return df.reset_index(drop=True)

    return _cached('sina_ff_today', TTL_FUNDFLOW, _fetch)


def fund_flow(period='今日'):
    """资金流统一入口。

    今日 -> 新浪（快、稳、含超大单净额）
    其余 -> 同花顺（唯一提供区间口径的可用源，较慢）

    Args:
        period: '今日' / '3日' / '5日' / '10日' / '20日'
    """
    if period == '今日':
        df = sina_fund_flow_today()
        if not df.empty:
            return df
        print('[资金流] 新浪今日不可用，改走同花顺')
    return ths_fund_flow(period)


# ----------------------------------------------------------------------------
# 腾讯：批量行情
# ----------------------------------------------------------------------------

def tencent_quotes(codes):
    """批量行情（腾讯 qt.gtimg.cn）。

    Returns:
        DataFrame，列：
            股票代码 最新价 涨跌幅 成交额 换手率 市盈率 市净率 总市值 流通市值
        市值单位为【元】（腾讯原本给的是亿元，这里乘 1e8 对齐 iwencai 口径）。
    """
    codes = [c for c in (normalize_code(x) for x in codes) if c]
    codes = list(dict.fromkeys(codes))
    if not codes:
        return pd.DataFrame()

    rows = []
    for i in range(0, len(codes), TENCENT_BATCH):
        batch = codes[i:i + TENCENT_BATCH]
        q = ','.join(f'{market_prefix(c)}{c}' for c in batch)
        try:
            r = requests.get(f'https://qt.gtimg.cn/q={q}', timeout=20,
                             headers={'User-Agent': UA})
            r.encoding = 'gbk'
            for line in r.text.split('\n'):
                if '="' not in line:
                    continue
                body = line.split('="', 1)[1].rstrip('";\r\n ')
                f = body.split('~')
                if len(f) < 50:
                    continue
                rows.append({
                    '股票代码': normalize_code(f[2]),
                    '股票简称': f[1].strip(),
                    '最新价': parse_number(f[3]),
                    '涨跌幅': parse_number(f[32]),
                    '成交额': (parse_number(f[37]) or 0) * 1e4,   # 腾讯给的是万元
                    '换手率': parse_number(f[38]),
                    '市盈率': parse_number(f[39]),
                    '市净率': parse_number(f[46]),
                    '流通市值': (parse_number(f[44]) or 0) * 1e8,  # 腾讯给的是亿元
                    '总市值': (parse_number(f[45]) or 0) * 1e8,
                })
        except Exception as e:
            print(f'[腾讯行情] 批次 {i // TENCENT_BATCH + 1} 失败: {type(e).__name__}')
        time.sleep(0.2)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df[df['股票代码'].notna()].drop_duplicates(subset=['股票代码'])
    return df.reset_index(drop=True)


def _quotes_cached(codes):
    """带缓存的批量行情。key 用排序后的代码串，保证同样一批命中同一缓存。"""
    codes = sorted({c for c in (normalize_code(x) for x in codes) if c})
    if not codes:
        return pd.DataFrame()
    # 代码集合可能很大，用长度+首尾做 key 足够（同一轮选股内集合稳定）
    key = f'q_{len(codes)}_{codes[0]}_{codes[-1]}_{hash(tuple(codes[:50]))}'
    return _cached(key, TTL_QUOTE, lambda: tencent_quotes(codes))


# ----------------------------------------------------------------------------
# 东财 datacenter：财务指标
# ----------------------------------------------------------------------------

def latest_report_date(today=None):
    """推导最新可用报告期。

    今天 2026-09-29 -> '2026-06-30'（中报）。
    规则：1/2/3月用上年三季报，4月及以后用当年一季报，7月及以后用当年中报，
    10月及以后用当年三季报。
    """
    d = today or datetime.now()
    y, m = d.year, d.month
    if m >= 10:
        return f'{y}-09-30'
    if m >= 7:
        return f'{y}-06-30'
    if m >= 4:
        return f'{y}-03-31'
    return f'{y - 1}-09-30'


def _chunk(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _pages_for(n_codes, rows_per_code, page_size=EM_MAX_DATACENTER_PAGE, min_pages=2,
               max_pages=40):
    """按代码数估算需要翻几页（datacenter 的 max_pages 安全上限）"""
    need = (n_codes * rows_per_code + page_size - 1) // page_size
    return max(min_pages, min(max_pages, need))


def _filter_date_window(df, col, start, end):
    """本地按日期区间过滤（服务端对日期区间条件是静默拒绝的，见 em_lift_stage）"""
    if df is None or df.empty or col not in df.columns:
        return df if df is not None else pd.DataFrame()
    d = pd.to_datetime(df[col], errors='coerce').dt.date
    keep = d.notna() & (d >= start) & (d <= end)
    return df[keep].reset_index(drop=True)


def _fetch_report_all(report_name, date_field, report_date, columns):
    """整表拉取某报告期的全部个股（约 12 页），失败返回空 DataFrame。"""
    data = _client.datacenter_all(
        report_name,
        columns=columns,
        filter_expr=f"({date_field}='{report_date}')",
        page_size=EM_MAX_DATACENTER_PAGE)
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data).rename(columns={'SECURITY_CODE': '股票代码'})
    df['股票代码'] = df['股票代码'].map(normalize_code)
    return df.drop_duplicates(subset=['股票代码']).reset_index(drop=True)


def em_financials_all(report_date=None):
    """全市场业绩报表：ROE / 销售毛利率 / 净利同比 / 营收同比 / 股息率 / 营收 / 净利润。

    一次拉全并缓存 1 小时。比按代码逐批请求（53 批）快一个数量级 ——
    全表约 12 页。

    Returns:
        DataFrame，列：股票代码 WEIGHTAVG_ROE XSMLL SJLTZ YSTZ ZXGXL
                      TOTAL_OPERATE_INCOME PARENT_NETPROFIT
    """
    rd = report_date or latest_report_date()
    return _cached(f'em_fin_all_{rd}', TTL_FINANCE, lambda: _fetch_report_all(
        'RPT_LICO_FN_CPD', 'REPORTDATE', rd,
        ['SECURITY_CODE', 'SECURITY_NAME_ABBR', 'REPORTDATE',
         'WEIGHTAVG_ROE', 'XSMLL', 'SJLTZ', 'YSTZ', 'ZXGXL',
         'TOTAL_OPERATE_INCOME', 'PARENT_NETPROFIT']))


def em_balance_all(report_date=None):
    """全市场资产负债表：资产负债率 + 行业。

    ⚠️ 该报表用的字段是 REPORT_DATE（带下划线），与 RPT_LICO_FN_CPD 的
    REPORTDATE 不同，不能混用。

    Returns:
        DataFrame，列：股票代码 DEBT_ASSET_RATIO INDUSTRY_NAME
    """
    rd = report_date or latest_report_date()
    return _cached(f'em_bal_all_{rd}', TTL_FINANCE, lambda: _fetch_report_all(
        'RPT_DMSK_FN_BALANCE', 'REPORT_DATE', rd,
        ['SECURITY_CODE', 'SECURITY_NAME_ABBR', 'REPORT_DATE',
         'DEBT_ASSET_RATIO', 'INDUSTRY_NAME']))


def em_financials(codes, report_date=None):
    """按代码取业绩报表（从全量缓存里筛，不为少量代码单独发请求）"""
    all_df = em_financials_all(report_date)
    if all_df.empty:
        return all_df
    want = {c for c in (normalize_code(x) for x in codes) if c}
    return all_df[all_df['股票代码'].isin(want)].reset_index(drop=True)


def em_balance(codes, report_date=None):
    """按代码取资产负债表（从全量缓存里筛）"""
    all_df = em_balance_all(report_date)
    if all_df.empty:
        return all_df
    want = {c for c in (normalize_code(x) for x in codes) if c}
    return all_df[all_df['股票代码'].isin(want)].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 东财 datacenter：解禁 / 增减持
# ----------------------------------------------------------------------------

def em_lift_stage(codes, days_forward=90):
    """限售解禁（东财 RPT_LIFT_STAGE）。

    ⚠️ 日期范围必须【本地过滤】，不能交给服务端：
       实测 `(FREE_DATE>='2026-09-29')` 这类区间条件会被**静默拒绝**，
       返回 count=0 而不是报错 —— 看起来像"没有解禁"，其实全市场未来
       90 天有 447 条。只有 `SECURITY_CODE in (...)` 这种精确条件可靠。
       故这里只按代码取全量历史（每只几十条），再本地按日期筛。

    Returns:
        DataFrame，列：股票代码 SECURITY_NAME_ABBR FREE_DATE FREE_SHARES
                      LIFT_MARKET_CAP FREE_RATIO
    """
    codes = sorted({c for c in (normalize_code(x) for x in codes) if c})
    if not codes:
        return pd.DataFrame()

    today = datetime.now().date()
    end = today + timedelta(days=days_forward)

    def _fetch():
        rows = []
        for chunk in _chunk(codes, EM_IN_BATCH):
            in_list = '","'.join(chunk)
            rows.extend(_client.datacenter_all(
                'RPT_LIFT_STAGE',
                columns=['SECURITY_CODE', 'SECURITY_NAME_ABBR', 'FREE_DATE',
                         'FREE_SHARES', 'LIFT_MARKET_CAP', 'FREE_RATIO'],
                filter_expr=f'(SECURITY_CODE in ("{in_list}"))',
                page_size=EM_MAX_DATACENTER_PAGE,
                max_pages=_pages_for(len(chunk), 60),
                sort_columns='FREE_DATE', sort_types=1))
            time.sleep(0.25)

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).rename(columns={'SECURITY_CODE': '股票代码'})
        df['股票代码'] = df['股票代码'].map(normalize_code)
        return df.reset_index(drop=True)

    # key 用「首末代码 + 全量哈希」，不能用 len(codes)——
    # 否则同样只有 1 只股票的不同调用会命中同一缓存，返回上一只的数据。
    key = f'em_lift_{today}_{end}_{len(codes)}_{codes[0]}_{codes[-1]}_{hash(tuple(codes))}'
    df = _cached(key, TTL_FINANCE, _fetch)
    return _filter_date_window(df, 'FREE_DATE', today, end)


def em_holder_increase(codes, days_back=90):
    """股东增减持（东财 RPT_SHARE_HOLDER_INCREASE）。

    ⚠️ 同 em_lift_stage：`NOTICE_DATE` 的区间条件会被服务端静默拒绝（返回 0），
       必须按代码取全量后本地筛日期。

    Returns:
        DataFrame，列：股票代码 SECURITY_NAME_ABBR NOTICE_DATE HOLDER_NAME
                      DIRECTION CHANGE_NUM CHANGE_RATE
    """
    codes = sorted({c for c in (normalize_code(x) for x in codes) if c})
    if not codes:
        return pd.DataFrame()

    today = datetime.now().date()
    start = today - timedelta(days=days_back)

    def _fetch():
        rows = []
        for chunk in _chunk(codes, EM_IN_BATCH):
            in_list = '","'.join(chunk)
            rows.extend(_client.datacenter_all(
                'RPT_SHARE_HOLDER_INCREASE',
                columns=['SECURITY_CODE', 'SECURITY_NAME_ABBR', 'NOTICE_DATE',
                         'HOLDER_NAME', 'DIRECTION', 'CHANGE_NUM', 'CHANGE_RATE'],
                filter_expr=f'(SECURITY_CODE in ("{in_list}"))',
                page_size=EM_MAX_DATACENTER_PAGE,
                max_pages=_pages_for(len(chunk), 60)))
            time.sleep(0.25)

        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).rename(columns={'SECURITY_CODE': '股票代码'})
        df['股票代码'] = df['股票代码'].map(normalize_code)
        return df.reset_index(drop=True)

    # key 同上：必须含真实代码，不能用 len(codes)
    key = f'em_hold_{start}_{len(codes)}_{codes[0]}_{codes[-1]}_{hash(tuple(codes))}'
    df = _cached(key, TTL_FINANCE, _fetch)
    return _filter_date_window(df, 'NOTICE_DATE', start, today)


def em_announcements(code, limit=20):
    """个股公告（东财 np-anotice-stock）"""
    code = normalize_code(code)
    if not code:
        return []

    def _fetch():
        items = _client.notice_page(code, page_size=limit)
        out = []
        for n in items:
            cols = n.get('columns') or [{}]
            out.append({
                'title': (n.get('title') or '').strip(),
                'date': (n.get('notice_date') or '')[:10],
                'column': cols[0].get('column_name', '') if cols else '',
                'url': f'https://data.eastmoney.com/notices/detail/{code}/{n.get("art_code", "")}.html',
            })
        return out

    return _cached(f'em_ann_{code}_{limit}', TTL_FINANCE, _fetch)


# ----------------------------------------------------------------------------
# 契约：规范列顺序
# ----------------------------------------------------------------------------

# 顺序是硬约束，不是风格问题 —— 下游多处用 [col for col in df.columns if ...][0] 取值：
#   * main_force_analysis.py:161 / main_force_pdf_generator.py:164 取 '涨跌幅' 的首个匹配
#     → 『区间涨跌幅』必须排在『涨跌幅』之前
#   * main_force_analysis.py:151 取 ('主力' in col and '净流入' in col) 的首个匹配
#     → 绝不能暴露『今日主力净流入』/『5日主力净流入』这类列
CANONICAL_COLUMNS = [
    '股票代码',
    '股票简称',
    '所属同花顺行业',
    '最新价',                  # low_price_bull_ui.py:421 用 row.get('股价', row.get('最新价', 0))，
                               # 规范名存在即可，无需再补 '股价' 别名
    '区间涨跌幅',               # 必须早于 '涨跌幅'
    '涨跌幅',
    '成交额',
    '换手率',
    '区间主力资金流向',          # 不得再暴露『今日主力净流入』等，否则 :151 取 [0] 会串列
    '总市值',
    '流通市值',
    '市盈率',
    '市净率',
    '股息率',
    '资产负债率',
    '净利润增长率',             # 消费方一律写成 row.get('净利润增长率', row.get('净利润同比增长率', ...))，
    '营收增长率',               # 规范名优先命中，故不提供别名列（别名只会在 AI 表格里造成重复列）
    '营业收入',
    '净利润',
    '加权ROE',
    '销售毛利率',
]


def enforce_contract(df):
    """把 DataFrame 调整为规范列名/列顺序，补齐缺失列（缺失填 None）。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    out = df.copy()
    for col in CANONICAL_COLUMNS:
        if col not in out.columns:
            out[col] = None
    return out[CANONICAL_COLUMNS]


# ----------------------------------------------------------------------------
# 编排：构建候选池
# ----------------------------------------------------------------------------

def build_universe(period='今日', extra_codes=None, with_financials=True):
    """构建选股候选池。

    流程：
        1. 同花顺取全市场资金流排行（已是全市场，天然是"池"）
        2. 腾讯补行情（市值/PE/PB/成交额/换手率）
        3. 东财补财务（ROE/毛利率/增长率/股息率/负债率/行业）
        4. 统一为契约列名

    Args:
        period: '今日' / '3日' / '5日' / '10日' / '20日'
        extra_codes: 额外要并入的代码（保证这些一定在池中）
        with_financials: 是否补财务（关掉可显著加速）

    Returns:
        DataFrame（契约列名）；完全失败返回空 DataFrame
    """
    ff = fund_flow(period)
    if ff.empty:
        print('[选股池] ⚠️ 资金流数据获取失败')
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    codes = set(ff['股票代码'].dropna().tolist())
    if extra_codes:
        codes |= {c for c in (normalize_code(x) for x in extra_codes) if c}
    codes = sorted(codes)

    print(f'[选股池] 同花顺资金流 {len(ff)} 只（{period}），待补数据 {len(codes)} 只')

    df = ff

    # 行情
    q = _quotes_cached(codes)
    if not q.empty:
        df = df.merge(q, on='股票代码', how='left', suffixes=('', '_q'))
        # 腾讯的名称/最新价更权威，覆盖同花顺的
        if '股票简称_q' in df.columns:
            df['股票简称'] = df['股票简称_q'].fillna(df['股票简称'])
            df = df.drop(columns=['股票简称_q'])
        if '最新价_q' in df.columns:
            df['最新价'] = df['最新价_q'].fillna(df['最新价'])
            df = df.drop(columns=['最新价_q'])
    else:
        print('[选股池] ⚠️ 腾讯行情获取失败，市值/PE/PB 将缺失')

    # 财务
    if with_financials:
        fin = em_financials(codes)
        if not fin.empty:
            df = df.merge(
                fin[['股票代码', 'WEIGHTAVG_ROE', 'XSMLL', 'SJLTZ', 'YSTZ',
                     'ZXGXL', 'TOTAL_OPERATE_INCOME', 'PARENT_NETPROFIT']],
                on='股票代码', how='left')
            df = df.rename(columns={
                'WEIGHTAVG_ROE': '加权ROE',
                'XSMLL': '销售毛利率',
                'SJLTZ': '净利润增长率',
                'YSTZ': '营收增长率',
                'ZXGXL': '股息率',
                'TOTAL_OPERATE_INCOME': '营业收入',
                'PARENT_NETPROFIT': '净利润',
            })
        else:
            print('[选股池] ⚠️ 东财财务数据获取失败')

        bal = em_balance(codes)
        if not bal.empty:
            df = df.merge(
                bal[['股票代码', 'DEBT_ASSET_RATIO', 'INDUSTRY_NAME']],
                on='股票代码', how='left')
            df = df.rename(columns={
                'DEBT_ASSET_RATIO': '资产负债率',
                'INDUSTRY_NAME': '所属同花顺行业',
            })
        else:
            print('[选股池] ⚠️ 东财资产负债表获取失败')

    df = df[df['股票代码'].notna()].drop_duplicates(subset=['股票代码'])
    return enforce_contract(df).reset_index(drop=True)


# ----------------------------------------------------------------------------
# 声明式选股
# ----------------------------------------------------------------------------

def _apply_numeric(df, col, *, ge=None, le=None):
    """按数值上下限过滤。

    ⚠️ 缺失值一律视为【不满足】。理由：东财报表里没有某字段通常意味着
    该指标不适用（如不分红就没有股息率），放行会得出"股息率≥1%"的假阳性。
    """
    if col not in df.columns:
        print(f'[选股] ⚠️ 缺少字段 {col}，该条件已跳过')
        return df
    v = pd.to_numeric(df[col], errors='coerce')
    mask = v.notna()
    if ge is not None:
        mask &= (v >= ge)
    if le is not None:
        mask &= (v <= le)
    return df[mask]


def screen(period='今日', *, exclude_st=True, exclude_boards=(), markets=None,
           min_price=None, max_price=None,
           min_market_cap=None, max_market_cap=None, cap_in_yi=True,
           min_net_profit_growth=None, min_revenue_growth=None,
           max_pe=None, max_pb=None, min_dividend_yield=None,
           max_debt_ratio=None,
           sort_by=None, ascending=False, top_n=None, with_financials=True):
    """声明式选股：建池 -> 逐条过滤 -> 排序 -> 取前 N。

    所有阈值条件中的缺失值都不通过（见 _apply_numeric）。

    Args:
        period:    '今日'/'3日'/'5日'/'10日'/'20日'
        exclude_boards: 要排除的板块，如 ['科创板', '创业板']
        markets:   要保留的交易所，如 ['sz']；None 表示不限
        min/max_market_cap: 市值区间，默认单位【亿】（cap_in_yi=True）
        sort_by:   排序字段；None 时按主力资金净流入降序
        top_n:     返回条数

    Returns:
        DataFrame（契约列名）；失败返回空 DataFrame
    """
    df = build_universe(period=period, with_financials=with_financials)
    if df is None or df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    n0 = len(df)
    steps = []

    if exclude_st:
        df = df[~df['股票简称'].apply(is_st)]
        steps.append(('非ST', len(df)))

    if exclude_boards:
        df = df[~df['股票代码'].apply(lambda c: board_of(c) in exclude_boards)]
        steps.append((f"排除{'/'.join(exclude_boards)}", len(df)))

    if markets:
        df = df[df['股票代码'].apply(lambda c: exchange_of(c) in markets)]
        steps.append((f"仅{'/'.join(markets)}", len(df)))

    if min_price is not None or max_price is not None:
        df = _apply_numeric(df, '最新价', ge=min_price, le=max_price)
        steps.append((f"股价 {min_price}~{max_price}", len(df)))

    if min_market_cap is not None or max_market_cap is not None:
        cap = pd.to_numeric(df['总市值'], errors='coerce')
        if cap_in_yi:
            cap = cap / 1e8
        mask = cap.notna()
        if min_market_cap is not None:
            mask &= (cap >= min_market_cap)
        if max_market_cap is not None:
            mask &= (cap <= max_market_cap)
        df = df[mask]
        steps.append((f"市值 {min_market_cap}~{max_market_cap}亿", len(df)))

    if min_net_profit_growth is not None:
        df = _apply_numeric(df, '净利润增长率', ge=min_net_profit_growth)
        steps.append((f"净利增长≥{min_net_profit_growth}%", len(df)))

    if min_revenue_growth is not None:
        df = _apply_numeric(df, '营收增长率', ge=min_revenue_growth)
        steps.append((f"营收增长≥{min_revenue_growth}%", len(df)))

    if max_pe is not None:
        df = _apply_numeric(df, '市盈率', ge=0, le=max_pe)
        steps.append((f"市盈率≤{max_pe}", len(df)))

    if max_pb is not None:
        df = _apply_numeric(df, '市净率', ge=0, le=max_pb)
        steps.append((f"市净率≤{max_pb}", len(df)))

    if min_dividend_yield is not None:
        df = _apply_numeric(df, '股息率', ge=min_dividend_yield)
        steps.append((f"股息率≥{min_dividend_yield}%", len(df)))

    if max_debt_ratio is not None:
        df = _apply_numeric(df, '资产负债率', le=max_debt_ratio)
        steps.append((f"负债率≤{max_debt_ratio}%", len(df)))

    print(f'[选股] 过滤: {n0} -> ' + ' -> '.join(f'{n}({c})' for n, c in steps))

    sort_col = sort_by or '区间主力资金流向'
    if sort_col in df.columns:
        df = df.copy()
        df[sort_col] = pd.to_numeric(df[sort_col], errors='coerce')
        df = df.sort_values(sort_col, ascending=ascending, na_position='last')

    if top_n:
        df = df.head(top_n)

    return df.reset_index(drop=True)


# ----------------------------------------------------------------------------
# 降级分支：原 iwencai 链路
# ----------------------------------------------------------------------------

def pywencai_fallback(query):
    """原 iwencai（问财）链路，作为降级分支保留。

    上游 `POST /customized/chart/get-robot-data` 目前对程序化请求一律返回
    403（即便带真实登录 cookie），故此路径基本不会成功。保留它是为了上游
    恢复时能自动接回，无需再改代码。

    Returns:
        DataFrame，任何失败都返回 None（**不再抛异常**，也不再返回 dict ——
        原来各模块直接拿返回值调 `.empty`，dict 会 AttributeError）。
    """
    try:
        from utils.pywencai_helper import safe_get
    except Exception as e:
        print(f'[选股] pywencai 不可用: {e}')
        return None

    try:
        result = safe_get(query=query, loop=True)
    except Exception as e:
        print(f'[选股] pywencai 调用失败: {type(e).__name__}: {e}')
        return None

    if result is None:
        return None
    if isinstance(result, pd.DataFrame):
        return None if result.empty else result
    if isinstance(result, list):
        df = pd.DataFrame(result)
        return None if df.empty else df
    if isinstance(result, dict):
        for key in ('tableV1', 'data', 'result'):
            t = result.get(key)
            if t is None:
                continue
            df = t if isinstance(t, pd.DataFrame) else pd.DataFrame(t)
            return None if df.empty else df
    print(f'[选股] pywencai 返回未知格式: {type(result)}')
    return None
