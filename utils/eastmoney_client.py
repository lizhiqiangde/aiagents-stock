"""
东方财富数据接口客户端（传输层）

职责：主机轮换、节流、重试、JSON 解析、分页原语。不含任何业务语义。

背景（为什么要做主机轮换）：
    push2.eastmoney.com 实际是一个【分片主机池】。实测同一时刻
    push2.eastmoney.com 与 push2his.eastmoney.com 可用，而
    1.push2 / 7.push2 / push2delay / 82.push2 全部连接被重置。
    这些域名 DNS 均正常且解析到【不同 IP】，失败发生在连接层。
    可用主机的集合随时间和所在网络变化，因此不能硬编码"好主机"，
    必须做运行时健康度追踪，把失败主机自动降级到队尾。

    README.md 也曾记录 push2 因本机 IP 被封锁导致 RemoteDisconnected。

关于重试：这里实现了「主机轮换 + 指数退避」，本身已覆盖重试语义，
    故不再叠加 utils/akshare_helper.retry_on_failure（6 台主机 × 3 次
    会放大到 18 次请求，反而更容易触发封禁）。

用法：
    from utils.eastmoney_client import EastmoneyClient
    client = EastmoneyClient()
    rows, total = client.clist_page('m:0+t:6,m:1+t:2', ['f12', 'f14'], fid='f6', po=0)
"""

import sys
import io
import json
import time
import random
import threading
import warnings

import requests

# 复用仓库既有的请求补丁（浏览器 UA + Referer + 默认超时）
from utils.akshare_helper import patch_requests

patch_requests()
warnings.filterwarnings('ignore')


def _setup_stdout_encoding():
    """仅在命令行环境把 stdout 设为 UTF-8，避免 GBK 控制台在 emoji 上崩溃。

    在 Streamlit 环境下不修改（与 qstock_news_data.py 的做法一致）。
    """
    if sys.platform != 'win32':
        return
    try:
        import streamlit  # noqa: F401
        return
    except ImportError:
        pass
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass


_setup_stdout_encoding()


# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

# push2 分片主机池（顺序即初始优先级，运行时会按健康度重排）
EM_PUSH2_HOSTS = [
    'push2.eastmoney.com',
    'push2his.eastmoney.com',
    '1.push2.eastmoney.com',
    '7.push2.eastmoney.com',
    'push2delay.eastmoney.com',
    '82.push2.eastmoney.com',
]

EM_DATA_HOST = 'datacenter-web.eastmoney.com'
EM_NOTICE_HOST = 'np-anotice-stock.eastmoney.com'

# 单页上限：实测 clist 传再大也只返回 100 行；datacenter 上限 500。
# 传超大值（如 pz=6000）会直接触发 RemoteDisconnected，故写死并断言。
EM_MAX_CLIST_PAGE = 100
EM_MAX_DATACENTER_PAGE = 500

EM_MIN_INTERVAL = 0.35   # 全局最小请求间隔（秒）
EM_TIMEOUT = 15.0
EM_MAX_RETRIES = 3
EM_BACKOFF = 1.8         # 退避基数，带抖动

# 一台主机连续失败多少次后降级到队尾
EM_HOST_FAIL_THRESHOLD = 2


class EastmoneyError(Exception):
    """东方财富接口全部主机/重试均失败"""


class EastmoneyClient:
    """东方财富 HTTP 客户端：主机轮换 + 节流 + 重试"""

    def __init__(self, min_interval=EM_MIN_INTERVAL, timeout=EM_TIMEOUT,
                 max_retries=EM_MAX_RETRIES, verbose=True):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose

        # 运行时可变的 push2 主机优先顺序
        self._push2_hosts = list(EM_PUSH2_HOSTS)
        # 每台主机的连续失败次数
        self._host_fails = {}
        # 节流状态
        self._lock = threading.Lock()
        self._last_call_ts = 0.0

        self._session = requests.Session()

    # -- 内部工具 ----------------------------------------------------------

    def _log(self, msg):
        if self.verbose:
            print(msg)

    def _throttle(self):
        """全局最小请求间隔（线程安全）"""
        with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_call_ts = time.time()

    def _mark_host_ok(self, host):
        with self._lock:
            self._host_fails[host] = 0
            # sticky：成功的主机移到队首，减少无谓探测
            if host in self._push2_hosts and self._push2_hosts[0] != host:
                self._push2_hosts.remove(host)
                self._push2_hosts.insert(0, host)

    def _mark_host_fail(self, host):
        with self._lock:
            fails = self._host_fails.get(host, 0) + 1
            self._host_fails[host] = fails
            if fails >= EM_HOST_FAIL_THRESHOLD and host in self._push2_hosts:
                self._push2_hosts.remove(host)
                self._push2_hosts.append(host)
                self._log(f'[东财] 主机 {host} 连续失败 {fails} 次，降级到队尾')

    def _order_hosts(self, hosts):
        """把给定的主机列表按当前健康度排序（healthier 在前）"""
        if hosts is not None:
            return list(hosts)
        # 未指定则用 push2 池（已按健康度维护顺序）
        with self._lock:
            return list(self._push2_hosts)

    def get_json(self, path, params=None, hosts=None, retries=None):
        """按主机优先顺序轮换请求，返回解析后的 JSON dict。

        Args:
            path:    URL 路径，如 '/api/qt/clist/get'
            params:  查询参数
            hosts:   显式指定主机列表（如 [EM_DATA_HOST]）；None 表示用 push2 池
            retries: 覆盖默认重试次数

        Returns:
            dict | None — 全部主机失败时返回 None（不抛异常，便于调用方降级）

        Raises:
            EastmoneyError: 仅在 retries 用尽且 strict=True 时（本方法不抛）
        """
        host_list = self._order_hosts(hosts)
        max_retries = self.max_retries if retries is None else retries
        params = params or {}

        last_err = None
        for attempt in range(max_retries):
            for host in self._order_hosts(host_list):
                url = f'https://{host}{path}'
                try:
                    self._throttle()
                    resp = self._session.get(url, params=params, timeout=self.timeout)

                    if resp.status_code != 200:
                        last_err = f'HTTP {resp.status_code}'
                        self._mark_host_fail(host)
                        continue

                    # 空响应体也要当失败处理（实测封禁的表现之一）
                    if not resp.text or not resp.text.strip():
                        last_err = '空响应体'
                        self._mark_host_fail(host)
                        continue

                    data = json.loads(resp.text)
                    self._mark_host_ok(host)
                    return data

                except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
                    last_err = f'{type(e).__name__}: {e}'
                    self._mark_host_fail(host)
                    continue

            if attempt < max_retries - 1:
                delay = EM_BACKOFF ** attempt + random.uniform(0, 0.4)
                self._log(f'[东财] 第{attempt + 1}轮全部主机失败（{last_err}），{delay:.1f}s 后重试')
                time.sleep(delay)

        self._log(f'[东财] 请求失败: {path} — {last_err}')
        return None

    # -- push2 原语 --------------------------------------------------------

    def clist_page(self, fs, fields, fid=None, po=1, page=1, page_size=EM_MAX_CLIST_PAGE):
        """榜单/行情分页查询。

        Args:
            fs:     市场过滤表达式，如 'm:0+t:6,m:1+t:2'
            fields: 字段列表，如 ['f12', 'f14', 'f2']
            fid:    排序字段（如 'f62' 主力净流入、'f6' 成交额、'f20' 总市值）
            po:     排序方向，1=降序，0=升序
            page:   页码，从 1 开始
            page_size: 每页条数，上限 EM_MAX_CLIST_PAGE（超出会被服务端截断为 100）

        Returns:
            (rows: list[dict], total: int) — 失败时返回 ([], 0)
        """
        assert page_size <= EM_MAX_CLIST_PAGE, f'clist 单页上限 {EM_MAX_CLIST_PAGE}'

        params = {
            'np': '1',
            'fltt': '2',
            'invt': '2',
            'fs': fs,
            'fields': ','.join(fields),
            'pn': str(page),
            'pz': str(page_size),
        }
        if fid:
            params['fid'] = fid
            params['po'] = str(po)

        data = self.get_json('/api/qt/clist/get', params)
        if not data:
            return [], 0

        d = data.get('data') or {}
        return (d.get('diff') or []), int(d.get('total') or 0)

    def ulist(self, secids, fields):
        """批量行情查询。

        Args:
            secids: ['1.600000', '0.000001'] — 前缀 1.=沪 0.=深
            fields: 字段列表

        Returns:
            list[dict] — 失败或空时返回 []
        """
        secids = list(dict.fromkeys(secids))  # 去重且保序
        if not secids:
            return []

        data = self.get_json('/api/qt/ulist.np/get', {
            'secids': ','.join(secids),
            'fltt': '2',
            'invt': '2',
            'np': '1',
            'fields': ','.join(fields),
        })
        if not data:
            return []
        return ((data.get('data') or {}).get('diff') or [])

    # -- datacenter 原语 ---------------------------------------------------

    def datacenter_page(self, report_name, columns='ALL', filter_expr=None,
                        page_number=1, page_size=EM_MAX_DATACENTER_PAGE,
                        sort_columns=None, sort_types=None):
        """东财数据中心报表分页查询。

        Args:
            report_name:  如 'RPT_LICO_FN_CPD'（业绩报表）
            columns:      'ALL' 或字段名列表（传列表可显著减小响应体）
            filter_expr:  服务端过滤，如 "(REPORTDATE='2026-06-30')(SJLTZ>=100)"
                          支持 (SECURITY_CODE in ("600000","000001")) 批量
            page_number:  页码，从 1 开始
            page_size:    每页条数，上限 EM_MAX_DATACENTER_PAGE
            sort_columns / sort_types: 排序字段与方向

        Returns:
            (rows: list[dict], count: int) — 失败时返回 ([], 0)
        """
        assert page_size <= EM_MAX_DATACENTER_PAGE, \
            f'datacenter 单页上限 {EM_MAX_DATACENTER_PAGE}'

        cols = columns if isinstance(columns, str) else ','.join(columns)
        params = {
            'reportName': report_name,
            'columns': cols,
            'pageNumber': str(page_number),
            'pageSize': str(page_size),
        }
        if filter_expr:
            params['filter'] = filter_expr
        if sort_columns:
            params['sortColumns'] = sort_columns
            params['sortTypes'] = str(sort_types if sort_types is not None else -1)

        data = self.get_json('/api/data/v1/get', params, hosts=[EM_DATA_HOST])
        if not data:
            return [], 0

        result = data.get('result') or {}
        return (result.get('data') or []), int(result.get('count') or 0)

    def datacenter_all(self, report_name, columns='ALL', filter_expr=None,
                       page_size=EM_MAX_DATACENTER_PAGE, max_pages=40,
                       sort_columns=None, sort_types=None):
        """翻完所有页，返回合并后的行列表。

        Args:
            max_pages: 安全上限，防止报表异常时无限翻页

        Returns:
            list[dict]
        """
        rows, count = self.datacenter_page(
            report_name, columns=columns, filter_expr=filter_expr,
            page_number=1, page_size=page_size,
            sort_columns=sort_columns, sort_types=sort_types)

        if not rows:
            return []

        all_rows = list(rows)
        pages = min((count + page_size - 1) // page_size, max_pages)

        for p in range(2, pages + 1):
            time.sleep(random.uniform(0.3, 0.6))  # 页间抖动，避免触发封禁
            more, _ = self.datacenter_page(
                report_name, columns=columns, filter_expr=filter_expr,
                page_number=p, page_size=page_size,
                sort_columns=sort_columns, sort_types=sort_types)
            if not more:
                break
            all_rows.extend(more)

        return all_rows

    # -- 公告原语 ----------------------------------------------------------

    def notice_page(self, stock_list, page_size=50, page_index=1, ann_type='A'):
        """东财个股公告（与 qstock_news_data.py 使用的是同一个接口）。

        Args:
            stock_list: 6 位股票代码
            page_size:  每页条数
            page_index: 页码
            ann_type:   公告类型，'A' 表示 A 股

        Returns:
            list[dict] — 失败或空时返回 []
        """
        data = self.get_json('/api/security/ann', {
            'sr': '-1',
            'page_size': str(page_size),
            'page_index': str(page_index),
            'ann_type': ann_type,
            'client_source': 'web',
            'f_node': '0',
            'stock_list': str(stock_list),
        }, hosts=[EM_NOTICE_HOST])

        if not data:
            return []
        return ((data.get('data') or {}).get('list') or [])


# 全局单例（与仓库其它模块的约定一致）
eastmoney_client = EastmoneyClient()
