#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低价擒牛选股模块

数据来源：utils/screener_data.py（新浪/同花顺资金流 + 腾讯行情 + 东财财务）
          —— 原 iwencai 接口已对程序化请求一律返回 403，无法在应用内修复。
          pywencai 仍保留为降级分支。

选股策略（与原问财查询语句逐条对应）：
    最新价 ≤ 10 元、净利润增长率 ≥ 100%、非ST、非科创板、非创业板、
    沪深A股、按成交额由小至大排名

公开契约（下游 low_price_bull_ui.py 依赖，勿改）：
    get_low_price_stocks(top_n) -> (success: bool, df: DataFrame, message: str)
    get_stock_codes() -> list[str]
"""

from typing import Tuple, Optional

import pandas as pd

from utils.screener_data import screen, pywencai_fallback


class LowPriceBullSelector:
    """低价擒牛选股类"""

    # 原问财查询语句（降级分支仍然用它）
    QUERY = (
        "股价<10元，"
        "净利润增长率(净利润同比增长率)≥100%，"
        "非st，"
        "非科创板，"
        "非创业板，"
        "沪深A股，"
        "成交额由小至大排名"
    )

    def __init__(self):
        self.raw_data = None
        self.selected_stocks = None

    def get_low_price_stocks(self, top_n: int = 5) -> Tuple[bool, Optional[pd.DataFrame], str]:
        """获取低价高成长股票。

        Args:
            top_n: 返回前N只股票

        Returns:
            (success, dataframe, message)

        注：原条件是「股价<10元」，本地过滤只支持 ≤，故 10 元整也会通过
        （差 1 分钱，可忽略）。
        """
        try:
            print(f"\n{'='*60}")
            print(f"🐂 低价擒牛选股 - 数据获取中")
            print(f"{'='*60}")
            print(f"策略: 股价<10元 + 净利润增长率≥100% + 沪深A股")
            print(f"目标: 筛选前{top_n}只股票")

            df = screen(
                period='今日',
                max_price=10,
                min_net_profit_growth=100,
                exclude_boards=('科创板', '创业板'),
                markets=['sh', 'sz'],
                sort_by='成交额', ascending=True,
                top_n=None,          # 先不过滤条数，便于打印命中总数
            )

            df = self._normalize(df)
            if df is None or df.empty:
                print("\n⚠️ 主数据源无结果，切换到 pywencai 降级分支...")
                df = self._normalize(pywencai_fallback(self.QUERY))
                if df is None or df.empty:
                    return False, None, "未获取到符合条件的股票数据（主源与降级源均无结果）"

            self.raw_data = df

            if len(df) > top_n:
                selected = df.head(top_n)
                print(f"\n从 {len(df)} 只股票中选出前 {top_n} 只")
            else:
                selected = df
                print(f"\n共 {len(df)} 只符合条件的股票")

            self.selected_stocks = selected

            print(f"\n✅ 选中的股票:")
            for i, (_, row) in enumerate(selected.iterrows(), 1):
                code = row.get('股票代码', 'N/A')
                name = row.get('股票简称', 'N/A')
                price = row.get('股价', row.get('最新价', 'N/A'))
                growth = row.get('净利润增长率', row.get('净利润同比增长率', 'N/A'))
                turnover = row.get('成交额', 'N/A')
                print(f"  {i}. {code} {name} - 股价:{price} 净利增长:{growth}% 成交额:{turnover}")

            print(f"{'='*60}\n")

            return True, selected, f"成功筛选出{len(selected)}只低价高成长股票"

        except Exception as e:
            error_msg = f"获取数据失败: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return False, None, error_msg

    @staticmethod
    def _normalize(df) -> Optional[pd.DataFrame]:
        """兜底规整：确保 股票代码/股票简称 存在（降级分支的列名不可控）"""
        if df is None:
            return None
        if not isinstance(df, pd.DataFrame):
            df = LowPriceBullSelector._convert_to_dataframe(df)
        if df is None or df.empty:
            return None
        return df

    @staticmethod
    def _convert_to_dataframe(result) -> Optional[pd.DataFrame]:
        """将 pywencai 返回结果转换为 DataFrame"""
        try:
            if isinstance(result, pd.DataFrame):
                return result
            elif isinstance(result, dict):
                if 'data' in result:
                    return pd.DataFrame(result['data'])
                elif 'result' in result:
                    return pd.DataFrame(result['result'])
                else:
                    return pd.DataFrame(result)
            elif isinstance(result, list):
                return pd.DataFrame(result)
            else:
                print(f"⚠️ 未知的数据格式: {type(result)}")
                return None
        except Exception as e:
            print(f"转换DataFrame失败: {e}")
            return None

    def get_stock_codes(self) -> list:
        """获取选中股票的代码列表（去掉市场后缀）

        Returns:
            股票代码列表
        """
        if self.selected_stocks is None or self.selected_stocks.empty:
            return []

        if '股票代码' not in self.selected_stocks.columns:
            return []

        codes = []
        for code in self.selected_stocks['股票代码'].tolist():
            if isinstance(code, str):
                # 去掉 .SZ 等后缀
                clean_code = code.split('.')[0] if '.' in code else code
                codes.append(clean_code)
            else:
                codes.append(str(code))

        return codes


# 全局实例
low_price_bull_selector = LowPriceBullSelector()
