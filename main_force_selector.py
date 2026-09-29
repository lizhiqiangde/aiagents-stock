#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主力选股模块

数据来源：utils/screener_data.py（新浪资金流 + 腾讯行情 + 东财财务）
          —— 原 iwencai 接口已对程序化请求一律返回 403，无法在应用内修复。
          pywencai 仍保留为降级分支，见 _pywencai_fallback()。

公开契约（下游 main_force_analysis.py 依赖，勿改）：
    get_main_force_stocks(...) -> (success: bool, df: DataFrame, message: str)
"""

import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import pandas as pd

from utils.screener_data import (
    build_universe, board_of, is_st, CANONICAL_COLUMNS,
)

# 主力资金列名（下游按子串匹配，顺序即优先级）
MAIN_FUND_COL = '区间主力资金流向'


class MainForceStockSelector:
    """主力选股类"""

    def __init__(self):
        self.raw_data = None
        self.filtered_stocks = None

    # -- 周期映射 ---------------------------------------------------------

    @staticmethod
    def _resolve_days(start_date: str = None, days_ago: int = None) -> int:
        """把 start_date / days_ago 统一成"距今天数"。

        东财/新浪/同花顺都只支持固定周期（今日/3日/5日/10日/20日），
        不支持任意起始日期，故最终会映射到最接近的固定周期。
        """
        if days_ago is not None:
            return int(days_ago)
        if start_date:
            m = re.match(r'\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})', str(start_date))
            if m:
                try:
                    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    return max(1, (datetime.now() - d).days)
                except ValueError:
                    pass
        return 1

    @staticmethod
    def _period_of(days: int) -> str:
        """天数 -> 固定周期（就近取，不支持任意区间）"""
        if days <= 1:
            return '今日'
        if days <= 3:
            return '3日'
        if days <= 5:
            return '5日'
        if days <= 10:
            return '10日'
        return '20日'

    # -- 主流程 -----------------------------------------------------------

    def get_main_force_stocks(self, start_date: str = None, days_ago: int = None,
                              min_market_cap: float = None,
                              max_market_cap: float = None,
                              top_n: int = 100) -> Tuple[bool, pd.DataFrame, str]:
        """获取主力资金净流入排名靠前的股票。

        Args:
            start_date: 开始日期（"2025年10月1日" 等），会折算成 days_ago
            days_ago:   距今多少天
            min_market_cap / max_market_cap: 市值范围（单位：亿）
            top_n:      返回条数上限

        Returns:
            (success, dataframe, message)
        """
        days = self._resolve_days(start_date, days_ago)
        period = self._period_of(days)

        print(f"\n{'=' * 60}")
        print(f"🔍 主力选股 - 数据获取中")
        print(f"{'=' * 60}")
        print(f"距今 {days} 天 -> 采用周期: {period}")

        try:
            df = build_universe(period=period)
        except Exception as e:
            print(f"  ❌ 建池异常: {type(e).__name__}: {e}")
            df = pd.DataFrame(columns=CANONICAL_COLUMNS)

        if df is None or df.empty:
            print("\n⚠️ 主数据源不可用，切换到 pywencai 降级分支...")
            ok, fallback_df, msg = self._pywencai_fallback(
                start_date, days, min_market_cap, max_market_cap)
            if ok:
                self.raw_data = fallback_df
            return ok, fallback_df, msg

        original_count = len(df)

        # 非ST、非科创板（与原 iwencai 查询条件一致）
        df = df[~df['股票简称'].apply(is_st)]
        df = df[df['股票代码'].apply(lambda c: board_of(c) != '科创板')]

        # 市值筛选（单位：亿）
        if min_market_cap is not None or max_market_cap is not None:
            cap = pd.to_numeric(df['总市值'], errors='coerce') / 1e8
            if min_market_cap is not None:
                df = df[cap >= float(min_market_cap)]
                cap = cap[df.index]
            if max_market_cap is not None:
                df = df[cap <= float(max_market_cap)]

        # 按主力资金净流入排序取前 N
        df[MAIN_FUND_COL] = pd.to_numeric(df[MAIN_FUND_COL], errors='coerce')
        df = df[df[MAIN_FUND_COL].notna()]
        df = df.nlargest(top_n, MAIN_FUND_COL) if top_n else df

        if df.empty:
            msg = "筛选后没有符合条件的股票，请放宽市值范围"
            print(f"\n❌ {msg}")
            return False, None, msg

        self.raw_data = df

        print(f"\n✅ 数据获取成功: {original_count} 只 -> 筛出 {len(df)} 只")
        print(f"   数据字段: {list(df.columns[:12])}...")

        return True, df, f"成功获取{len(df)}只股票数据（周期：{period}）"

    # -- 降级分支 ---------------------------------------------------------

    @staticmethod
    def _pywencai_fallback(start_date, days, min_market_cap, max_market_cap):
        """原 iwencai 链路。上游 403 时基本不会成功，保留是为了它恢复时能自动接回。"""
        try:
            from utils.pywencai_helper import safe_get
        except Exception as e:
            return False, None, f"pywencai 不可用: {e}"

        if not start_date:
            d = datetime.now() - timedelta(days=days)
            start_date = f"{d.year}年{d.month}月{d.day}日"

        queries = [
            f"{start_date}以来主力资金净流入排名，并计算区间涨跌幅，"
            f"市值{min_market_cap}-{max_market_cap}亿之间，非科创非st，"
            f"所属同花顺行业，总市值，净利润，营收，市盈率，市净率",
            f"{start_date}以来主力资金净流入前100名，并计算区间涨跌幅，"
            f"市值{min_market_cap}-{max_market_cap}亿，非st非科创板，所属行业，总市值",
        ]

        for i, query in enumerate(queries, 1):
            try:
                print(f"  尝试降级方案 {i}/{len(queries)}...")
                result = safe_get(query=query, loop=True)
                if result is None:
                    continue
                df = MainForceStockSelector._to_dataframe(result)
                if df is None or df.empty:
                    continue
                print(f"  ✅ 降级方案{i}成功: {len(df)} 只")
                return True, df, f"成功获取{len(df)}只股票数据（pywencai 降级）"
            except Exception as e:
                print(f"  ❌ 降级方案{i}失败: {type(e).__name__}: {e}")
                time.sleep(2)

        return False, None, "所有数据源均不可用（主源 + pywencai 降级均失败）"

    @staticmethod
    def _to_dataframe(result) -> pd.DataFrame:
        """转换 pywencai 返回结果为 DataFrame"""
        try:
            if isinstance(result, pd.DataFrame):
                return result
            if isinstance(result, dict):
                if 'tableV1' in result:
                    t = result['tableV1']
                    return t if isinstance(t, pd.DataFrame) else pd.DataFrame(t)
                return pd.DataFrame([result])
            if isinstance(result, list):
                return pd.DataFrame(result)
        except Exception as e:
            print(f"  转换DataFrame失败: {e}")
        return None

    # 旧名保留（曾有外部调用，删掉会破坏兼容）
    _convert_to_dataframe = _to_dataframe

    # -- 后置筛选 ---------------------------------------------------------

    def filter_stocks(self, df: pd.DataFrame,
                      max_range_change: float = None,
                      min_market_cap: float = None,
                      max_market_cap: float = None) -> pd.DataFrame:
        """按区间涨跌幅与市值做后置筛选。"""
        if df is None or df.empty:
            return df

        print(f"\n{'=' * 60}")
        print(f"🔍 智能筛选中...")
        print(f"{'=' * 60}")
        print(f"  - 区间涨跌幅 < {max_range_change}%")
        print(f"  - 市值范围: {min_market_cap}-{max_market_cap}亿")

        original_count = len(df)
        filtered_df = df.copy()

        # 1. 区间涨跌幅
        pct_col = None
        for name in ['区间涨跌幅:前复权', '区间涨跌幅:前复权(%)', '区间涨跌幅(%)',
                     '区间涨跌幅', '涨跌幅:前复权', '涨跌幅:前复权(%)',
                     '涨跌幅(%)', '涨跌幅']:
            hit = [c for c in df.columns if name in c]
            if hit:
                pct_col = hit[0]
                break

        if pct_col and max_range_change is not None:
            print(f"\n使用字段: {pct_col}")
            filtered_df[pct_col] = pd.to_numeric(filtered_df[pct_col], errors='coerce')
            before = len(filtered_df)
            filtered_df = filtered_df[
                filtered_df[pct_col].notna() & (filtered_df[pct_col] < max_range_change)]
            print(f"  区间涨跌幅筛选: {before} -> {len(filtered_df)} 只")

        # 2. 市值（统一换算成亿）
        cap_cols = [c for c in df.columns if '总市值' in c or '市值' in c]
        if cap_cols and (min_market_cap is not None or max_market_cap is not None):
            col_name = cap_cols[0]
            print(f"\n使用字段: {col_name}")
            cap = pd.to_numeric(filtered_df[col_name], errors='coerce')
            # 原数据是元，10万以上判定为元并折算成亿
            if cap.max() is not None and cap.max() > 100000:
                print(f"  检测到单位为元，转换为亿")
                cap = cap / 1e8

            before = len(filtered_df)
            mask = cap.notna()
            if min_market_cap is not None:
                mask &= (cap >= float(min_market_cap))
            if max_market_cap is not None:
                mask &= (cap <= float(max_market_cap))
            filtered_df = filtered_df[mask]
            print(f"  市值筛选: {before} -> {len(filtered_df)} 只")

        # 3. ST 兜底
        if '股票简称' in filtered_df.columns:
            before = len(filtered_df)
            filtered_df = filtered_df[~filtered_df['股票简称'].apply(is_st)]
            if before != len(filtered_df):
                print(f"  ST股票过滤: {before} -> {len(filtered_df)} 只")

        print(f"\n筛选完成: {original_count} -> {len(filtered_df)} 只股票")
        self.filtered_stocks = filtered_df
        return filtered_df

    def get_top_stocks(self, df: pd.DataFrame, top_n: int = None) -> pd.DataFrame:
        """按主力资金净流入取前 N 名"""
        if df is None or df.empty:
            return df

        main_fund_col = None
        for pattern in ['区间主力资金流向', '区间主力资金净流入',
                        '主力资金流向', '主力资金净流入', '主力净流入']:
            hit = [c for c in df.columns if pattern in c]
            if hit:
                main_fund_col = hit[0]
                break

        if main_fund_col:
            print(f"\n使用字段排序: {main_fund_col}")
            df = df.copy()
            df[main_fund_col] = pd.to_numeric(df[main_fund_col], errors='coerce')
            top_df = df.nlargest(top_n, main_fund_col) if top_n else df
            print(f"获取主力资金净流入前 {len(top_df)} 名")
            return top_df

        print(f"未找到主力资金列，返回前{top_n}条数据")
        return df.head(top_n) if top_n else df

    # -- 供 AI 分析 -------------------------------------------------------

    def format_stock_list_for_analysis(self, df: pd.DataFrame) -> List[Dict]:
        """格式化股票列表供 AI 分析师使用。

        原先这里提取的是 iwencai 的 8 个"评分/能力"字段。那些是付费字段，
        没有免费替代，故改为**真实财务指标**（ROE/毛利率/负债率/增长率等）。
        语义不同（不是评分），但信息量不降反升。
        """
        if df is None or df.empty:
            return []

        def first_col(*names):
            for n in names:
                hit = [c for c in df.columns if n in c]
                if hit:
                    return hit[0]
            return None

        pct_col = first_col('区间涨跌幅', '涨跌幅')
        fund_col = first_col('区间主力资金流向', '主力资金净流入', '主力资金流向')
        industry_col = first_col('所属同花顺行业', '所属行业', '行业')

        financial_keys = ['加权ROE', '销售毛利率', '净利润增长率', '营收增长率',
                          '资产负债率', '股息率']

        stock_list = []
        for _, row in df.iterrows():
            stock_data = {
                'symbol': row.get('股票代码', 'N/A'),
                'name': row.get('股票简称', 'N/A'),
                'industry': row.get(industry_col, 'N/A') if industry_col else 'N/A',
                'market_cap': row.get('总市值', 'N/A'),
                'range_change': row.get(pct_col) if pct_col else None,
                'main_fund_inflow': row.get(fund_col) if fund_col else None,
                'pe_ratio': row.get('市盈率', 'N/A'),
                'pb_ratio': row.get('市净率', 'N/A'),
                'revenue': row.get('营业收入', 'N/A'),
                'net_profit': row.get('净利润', 'N/A'),
                'financials': {k: row.get(k) for k in financial_keys if k in df.columns},
                'raw_data': row.to_dict(),
            }
            stock_list.append(stock_data)

        return stock_list

    def print_stock_summary(self, stock_list: List[Dict]):
        """打印股票摘要"""
        print(f"\n{'=' * 80}")
        print(f"📊 候选股票列表 ({len(stock_list)}只)")
        print(f"{'=' * 80}")
        print(f"{'序号':<4} {'代码':<8} {'名称':<12} {'行业':<15} {'主力资金':<12} {'涨跌幅':<8}")
        print(f"{'-' * 80}")

        for i, stock in enumerate(stock_list, 1):
            symbol = stock.get('symbol', 'N/A')
            name = stock.get('name') or 'N/A'
            industry = stock.get('industry') or 'N/A'
            name = name[:10] if isinstance(name, str) else 'N/A'
            industry = industry[:13] if isinstance(industry, str) else 'N/A'

            main_fund = stock.get('main_fund_inflow')
            if isinstance(main_fund, (int, float)):
                main_fund_str = (f"{main_fund / 1e8:.2f}亿" if abs(main_fund) >= 1e8
                                 else f"{main_fund / 1e4:.2f}万")
            else:
                main_fund_str = 'N/A'

            change = stock.get('range_change')
            change_str = f"{change:.2f}%" if isinstance(change, (int, float)) else 'N/A'

            print(f"{i:<4} {symbol:<8} {name:<12} {industry:<15} "
                  f"{main_fund_str:<12} {change_str:<8}")

        print(f"{'=' * 80}\n")


# 全局实例
main_force_selector = MainForceStockSelector()
