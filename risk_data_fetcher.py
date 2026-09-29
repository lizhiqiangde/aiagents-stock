"""
风险数据获取模块

数据来源：东方财富结构化报表 + 公告接口（见 utils/screener_data.py）

    限售解禁    东财 RPT_LIFT_STAGE          结构化（解禁日/股数/市值/占比/类型）
    股东增减持  东财 RPT_SHARE_HOLDER_INCREASE  结构化（股东/方向/数量/比例）
    近期重要事件 东财 np-anotice-stock        公告标题 + 东财自带的公告分类

原实现走 pywencai（问财），但该接口已对程序化请求一律返回 403，
无法在应用内修复，故整体改为东财。pywencai 保留为降级分支。

⚠️ 东财 datacenter 对**日期区间**条件会静默返回 0 条（不报错），
   故解禁/增减持按代码取全量后本地筛日期，见 screener_data.em_lift_stage。
"""

import os
import time
import warnings
from typing import Any, Dict

import pandas as pd

from utils.screener_data import (
    em_lift_stage, em_holder_increase, em_announcements, pywencai_fallback,
)

# 屏蔽 Node.js / pywencai 的警告信息（不影响功能）
warnings.filterwarnings('ignore', category=DeprecationWarning)
os.environ.setdefault('PYTHONWARNINGS', 'ignore::DeprecationWarning')
os.environ.setdefault('NODE_NO_WARNINGS', '1')

# 解禁 / 增减持的回看（前瞻）窗口
LIFT_DAYS_FORWARD = 180    # 未来半年的解禁压力
HOLDER_DAYS_BACK = 180     # 近半年的增减持
EVENT_LIMIT = 20           # 近期公告条数

# 公告分类/标题里的风险与利好关键字（东财公告接口自带 column_name，比纯关键字准）
# ⚠️ 只放真正带风险含义的词。'变更'/'监管'/'延期' 之类看似负面、实则常出现在
#    中性公告里（实测「关于变更注册资本获核准的公告」被误判为风险），已剔除。
RISK_KEYWORDS = (
    '减持', '解禁', '质押', '冻结', '诉讼', '仲裁', '涉诉', '处罚', '立案',
    '问询', '关注函', '退市', '风险警示', '亏损', '预亏', '业绩下降',
    '担保', '违规', '终止上市', '整改', '商誉', '辞职',
)
POSITIVE_KEYWORDS = ('增持', '回购', '分红', '派息', '预增', '中标', '专利', '订单', '获核准')

# 降级策略：**只在主数据源报错时**才走 pywencai，而不是主源"没查到记录"时。
# 否则每次查询一只没有解禁/减持的股票都要启动一次 Playwright 浏览器（实测数秒），
# 而"没有记录"本身是有效答案。iwencai 已确认对程序化请求一律 403，
# 保留该分支只为上游恢复时能自动接回。
FALLBACK_ON_EMPTY = False


class RiskDataFetcher:
    """风险数据获取类"""

    def __init__(self):
        """初始化"""
        pass

    def get_risk_data(self, symbol: str) -> Dict[str, Any]:
        """
        获取股票风险相关数据

        Args:
            symbol: 股票代码（如：600000）

        Returns:
            包含风险数据的字典
        """
        print(f"\n正在获取 {symbol} 的风险数据...")

        risk_data = {
            'symbol': symbol,
            'data_success': False,
            'lifting_ban': None,  # 限售解禁数据
            'shareholder_reduction': None,  # 大股东减持数据
            'important_events': None,  # 重要事件数据
            'error': None
        }

        try:
            # 1. 获取限售解禁数据
            print("   查询限售解禁数据...")
            lifting_ban = self._get_lifting_ban_data(symbol)
            risk_data['lifting_ban'] = lifting_ban
            if lifting_ban and lifting_ban.get('has_data'):
                print(f"   获取到限售解禁数据")
            else:
                print(f"   暂无限售解禁数据")

            time.sleep(0.3)

            # 2. 获取大股东减持公告
            print("   查询大股东减持公告...")
            reduction = self._get_shareholder_reduction_data(symbol)
            risk_data['shareholder_reduction'] = reduction
            if reduction and reduction.get('has_data'):
                print(f"   获取到大股东减持数据")
            else:
                print(f"   暂无大股东减持数据")

            time.sleep(0.3)

            # 3. 获取近期重要事件
            print("   查询近期重要事件...")
            events = self._get_important_events_data(symbol)
            risk_data['important_events'] = events
            if events and events.get('has_data'):
                print(f"   获取到重要事件数据")
            else:
                print(f"   暂无重要事件数据")

            # 如果至少有一个数据源成功，则认为获取成功
            if (lifting_ban and lifting_ban.get('has_data')) or \
               (reduction and reduction.get('has_data')) or \
               (events and events.get('has_data')):
                risk_data['data_success'] = True
                print(f"风险数据获取完成")
            else:
                print(f"未获取到风险相关数据")

        except Exception as e:
            print(f"风险数据获取失败: {str(e)}")
            risk_data['error'] = str(e)

        return risk_data

    # -- 1. 限售解禁 -------------------------------------------------------

    def _get_lifting_ban_data(self, symbol: str) -> Dict[str, Any]:
        """获取限售解禁数据（东财 RPT_LIFT_STAGE，未来 LIFT_DAYS_FORWARD 天）"""
        result = {
            'has_data': False,
            'query': f"东方财富 · 限售解禁（未来{LIFT_DAYS_FORWARD}天）",
            'data': None,
            'summary': None
        }

        try:
            df = self._normalize_lift(em_lift_stage([symbol], days_forward=LIFT_DAYS_FORWARD))

            # 主源正常但"没有解禁"是有效答案，不再走降级（见 FALLBACK_ON_EMPTY 注释）
            if (df is None or df.empty) and FALLBACK_ON_EMPTY:
                df = self._normalize_lift(self._fallback_lift(symbol))
            if df is None or df.empty:
                return result

            result['has_data'] = True
            result['data'] = df

            # 按解禁日升序，最近的在前
            summary = [f"未来{LIFT_DAYS_FORWARD}天内共 {len(df)} 次解禁"]
            big = df[pd.to_numeric(df['解禁比例(%)'], errors='coerce') >= 1]
            if len(big):
                summary.append(f"其中解禁比例≥1%的有 {len(big)} 次（冲击较大，重点关注前几条）")

            for _, row in df.head(5).iterrows():
                parts = []
                if pd.notna(row.get('解禁日期')):
                    parts.append(f"日期: {row['解禁日期']}")
                if pd.notna(row.get('解禁比例(%)')):
                    parts.append(f"解禁比例: {row['解禁比例(%)']:.2f}%")
                if pd.notna(row.get('解禁类型')):
                    parts.append(f"类型: {row['解禁类型']}")
                if parts:
                    summary.append(" | ".join(parts))

            result['summary'] = "\n".join(summary)

        except Exception as e:
            result['error'] = str(e)
            # 主源报错才算"不可用"，此时才值得尝试降级
            try:
                df = self._normalize_lift(self._fallback_lift(symbol))
                if df is not None and not df.empty:
                    result.update({'has_data': True, 'data': df,
                                   'summary': f"获取到 {len(df)} 条记录（问财降级源）"})
            except Exception:
                pass

        return result

    @staticmethod
    def _normalize_lift(df) -> pd.DataFrame:
        """东财字段 -> 中文列名

        ⚠️ 刻意**不输出** FREE_SHARES / LIFT_MARKET_CAP：
           实测这两个字段不满足「解禁市值 = 解禁股数 × 现价」的关系
           （LIFT_MARKET_CAP/FREE_SHARES/现价 的取值分布≈ FREE_RATIO/100，
            即其中的股数口径并非本次解禁批次），因此无法给出可靠的单位标注。
           与其让 AI 拿到标错单位的数字，不如只给三个已验证可靠的字段：
           日期、比例(%)、类型。
        """
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        rename = {
            'FREE_DATE': '解禁日期', 'FREE_RATIO': '解禁比例(%)',
            'FREE_SHARES_TYPE': '解禁类型', 'SECURITY_NAME_ABBR': '股票简称',
        }
        out = df.rename(columns=rename)
        if '解禁日期' in out.columns:
            out['解禁日期'] = pd.to_datetime(out['解禁日期'], errors='coerce').dt.strftime('%Y-%m-%d')
            out = out.sort_values('解禁日期', na_position='last')
        keep = [c for c in ['股票代码', '股票简称', '解禁日期', '解禁比例(%)',
                            '解禁类型'] if c in out.columns]
        return out[keep].reset_index(drop=True)

    @staticmethod
    def _fallback_lift(symbol: str) -> pd.DataFrame:
        """降级：问财。上游 403 时基本不会成功，保留备接。"""
        df = pywencai_fallback(f"{symbol}限售解禁")
        if df is None or df.empty:
            return None
        return df

    # -- 2. 股东增减持 -----------------------------------------------------

    def _get_shareholder_reduction_data(self, symbol: str) -> Dict[str, Any]:
        """获取大股东减持数据（东财 RPT_SHARE_HOLDER_INCREASE，近 HOLDER_DAYS_BACK 天）"""
        result = {
            'has_data': False,
            'query': f"东方财富 · 股东增减持（近{HOLDER_DAYS_BACK}天）",
            'data': None,
            'summary': None
        }

        try:
            df = self._normalize_holder(em_holder_increase([symbol], days_back=HOLDER_DAYS_BACK))

            if (df is None or df.empty) and FALLBACK_ON_EMPTY:
                df = self._normalize_holder(self._fallback_holder(symbol))
            if df is None or df.empty:
                return result

            result['has_data'] = True
            result['data'] = df

            summary = [f"近{HOLDER_DAYS_BACK}天共 {len(df)} 条股东增减持记录"]

            reductions = df[df['方向'].astype(str).str.contains('减持', na=False)]
            increases = df[df['方向'].astype(str).str.contains('增持', na=False)]
            if len(reductions):
                summary.append(f"其中减持 {len(reductions)} 条"
                               f"（涉及 {reductions['股东名称'].nunique()} 名股东）")
            if len(increases):
                summary.append(f"其中增持 {len(increases)} 条（属利好信号，可对冲部分减持压力）")

            for _, row in reductions.head(5).iterrows():
                parts = []
                if pd.notna(row.get('公告日期')):
                    parts.append(f"日期: {row['公告日期']}")
                if pd.notna(row.get('股东名称')):
                    parts.append(f"股东: {str(row['股东名称'])[:30]}")
                if pd.notna(row.get('变动比例(%)')):
                    parts.append(f"变动比例: {row['变动比例(%)']:.2f}%")
                if pd.notna(row.get('变动数量')):
                    parts.append(f"变动数量(东财原值): {row['变动数量']:.2f}")
                if parts:
                    summary.append(" | ".join(parts))

            result['summary'] = "\n".join(summary)

        except Exception as e:
            result['error'] = str(e)
            try:
                df = self._normalize_holder(self._fallback_holder(symbol))
                if df is not None and not df.empty:
                    result.update({'has_data': True, 'data': df,
                                   'summary': f"获取到 {len(df)} 条记录（问财降级源）"})
            except Exception:
                pass

        return result

    @staticmethod
    def _normalize_holder(df) -> pd.DataFrame:
        """东财字段 -> 中文列名

        ⚠️ 两点实测修正：
        1. `CHANGE_RATE` 的**符号不可信** —— 实测「增持」行出现负值、三行里两正一负，
           故统一取绝对值，方向一律以 `DIRECTION` 字段为准。
        2. `CHANGE_NUM` 的单位无法从接口确认（对不上任何「股数×价格」关系），
           故列名只叫「变动数量」不做单位标注。
        """
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        rename = {
            'NOTICE_DATE': '公告日期', 'HOLDER_NAME': '股东名称',
            'DIRECTION': '方向', 'CHANGE_NUM': '变动数量', 'CHANGE_RATE': '变动比例(%)',
            'SECURITY_NAME_ABBR': '股票简称',
        }
        out = df.rename(columns=rename)
        if '公告日期' in out.columns:
            out['公告日期'] = pd.to_datetime(out['公告日期'], errors='coerce').dt.strftime('%Y-%m-%d')
            out = out.sort_values('公告日期', ascending=False, na_position='last')
        if '变动比例(%)' in out.columns:
            out['变动比例(%)'] = pd.to_numeric(out['变动比例(%)'], errors='coerce').abs()
        keep = [c for c in ['股票代码', '股票简称', '公告日期', '股东名称',
                            '方向', '变动数量', '变动比例(%)'] if c in out.columns]
        return out[keep].reset_index(drop=True)

    @staticmethod
    def _fallback_holder(symbol: str) -> pd.DataFrame:
        """降级：问财"""
        return pywencai_fallback(f"{symbol}大股东减持公告")

    # -- 3. 近期重要事件 ---------------------------------------------------

    def _get_important_events_data(self, symbol: str) -> Dict[str, Any]:
        """获取近期重要事件（东财个股公告，近 EVENT_LIMIT 条）

        东财公告接口自带 `columns[0].column_name` 分类（半年度报告 / 分配方案实施 /
        高管人员任职变动 / 调研活动 …），比纯关键字匹配准得多，故直接采用，
        再按风险关键字叠加一列标记供 AI 快速定位。
        """
        result = {
            'has_data': False,
            'query': f"东方财富 · 个股公告（最近{EVENT_LIMIT}条）",
            'data': None,
            'summary': None
        }

        try:
            items = em_announcements(symbol, limit=EVENT_LIMIT)
            if not items:
                if FALLBACK_ON_EMPTY:
                    df = pywencai_fallback(f"{symbol}近期重要事件")
                    if df is not None and not df.empty:
                        result.update({'has_data': True, 'data': df,
                                       'summary': f"获取到 {len(df)} 条记录（问财降级源）"})
                return result

            df = pd.DataFrame([{
                '公告日期': it.get('date', ''),
                '公告类型': it.get('column', ''),
                '公告标题': it.get('title', ''),
                '风险标记': self._classify_event(it.get('title', '') + it.get('column', '')),
            } for it in items])

            result['has_data'] = True
            result['data'] = df

            risk_rows = df[df['风险标记'] == '⚠️ 风险']
            good_rows = df[df['风险标记'] == '✅ 利好']

            summary = [f"最近 {len(df)} 条公告，其中风险类 {len(risk_rows)} 条、利好类 {len(good_rows)} 条"]
            for _, row in risk_rows.head(8).iterrows():
                summary.append(f"{row['公告日期']} [{row['公告类型']}] {row['公告标题']}")
            if risk_rows.empty:
                summary.append("未发现明显的减持/解禁/质押/诉讼/处罚类公告")

            result['summary'] = "\n".join(summary)

        except Exception as e:
            result['error'] = str(e)

        return result

    @staticmethod
    def _classify_event(text: str) -> str:
        """按关键字给公告打风险/利好标记"""
        if not text:
            return ''
        t = str(text)
        if any(k in t for k in RISK_KEYWORDS):
            return '⚠️ 风险'
        if any(k in t for k in POSITIVE_KEYWORDS):
            return '✅ 利好'
        return ''

    # -- 兼容：原 pywencai 的返回解析 --------------------------------------

    def _convert_to_dataframe(self, result) -> pd.DataFrame:
        """将 pywencai 返回结果转换为DataFrame（降级分支用，保留兼容）"""
        try:
            if result is None:
                return None

            df_result = None

            if isinstance(result, dict):
                try:
                    df_result = pd.DataFrame([result])
                except Exception:
                    return None
            elif isinstance(result, pd.DataFrame):
                df_result = result
            else:
                return None

            if df_result is None or df_result.empty:
                return None

            # 处理嵌套结构（tableV1）
            if 'tableV1' in df_result.columns and len(df_result.columns) == 1:
                table_v1_data = df_result.iloc[0]['tableV1']
                if isinstance(table_v1_data, pd.DataFrame):
                    df_result = table_v1_data
                elif isinstance(table_v1_data, list) and len(table_v1_data) > 0:
                    df_result = pd.DataFrame(table_v1_data)
                else:
                    return None

            # 处理嵌套结构（title_content等单列嵌套）
            # 如果只有一列，且该列的值是DataFrame，则展开
            if len(df_result.columns) == 1:
                col_name = df_result.columns[0]
                first_value = df_result.iloc[0][col_name]
                if isinstance(first_value, pd.DataFrame):
                    print(f"   检测到嵌套DataFrame（列名: {col_name}），正在展开...")
                    df_result = first_value

            return df_result if not df_result.empty else None

        except Exception as e:
            print(f"   转换DataFrame时出错: {str(e)}")
            return None

    # -- 格式化供 AI -------------------------------------------------------

    def format_risk_data_for_ai(self, risk_data: Dict[str, Any]) -> str:
        """格式化风险数据供AI分析使用 - 直接转换DataFrame为字符串"""
        if not risk_data or not risk_data.get('data_success'):
            return "未获取到风险数据"

        formatted_text = []

        try:
            sections = [
                ('lifting_ban', "【限售解禁数据】"),
                ('shareholder_reduction', "【大股东减持数据】"),
                ('important_events', "【重要事件数据】"),
            ]

            for key, title in sections:
                block = risk_data.get(key)
                if not block or not block.get('has_data') or block.get('data') is None:
                    continue

                formatted_text.append("=" * 80)
                formatted_text.append(title)
                formatted_text.append("=" * 80)
                formatted_text.append(f"数据来源: {block.get('query', '')}")
                if block.get('summary'):
                    formatted_text.append(f"摘要: {block['summary']}")
                formatted_text.append("")

                # 直接将DataFrame转换为字符串（最多50行）
                df = block.get('data')
                try:
                    df_str = df.head(50).to_string(index=False, max_rows=50, max_cols=20)
                    formatted_text.append(f"共 {len(df)} 条记录，显示前50条：")
                    formatted_text.append(df_str)
                except Exception as e:
                    formatted_text.append(f"数据转换失败: {str(e)}")
                formatted_text.append("")

            return "\n".join(formatted_text) if formatted_text else "暂无风险数据"

        except Exception as e:
            print(f"格式化风险数据时出错: {str(e)}")
            import traceback
            traceback.print_exc()
            return f"格式化风险数据时出错: {str(e)}"

    def _format_dataframe_for_ai(self, df: pd.DataFrame, data_type: str) -> str:
        """将DataFrame格式化为AI易读的文本格式"""
        lines = []

        # 显示数据总数
        lines.append(f"共 {len(df)} 条{data_type}记录")
        lines.append("")

        # 显示列名
        lines.append(f"数据字段：{', '.join(df.columns.tolist())}")
        lines.append("")

        # 逐行显示数据（最多显示50条，避免数据过大）
        max_rows = min(50, len(df))

        for idx, row in df.head(max_rows).iterrows():
            lines.append(f"【记录 {idx + 1}】")

            # 显示每个字段的值
            for col in df.columns:
                value = row[col]

                # 处理不同类型的值
                if pd.isna(value):
                    value_str = "无数据"
                elif isinstance(value, (int, float)):
                    value_str = str(value)
                else:
                    value_str = str(value)
                    # 限制过长的字符串
                    if len(value_str) > 200:
                        value_str = value_str[:200] + "..."

                lines.append(f"  {col}: {value_str}")

            lines.append("")

        if len(df) > max_rows:
            lines.append(f"... 还有 {len(df) - max_rows} 条记录（已省略）")
            lines.append("")

        return "\n".join(lines)


# 测试代码
if __name__ == "__main__":
    fetcher = RiskDataFetcher()

    for test_symbol in ["600000", "600375"]:
        print(f"\n{'#' * 70}")
        print(f"# 测试获取 {test_symbol} 的风险数据...")
        print(f"{'#' * 70}")

        risk_data = fetcher.get_risk_data(test_symbol)

        print("\n" + "=" * 60)
        print("获取结果:")
        print("=" * 60)
        print(f"数据获取成功: {risk_data['data_success']}")

        if risk_data['data_success']:
            print("\n格式化的风险数据:")
            print(fetcher.format_risk_data_for_ai(risk_data))
