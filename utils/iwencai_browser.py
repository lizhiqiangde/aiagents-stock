"""
iwencai 浏览器会话模块

使用 Playwright (Chromium) 启动真实浏览器获取 cookies，
解决 pywencai 因 TLS 指纹 / IP 限制被 iwencai 服务器
要求验证码 (CAPTCHA) 的问题。

工作原理：
1. 启动无头 Chrome 浏览器
2. 访问 iwencai.com 获取真实浏览器 cookies
3. 将 cookies 注入 pywencai 的请求头
4. pywencai 使用浏览器级别的会话发送查询

使用方式（在 safe_get 中自动调用）：
    from utils.iwencai_browser import get_browser_cookies
    cookies = get_browser_cookies()
    result = pywencai.get(query=..., cookie=cookies)
"""

import io
import sys
import time
import logging

logger = logging.getLogger(__name__)


def _setup_stdout_encoding():
    """把 stdout 设为 UTF-8，避免 GBK 控制台在 print emoji 时崩溃。

    本模块的提示语带 emoji。Windows 控制台默认 GBK 时 `print('🚀')` 会抛
    UnicodeEncodeError —— 而它排在 `try` **之前**，导致函数连浏览器都启动不了；
    更糟的是 except 分支里的 print 也会因为同样的原因再抛一次，把原始异常盖掉。

    ⚠️ 这里**不**沿用「能 import streamlit 就跳过」的老写法：Streamlit 应用
    同样可能从 GBK 控制台启动，跳过等于保护失效。用 errors='replace' 后，
    重新配置在两种环境下都只会放宽而不会收紧，故总是执行。

    优先 `reconfigure`：`sys.stdout` 可能已被包装过而没有 `.buffer`
    属性（此时 `TextIOWrapper(sys.stdout.buffer, ...)` 会 AttributeError）。
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

# 缓存浏览器 cookies（每次有效期为5分钟）
_cookie_cache = None
_cookie_time = 0
_COOKIE_TTL = 300  # 5秒后重新获取


def get_browser_cookies(force_refresh=False):
    """
    通过 Playwright 无头浏览器获取 iwencai 的有效 cookies。
    
    适用于 pywencai 因 TLS 指纹问题被 iwencai 服务器要求验证码的场景。
    浏览器环境提供真实的 TLS 握手和 JavaScript 执行环境。
    
    Args:
        force_refresh: 是否强制刷新（忽略缓存）
    
    Returns:
        str: cookie 字符串，可用于 pywencai.get(cookie=...)
              失败时返回空字符串。
    """
    global _cookie_cache, _cookie_time
    
    # 如果缓存还在有效期内，直接返回
    now = time.time()
    if not force_refresh and _cookie_cache and (now - _cookie_time) < _COOKIE_TTL:
        return _cookie_cache
    
    print(f"[iwencai] 🚀 启动浏览器获取会话...")
    print(f"[iwencai] 💡 如果获取失败，请确保在浏览器中登录了 https://www.iwencai.com")
    
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright 未安装，无法获取浏览器 cookies")
        return ""
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                ),
            )
            page = context.new_page()
            
            # 访问 iwencai 主站，触发 cookie 设置
            page.goto('https://www.iwencai.com/', wait_until='load', timeout=30000)
            time.sleep(2)  # 等待 JS 设置 cookie
            
            # 获取所有 cookies，拼接成字符串
            cookies = context.cookies()
            cookie_str = '; '.join(
                f'{c["name"]}={c["value"]}'
                for c in cookies
            )
            
            browser.close()
            
            if cookie_str:
                _cookie_cache = cookie_str
                _cookie_time = time.time()
                print(f"[iwencai] ✅ 成功获取浏览器会话")
                return cookie_str
            else:
                print(f"[iwencai] ⚠️ 浏览器未返回任何 cookies")
                print(f"[iwencai] 💡 请用浏览器打开 https://www.iwencai.com 并确保已登录")
                return ""
            
    except Exception as e:
        print(f"[iwencai] ❌ 获取浏览器会话失败: {e}")
        print(f"[iwencai] 💡 请尝试手动打开 https://www.iwencai.com/screener 并登录")
        return ""
