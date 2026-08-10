"""
新闻数据获取模块
使用akshare获取股票的最新新闻信息（替代qstock）
"""

import pandas as pd
import sys
import io
import warnings
from datetime import datetime, timedelta
import akshare as ak

warnings.filterwarnings('ignore')

# 设置标准输出编码为UTF-8（仅在命令行环境，避免streamlit冲突）
def _setup_stdout_encoding():
    """仅在命令行环境设置标准输出编码"""
    if sys.platform == 'win32' and not hasattr(sys.stdout, '_original_stream'):
        try:
            # 检测是否在streamlit环境中
            import streamlit
            # 在streamlit中不修改stdout
            return
        except ImportError:
            # 不在streamlit环境，可以安全修改
            try:
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='ignore')
            except:
                pass

_setup_stdout_encoding()


class QStockNewsDataFetcher:
    """新闻数据获取类（使用akshare作为数据源）"""
    
    def __init__(self):
        self.max_items = 30  # 最多获取的新闻数量
        self.available = True
        print("✓ 新闻数据获取器初始化成功（akshare数据源）")
    
    def get_stock_news(self, symbol):
        """
        获取股票的新闻数据
        
        Args:
            symbol: 股票代码（6位数字）
            
        Returns:
            dict: 包含新闻数据的字典
        """
        data = {
            "symbol": symbol,
            "news_data": None,
            "data_success": False,
            "source": "qstock"
        }
        
        if not self.available:
            data["error"] = "qstock库未安装或不可用"
            return data
        
        # 只支持中国股票
        if not self._is_chinese_stock(symbol):
            data["error"] = "新闻数据仅支持中国A股股票"
            return data
        
        try:
            # 获取新闻数据
            print(f"📰 正在使用qstock获取 {symbol} 的最新新闻...")
            news_data = self._get_news_data(symbol)
            
            if news_data:
                data["news_data"] = news_data
                print(f"   ✓ 成功获取 {len(news_data.get('items', []))} 条新闻")
                data["data_success"] = True
                print("✅ 新闻数据获取完成")
            else:
                print("⚠️ 未能获取到新闻数据")
                
        except Exception as e:
            print(f"❌ 获取新闻数据失败: {e}")
            data["error"] = str(e)
        
        return data
    
    def _is_chinese_stock(self, symbol):
        """判断是否为中国股票"""
        return symbol.isdigit() and len(symbol) == 6
    
    def _get_news_data(self, symbol):
        """获取新闻数据（直接调用HTTP API，绕过akshare的pandas/pyarrow兼容问题）"""
        try:
            print(f"   正在获取 {symbol} 的新闻数据...")

            news_items = []

            # 方法1: 东方财富个股公告/新闻（直接HTTP调用）
            try:
                import requests as _req
                url = (
                    "https://np-anotice-stock.eastmoney.com/api/security/ann"
                    "?sr=-1&page_size=20&page_index=1&ann_type=A"
                    "&client_source=web&f_node=0&stock_list=" + symbol
                )
                resp = _req.get(url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                data = resp.json()
                if data.get("success") and data.get("data", {}).get("list"):
                    news_list = data["data"]["list"]
                    print(f"   ✓ 从东方财富获取到 {len(news_list)} 条公告/新闻")
                    for n in news_list[:self.max_items]:
                        news_items.append({
                            'source': '东方财富',
                            'title': n.get('title', ''),
                            'date': n.get('notice_date', '')[:10] if n.get('notice_date') else '',
                            'time': n.get('display_time', ''),
                            'column': n.get('columns', [{}])[0].get('column_name', '') if n.get('columns') else '',
                            'content': n.get('title', ''),
                            'url': f"https://data.eastmoney.com/notices/detail/{symbol}/{n.get('art_code', '')}.html"
                        })
                else:
                    print(f"   ⚠ 东方财富返回空数据")
            except Exception as e:
                print(f"   ⚠ 从东方财富获取失败: {e}")

            # 方法2: 新浪财经个股新闻（直接HTTP调用）
            try:
                import requests as _req
                # 根据代码前缀确定市场: 6开头=sh, 0/3开头=sz
                prefix = symbol[0]
                if prefix == '6':
                    sina_code = f"sh{symbol}"
                elif prefix in ('0', '3'):
                    sina_code = f"sz{symbol}"
                else:
                    sina_code = f"bj{symbol}" if prefix in ('8', '4') else f"sh{symbol}"

                url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sina_code}.phtml"
                resp = _req.get(url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                resp.encoding = 'gb2312'

                if resp.status_code == 200 and resp.text:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, 'lxml')
                    # 解析新浪新闻列表
                    news_rows = soup.select('.datelist ul li') or soup.find_all('li')
                    count = 0
                    for li in news_rows[:self.max_items]:
                        a_tag = li.find('a')
                        span_tag = li.find('span')
                        if a_tag:
                            title = a_tag.get_text(strip=True)
                            href = a_tag.get('href', '')
                            date_str = span_tag.get_text(strip=True) if span_tag else ''
                            if title:
                                news_items.append({
                                    'source': '新浪财经',
                                    'title': title,
                                    'date': date_str,
                                    'url': href if href.startswith('http') else f"https://vip.stock.finance.sina.com.cn{href}"
                                })
                                count += 1
                    if count > 0:
                        print(f"   ✓ 从新浪财经获取到 {count} 条新闻")
                    else:
                        print(f"   ⚠ 新浪财经未找到相关新闻（页面结构可能已变化）")
                else:
                    print(f"   ⚠ 新浪财经请求失败")
            except Exception as e:
                print(f"   ⚠ 从新浪财经获取失败: {e}")

            # 方法3: 财联社电报（直接HTTP调用）
            if len(news_items) < 5:
                try:
                    import requests as _req
                    url = "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=8.4.6"
                    resp = _req.get(url, timeout=15, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://www.cls.cn/telegraph"
                    })
                    data = resp.json()
                    if data.get("data", {}).get("roll_data"):
                        telegraph_list = data["data"]["roll_data"]
                        print(f"   ✓ 从财联社获取到 {len(telegraph_list)} 条电报")
                        for item in telegraph_list[:self.max_items]:
                            title = item.get('title', '') or item.get('content', '')
                            brief = item.get('brief', '') or item.get('content', '')
                            ctime = item.get('ctime', 0)
                            date_str = datetime.fromtimestamp(ctime).strftime('%Y-%m-%d %H:%M:%S') if ctime else ''
                            news_items.append({
                                'source': '财联社',
                                'title': title[:100] if title else '',
                                'date': date_str,
                                'content': brief[:500] if brief else ''
                            })
                    else:
                        print(f"   ⚠ 财联社返回空数据")
                except Exception as e:
                    print(f"   ⚠ 从财联社获取失败: {e}")
            
            if not news_items:
                print(f"   未找到股票 {symbol} 的新闻")
                return None
            
            # 限制数量
            news_items = news_items[:self.max_items]
            
            return {
                "items": news_items,
                "count": len(news_items),
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "date_range": "最近新闻"
            }
            
        except Exception as e:
            print(f"   获取新闻数据异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def format_news_for_ai(self, data):
        """
        将新闻数据格式化为适合AI阅读的文本
        """
        if not data or not data.get("data_success"):
            return "未能获取新闻数据"
        
        text_parts = []
        
        # 新闻数据
        if data.get("news_data"):
            news_data = data["news_data"]
            text_parts.append(f"""
【最新新闻 - akshare数据源】
查询时间：{news_data.get('query_time', 'N/A')}
时间范围：{news_data.get('date_range', 'N/A')}
新闻数量：{news_data.get('count', 0)}条

""")
            
            for idx, item in enumerate(news_data.get('items', []), 1):
                text_parts.append(f"新闻 {idx}:")
                
                # 优先显示的字段
                priority_fields = ['title', 'date', 'time', 'source', 'content', 'url']
                
                # 先显示优先字段
                for field in priority_fields:
                    if field in item:
                        value = item[field]
                        # 限制content长度
                        if field == 'content' and len(str(value)) > 500:
                            value = str(value)[:500] + "..."
                        text_parts.append(f"  {field}: {value}")
                
                # 再显示其他字段
                for key, value in item.items():
                    if key not in priority_fields and key != 'source':
                        # 跳过过长的字段
                        if len(str(value)) > 300:
                            value = str(value)[:300] + "..."
                        text_parts.append(f"  {key}: {value}")
                
                text_parts.append("")  # 空行分隔
        
        return "\n".join(text_parts)


# 测试函数
if __name__ == "__main__":
    print("测试新闻数据获取（akshare数据源）...")
    print("="*60)
    
    fetcher = QStockNewsDataFetcher()
    
    if not fetcher.available:
        print("❌ 新闻数据获取器不可用")
        sys.exit(1)
    
    # 测试股票
    test_symbols = ["000001", "600519"]  # 平安银行、贵州茅台
    
    for symbol in test_symbols:
        print(f"\n{'='*60}")
        print(f"正在测试股票: {symbol}")
        print(f"{'='*60}\n")
        
        data = fetcher.get_stock_news(symbol)
        
        if data.get("data_success"):
            print("\n" + "="*60)
            print("新闻数据获取成功！")
            print("="*60)
            
            formatted_text = fetcher.format_news_for_ai(data)
            print(formatted_text)
        else:
            print(f"\n获取失败: {data.get('error', '未知错误')}")
        
        print("\n")

