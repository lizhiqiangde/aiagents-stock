# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Streamlit-based web app that orchestrates multiple AI agents (via DeepSeek or any OpenAI-compatible API) to perform comprehensive stock analysis for Chinese A-shares, Hong Kong stocks, and US stocks. All UI text, prompts, and documentation are in Chinese.

## Common Commands

### Setup

```bash
# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1   # Windows PowerShell

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser (required for 选股/stock screening via iwencai.com)
playwright install chromium

# Copy and configure environment
cp .env.example .env
# Edit .env — at minimum set DEEPSEEK_API_KEY
```

### Running

```bash
# Launch the app
python run.py                      # Wrapper that checks deps, then starts Streamlit
# or directly:
streamlit run app.py --server.port 8503

# The app is then available at http://localhost:8503
```

### Docker

```bash
# Standard build
docker build -t agentsstock1 .
docker run -d -p 8503:8501 -v $(pwd)/.env:/app/.env --name agentsstock1 agentsstock1

# With China mirrors (faster for mainland users)
docker build -f "Dockerfile国内源版" -t agentsstock1 .

# Docker Compose
docker-compose up -d
docker-compose logs -f
```

### Testing

```bash
# You.com Research module unit tests (no API key needed)
python -m pytest utils/test_youchannels_research.py -v

# Integration test (requires YDC_API_KEY env var)
python -m pytest utils/test_youchannels_research.py::TestGetYoudotcomResearchIntegration -v
```

Tests are minimal — there are no other test suites in the project.

## Architecture

### Entry Point → Feature Routing

`app.py` is the single-page Streamlit entry point (~2800 lines). Navigation works via `st.session_state` boolean flags (e.g., `show_main_force`, `show_sector_strategy`). Sidebar buttons toggle these flags, and `main()` checks them sequentially to render the corresponding feature UI. There is no router library — it is a manual if/elif chain in `main()`.

### Shared Core (used by all features)

| Module | Role |
|---|---|
| `config.py` | Loads `.env` via `python-dotenv` (`override=True`); exports `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEFAULT_MODEL_NAME`, `TUSHARE_TOKEN`, `MINIQMT_CONFIG`, `TDX_CONFIG` |
| `deepseek_client.py` | `DeepSeekClient` — wraps `openai.OpenAI` pointed at the configured base URL. All features share this one client. Handles `reasoner` models specially (extracts `reasoning_content`, raises `max_tokens` to 8000). |
| `stock_data.py` | `StockDataFetcher` — auto-detects A-share (6-digit), HK (1-5 digit / HK prefix), and US (alphabetic) tickers; routes to appropriate data APIs. |
| `data_source_manager.py` | Multi-source with auto-fallback: tries primary source → retries 3× → falls back to alternates (Tushare → Sina → Tencent). Patches `requests` with browser headers via `utils/akshare_helper.py`. |
| `config_manager.py` | `ConfigManager` — CRUD on `.env` for the in-app environment config UI. |
| `notification_service.py` | Email (SMTP) + Webhook (DingTalk/Feishu) notifications. |

### Feature Module Pattern

Each feature follows a consistent decomposition across files. Not every feature has all layers, but the pattern is:

```
<feature>_data.py       # External data fetching
<feature>_agents.py      # AI agent prompt templates + LLM calls
<feature>_engine.py      # Orchestration: data → agents → synthesis
<feature>_db.py          # SQLite persistence
<feature>_ui.py          # Streamlit UI rendering
<feature>_pdf.py         # ReportLab PDF export
<feature>_scheduler.py   # Background scheduled tasks (uses `schedule` library)
```

### AI Agent Model

All AI agents follow the same pattern:
1. Build a detailed Chinese-language system + user prompt with structured sections
2. Call `deepseek_client.call_api(messages)` (shared `DeepSeekClient` instance)
3. Return a dict with `agent_name`, `agent_role`, `analysis`, `focus_areas`, `timestamp`

For single-stock analysis (`ai_agents.py`), 6 analysts run in sequence, then a final "team discussion" prompt synthesizes all outputs into one investment decision.

Feature-specific agents (智策板块, 智瞰龙虎, 新闻流量, 宏观分析, etc.) follow the same prompt → call → dict pattern but with domain-specific prompts.

### Data Sources

```
Historical K-line  → Tencent proxy.finance.qq.com
Real-time quotes   → Sina hq.sinajs.cn
Stock fundamentals → Sina hq.sinajs.cn
Financial reports  → Tonghuashun / Sina
Stock screening    → iwencai.com (via Playwright browser for CAPTCHA bypass)
Fund flow          → Eastmoney (degraded) → skipped on failure
Fallback           → Tushare (requires TUSHARE_TOKEN in .env)
US/HK stocks       → Yahoo Finance (yfinance)
```

Key utilities for data access:
- `utils/akshare_helper.py` — monkey-patches `requests` with browser User-Agent headers + retry decorator
- `utils/iwencai_browser.py` — headless Chromium via Playwright to obtain real browser cookies for iwencai.com (5-minute cache)
- `utils/pywencai_helper.py` — `safe_get()` wrapper: direct call → browser cookie retry fallback

### Persistence

Ten separate SQLite `.db` files in the project root, each managed by its own dedicated class. There is no shared ORM or connection pool — each module opens its own connection via `sqlite3` directly (some use `peewee` ORM). All database files are gitignored except the schema-creating code.

### Key Feature Modules

| Feature (侧边栏) | Files | Purpose |
|---|---|---|
| 股票分析 (单股) | `ai_agents.py`, `stock_data.py` | 6-analyst team: technical, fundamental, fund flow, risk, sentiment, news |
| 批量分析 | in `app.py` | Sequential or parallel batch analysis of multiple tickers |
| 智瞰龙虎 | `longhubang_*.py` (7 files) | Dragon-and-tiger board data → 5 AI analysts → stock picks for next day |
| 智策板块 | `sector_strategy_*.py` (7 files) | Sector rotation, bullish/bearish prediction, heat ranking, scheduled analysis |
| 主力选股 | `main_force_*.py` (6 files) | Main capital flow screening → AI team picks 3-5 best stocks |
| 新闻流量 | `news_flow_*.py` (10 files) | Multi-platform news monitoring → AI impact analysis on sectors/stocks |
| 宏观分析 | `macro_analysis_*.py` (4 files) | National Bureau of Statistics data → sector mapping |
| 宏观周期 | `macro_cycle_*.py` (5 files) | Kondratieff cycle + Merrill Lynch clock + China policy analysis |
| 选股板块 | `low_price_bull_*.py`, `small_cap_*.py`, `profit_growth_*.py`, `value_stock_*.py` | Various screening strategies via iwencai.com |
| 实时监测 | `monitor_*.py` | Price threshold monitoring with trading-hours-aware scheduler |
| AI盯盘 | `smart_monitor_*.py` (7 files) | Automated watch with K-line pattern recognition, TDX data, MiniQMT trading |
| 持仓分析 | `portfolio_*.py` | Portfolio tracking, batch analysis, scheduled analysis |

## Key Patterns & Conventions

### Model Configuration
- All models go through `.env` → `config.DEFAULT_MODEL_NAME` → `DeepSeekClient(model=...)`.
- The `StockAnalysisAgents` and all feature agents accept an optional `model` parameter.
- Switching models requires only changing `DEFAULT_MODEL_NAME` in `.env` and restarting.
- `model_config.py` lists 27 preset model options for the UI dropdown.

### Error Handling
- `retry_on_failure` decorator in `utils/akshare_helper.py` for flaky API calls.
- `safe_get()` in `utils/pywencai_helper.py` with dual-path fallback.
- Multi-source auto-fallback in `data_source_manager.py`.
- Features degrade gracefully: a failed data fetch shows a warning, never crashes the app.

### Streamlit Session State
- Feature flags: `st.session_state.show_<feature>` booleans for navigation.
- Analysis results: `st.session_state.batch_results`, `st.session_state.current_analysis`, etc.
- Background service handles: `st.session_state.monitor_running`, scheduler thread references.
- The `.streamlit/config.toml` sets port 8503 and light theme.

### Adding a New Feature
Follow the existing pattern:
1. Create `feature_data.py` — fetch data from external sources
2. Create `feature_agents.py` — define AI agent prompts using `DeepSeekClient`
3. Create `feature_engine.py` — orchestrate data → analysis → results
4. Create `feature_db.py` — SQLite persistence if needed
5. Create `feature_ui.py` — `display_feature()` function
6. Import and add a navigation button + conditional render in `app.py`
7. Add any new env vars to `.env.example` and `config_manager.py`
