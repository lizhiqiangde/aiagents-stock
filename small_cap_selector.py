#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小市值策略选股模块

数据来源：utils/screener_data.py（新浪/同花顺资金流 + 腾讯行情 + 东财财务）
          —— 原 iwencai 接口已对程序化请求一律返回 403，无法在应用内修复。
          pywencai 仍保留为降级分支。

选股策略（与原问财查询语句逐条对应）：
    总市值 ≤ 50亿、营收增长率 ≥ 10%、净利润增长率 ≥ 100%、
    沪深A股、非ST、非创业板、非科创板、按总市值由小到大排名

公开契约（下游 small_cap_ui.py 依赖，勿改）：
    get_small_cap_stocks(top_n) -> (success: bool, df: DataFrame, message: str)
    format_stock_info(df) -> str
"""

import logging
from typing import Tuple, Optional

import pandas as pd

from utils.screener_data import screen, pywencai_fallback


class SmallCapSelector:
    """小市值策略选股器"""

    QUERY = (
        "总市值≤50亿，"
        "营收增长率≥10%，"
        "净利润增长率(净利润同比增长率)≥100%，"
        "沪深A股，"
        "非ST，"
        "非创业板，"
        "非科创板，"
        "总市值由小至大排名"
    )

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.selected_stocks = None

    def get_small_cap_stocks(self, top_n: int = 5) -> Tuple[bool, Optional[pd.DataFrame], str]:
        """获取符合小市值策略的股票。

        Args:
            top_n: 返回前N只股票

        Returns:
            (是否成功, 数据DataFrame, 消息)
        """
        try:
            self.logger.info("开始执行小市值策略选股")

            df = screen(
                period='今日',
                max_market_cap=50,
                min_revenue_growth=10,
                min_net_profit_growth=100,
                exclude_boards=('科创板', '创业板'),
                markets=['sh', 'sz'],
                sort_by='总市值', ascending=True,
                top_n=None,
            )

            if df is None or df.empty:
                self.logger.warning("主数据源无结果，尝试 pywencai 降级分支")
                df = pywencai_fallback(self.QUERY)

            # ⚠️ 原实现直接调 result.empty，pywencai 返回 dict 时会 AttributeError。
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                self.logger.warning("未获取到符合条件的股票")
                return False, None, "未找到符合条件的股票"

            self.logger.info(f"获取到 {len(df)} 只股票")

            if len(df) > top_n:
                df = df.head(top_n)
                self.logger.info(f"筛选前 {top_n} 只股票")

            self.selected_stocks = df
            return True, df, f"成功获取 {len(df)} 只股票"

        except Exception as e:
            error_msg = f"选股失败: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            traceback.print_exc()
            return False, None, error_msg

    def format_stock_info(self, df: pd.DataFrame) -> str:
        """格式化股票信息为文本

        Args:
            df: 股票数据DataFrame

        Returns:
            格式化后的文本
        """
        if df is None or df.empty:
            return "无数据"

        lines = []
        for idx, row in df.iterrows():
            stock_code = row.get('股票代码', 'N/A')
            stock_name = row.get('股票简称', 'N/A')
            market_cap = row.get('总市值', row.get('总市值[20241211]', 'N/A'))
            revenue_growth = row.get('营收增长率', row.get('营业收入增长率', 'N/A'))
            profit_growth = row.get('净利润增长率', row.get('净利润同比增长率', 'N/A'))

            line = f"{idx+1}. {stock_code} {stock_name}"

            # 添加详细信息
            details = []
            if market_cap != 'N/A':
                try:
                    cap_val = float(market_cap)
                    if cap_val >= 100000000:
                        details.append(f"市值:{cap_val/100000000:.2f}亿")
                    else:
                        details.append(f"市值:{cap_val/10000:.2f}万")
                except:
                    pass

            if revenue_growth != 'N/A':
                try:
                    details.append(f"营收增长:{float(revenue_growth):.2f}%")
                except:
                    pass

            if profit_growth != 'N/A':
                try:
                    details.append(f"净利增长:{float(profit_growth):.2f}%")
                except:
                    pass

            if details:
                line += f" - {', '.join(details)}"

            lines.append(line)

        return '\n'.join(lines)


# 全局实例
small_cap_selector = SmallCapSelector()
