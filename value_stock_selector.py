#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低估值选股模块

数据来源：utils/screener_data.py（新浪/同花顺资金流 + 腾讯行情 + 东财财务）
          —— 原 iwencai 接口已对程序化请求一律返回 403，无法在应用内修复。
          pywencai 仍保留为降级分支。

选股策略（与原问财查询语句逐条对应）：
    市盈率 ≤ 20、市净率 ≤ 1.5、股息率 ≥ 1%、资产负债率 ≤ 30%、
    非ST、非科创板、非创业板、按流通市值由小到大排名

公开契约（下游 value_stock_ui.py 依赖，勿改）：
    get_value_stocks(top_n) -> (success: bool, df: DataFrame, message: str)
    get_stock_codes() -> list[str]
"""

from typing import Tuple, Optional

import pandas as pd

from utils.screener_data import screen, pywencai_fallback


class ValueStockSelector:
    """低估值选股类"""

    QUERY = (
        "市盈率小于等于20，"
        "市净率小于等于1.5，"
        "股息率大于等于1%，"
        "资产负债率小于等于30%，"
        "非st，"
        "非科创板，"
        "非创业板，"
        "按流通市值由小到大排名"
    )

    def __init__(self):
        self.raw_data = None
        self.selected_stocks = None

    def get_value_stocks(self, top_n: int = 10) -> Tuple[bool, Optional[pd.DataFrame], str]:
        """获取低估值优质股票。

        Args:
            top_n: 返回前N只股票

        Returns:
            (success, dataframe, message)

        注：股息率在财报里只有约 15% 的股票有值（不分红即无该项），
        且本模块对缺失值一律判为「不满足」，故结果集会比原问财明显更少。
        """
        try:
            print(f"\n{'='*60}")
            print(f"💎 低估值选股 - 数据获取中")
            print(f"{'='*60}")
            print(f"策略: PE≤20 + PB≤1.5 + 股息率≥1% + 资产负债率≤30%")
            print(f"排除: ST、科创板、创业板")
            print(f"排序: 按流通市值由小到大")
            print(f"目标: 筛选前{top_n}只股票")

            df = screen(
                period='今日',
                max_pe=20,
                max_pb=1.5,
                min_dividend_yield=1,
                max_debt_ratio=30,
                exclude_boards=('科创板', '创业板'),
                markets=['sh', 'sz'],
                sort_by='流通市值', ascending=True,
                top_n=None,
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
                pe = row.get('市盈率', row.get('市盈率(动态)', 'N/A'))
                pb = row.get('市净率', 'N/A')
                div_rate = row.get('股息率', 'N/A')
                debt_ratio = row.get('资产负债率', 'N/A')
                cap = row.get('流通市值', 'N/A')
                print(f"  {i}. {code} {name} - PE:{pe} PB:{pb} "
                      f"股息率:{div_rate}% 负债率:{debt_ratio}% 流通市值:{cap}")

            print(f"{'='*60}\n")

            return True, selected, f"成功筛选出{len(selected)}只低估值优质股票"

        except Exception as e:
            error_msg = f"获取数据失败: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            return False, None, error_msg

    @staticmethod
    def _normalize(df) -> Optional[pd.DataFrame]:
        """兜底规整：降级分支的返回格式/列名不可控"""
        if df is None:
            return None
        if not isinstance(df, pd.DataFrame):
            df = ValueStockSelector._convert_to_dataframe(df)
        if df is None or df.empty:
            return None
        return df

    @staticmethod
    def _convert_to_dataframe(result) -> Optional[pd.DataFrame]:
        """将 pywencai 返回结果转换为DataFrame"""
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
                clean_code = code.split('.')[0] if '.' in code else code
                codes.append(clean_code)
            else:
                codes.append(str(code))

        return codes


# 全局实例
value_stock_selector = ValueStockSelector()

# 测试
if __name__ == "__main__":
    print("=" * 60)
    print("测试低估值选股模块")
    print("=" * 60)

    selector = ValueStockSelector()
    success, df, msg = selector.get_value_stocks(top_n=10)
    print(f"\n结果: {msg}")
    if success and df is not None:
        print(f"共 {len(df)} 只股票")
