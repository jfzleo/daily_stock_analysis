# AlphaSift 每日选股推送

本能力在 [docs/alphasift-integration.md](./alphasift-integration.md) 的基础上加了一层「调度 + 通知」的封装：让 AlphaSift 不只是 Web 选股页里手动点一次，而是每天工作日自动跑一次，并把候选结果按现有所有通知渠道推送出去。

## 1. 功能定位

- 触发方式：GitHub Actions
  - 定时：工作日北京时间 18:00（cron `0 10 * * 1-5`）。
  - 手动：`workflow_dispatch` 可在 Actions 页面手动跑，支持覆盖策略 / 市场 / 数量 / dry-run / 强制运行。
- 选股能力：完全沿用 `alphasift.dsa_adapter.screen(..., use_llm=True)`，不在仓库内复刻策略逻辑。
- 通知渠道：复用 `NotificationService`，与 `00-daily-analysis.yml` 共享同一套 `secrets` / `vars`，配置过的所有渠道都会收到一份。
- 报告产物：每次运行在 `reports/alphasift_briefing_YYYYMMDD_HHMM.md` 写一份，并作为 workflow artifact 上传保留 30 天。

> AlphaSift 是第三方选股能力，结果仅供研究参考，不构成投资建议。

## 2. 涉及的文件

| 文件 | 作用 |
| --- | --- |
| `scripts/run_alphasift_briefing.py` | CLI：调用 AlphaSift → Markdown → NotificationService |
| `.github/workflows/01-daily-alphasift-briefing.yml` | GitHub Actions 调度入口 |
| `docs/alphasift-integration.md` | AlphaSift 适配层与 Web 选股页说明（原有） |
| `tests/test_alphasift_briefing.py` | CLI 单元测试（mock AlphaSift 与通知） |

## 3. GitHub 部署步骤

> 适用人群：你已经把 `daily_stock_analysis` fork / 推送到自己的 GitHub 仓库，并打算用 Actions 跑。

### 3.1 准备 fork

1. Fork 本仓库到你自己的 GitHub 账号（或 push 一份镜像）。
2. 在你的仓库 `Settings → Actions → General` 里确认：
   - `Actions permissions` 至少是 “Allow all actions and reusable workflows”。
   - `Workflow permissions` 选 “Read and write permissions”（默认即可）。

### 3.2 配置 Variables 与 Secrets

打开你的仓库 `Settings → Secrets and variables → Actions`：

非敏感配置写在 **Variables** 里（页签 `Variables`），敏感凭据写在 **Secrets** 里（页签 `Secrets`）。

#### Variables（推荐）

| 名称 | 说明 | 示例 |
| --- | --- | --- |
| `ALPHASIFT_BRIEFING_STRATEGY` | 默认单策略 ID | `dual_low` |
| `ALPHASIFT_BRIEFING_STRATEGIES` | 多策略列表（逗号分隔，优先级高于单策略） | `dual_low,growth_quality` |
| `ALPHASIFT_BRIEFING_MARKET` | 市场 | `cn` |
| `ALPHASIFT_BRIEFING_MAX_RESULTS` | 每个策略保留的候选数 | `5` |
| `ALPHASIFT_BRIEFING_TITLE` | 报告标题 | `AlphaSift 每日选股` |
| `ALPHASIFT_BRIEFING_ROUTE_TYPE` | 通知路由类型，默认 `report` | `report` |
| `ALPHASIFT_BRIEFING_TIMEOUT_MINUTES` | 单次运行超时（默认 30 分钟） | `45` |
| `ALPHASIFT_INSTALL_SPEC` | AlphaSift 安装来源（建议保持默认） | （默认即可） |

#### Secrets（必填项至少 1 个 LLM + 1 个通知渠道）

LLM（任选一个或多个，命名遵循仓库现有约定）：

- `GEMINI_API_KEY` / `GEMINI_API_KEYS`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_API_KEYS`
- `OPENAI_API_KEY` / `OPENAI_API_KEYS`（+ 可选 `OPENAI_BASE_URL` 走 vars）
- `ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEYS`
- 任何 `LLM_<CHANNEL>_API_KEY(S)` 渠道（参考 `00-daily-analysis.yml`）
- 数据相关：`TUSHARE_TOKEN`（强烈建议；AlphaSift 行情类策略需要）

通知渠道（任选一种或多种，工作流已支持所有渠道）：

- 飞书：`FEISHU_WEBHOOK_URL`、可选 `FEISHU_WEBHOOK_SECRET`
- 企业微信：`WECHAT_WEBHOOK_URL`
- Telegram：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
- 邮件：`EMAIL_SENDER`、`EMAIL_PASSWORD`、`EMAIL_RECEIVERS`
- PushPlus：`PUSHPLUS_TOKEN`
- Server酱3：`SERVERCHAN3_SENDKEY`
- 自定义 Webhook：`CUSTOM_WEBHOOK_URLS`
- 其余渠道（ntfy / Gotify / Pushover / Discord / Slack / AstrBot）同名 `secrets`

> 已经为「每日股票分析」配过这些 secrets 的话，**这里不需要重复添加**——工作流共用同一套命名。

### 3.3 启用工作流

1. 仓库主页 → `Actions` 标签页，第一次进入时点击 `I understand my workflows, go ahead and enable them` 启用。
2. 左侧列表找到 `AlphaSift 每日选股推送`，进入后右上角点 `Enable workflow`（如未自动启用）。
3. 等待最近一次 cron 触发，或者直接点 `Run workflow` 手动跑一次冒烟。

### 3.4 手动触发与覆盖参数

在 `Actions → AlphaSift 每日选股推送 → Run workflow` 弹窗里：

- `strategy`：单策略 ID（留空则用 Variables / 默认值）。
- `strategies`：逗号分隔的多策略，例如 `dual_low,growth_quality`，**优先级高于 strategy**。
- `market`：默认 `cn`，与 AlphaSift 支持范围一致。
- `max_results`：每个策略保留的候选数，默认 `5`。
- `force_run`：选 `true` 时即便 `ALPHASIFT_ENABLED=false` 也会跑（工作流默认已经把 `ALPHASIFT_ENABLED=true` 注入了环境）。
- `dry_run`：选 `true` 时跳过 AlphaSift 调用与通知，仅产出占位 Markdown 并打印——**首次配置时强烈建议先选这个跑一遍**，验证依赖安装、报告生成、artifact 上传是否正常。

### 3.5 验证流程

按以下顺序验证：

1. `dry_run=true` 跑一次：检查日志里能看到 `✅ alphasift.dsa_adapter 可用` 和占位 Markdown，artifact 中出现 `reports/alphasift_briefing_*.md`。
2. 配齐至少一个 LLM secret + 一个通知渠道 secret 后，`dry_run=false` 再跑一次：日志结尾应有 `AlphaSift 选股报告已推送到通知渠道。`，对应渠道收到一条新消息。
3. 失败时检查：
   - 日志中 `pip install "$ALPHASIFT_INSTALL_SPEC"` 是否成功；
   - `ALPHASIFT_ENABLED 未开启`：把 `force_run` 设 `true` 或新增 Variable `ALPHASIFT_ENABLED=true`；
   - `AlphaSift 选股报告推送失败`：可能是通知渠道 token 失效，本地用 `python main.py --check-notify` 复测；
   - `调用失败：...`：通常是 LLM 超时或 Tushare token 限流，可以放大 `LLM_TIMEOUT_SEC` Variable 或换备选 LLM。

## 4. 本地调试

```bash
# 1) 安装依赖 + AlphaSift 适配层
pip install -r requirements.txt
pip install git+https://github.com/ZhuLinsen/alphasift.git@b2ca66dd47001b9a09890cfe21c2b18c7219ccf5

# 2) 设置最少环境变量（示例）
export ALPHASIFT_ENABLED=true
export ALPHASIFT_BRIEFING_STRATEGY=dual_low
export GEMINI_API_KEY=...      # 任意一个可用 LLM 密钥
export FEISHU_WEBHOOK_URL=...  # 任意一个通知渠道

# 3) 干跑 + 真跑
python scripts/run_alphasift_briefing.py --dry-run
python scripts/run_alphasift_briefing.py --strategy dual_low --max-results 5
```

CLI 参数与 GitHub Actions inputs 一一对应；环境变量覆盖关系：`CLI 参数 > workflow inputs > Variables > Secrets > 默认值`。

## 5. 关闭与回滚

- 临时关闭：在 GitHub 仓库 `Actions → AlphaSift 每日选股推送`，右上角菜单选 `Disable workflow`。
- 永久回滚：删除 `.github/workflows/01-daily-alphasift-briefing.yml`、`scripts/run_alphasift_briefing.py`、`tests/test_alphasift_briefing.py` 三个文件即可；不影响 `00-daily-analysis.yml` 主流程，也不影响 Web 选股页。
- 切换策略：修改 Variable `ALPHASIFT_BRIEFING_STRATEGY` / `ALPHASIFT_BRIEFING_STRATEGIES`；不需要改代码。

## 6. 与「每日股票分析」工作流的关系

- 互相独立：本工作流只负责「全市场选股 + 推送」，`00-daily-analysis.yml` 负责「自选股 + 大盘复盘」。
- 可叠加运行：两者使用相同的 secrets / vars 命名，因此你可以同时启用，两份消息会先后到达。
- 通知路由：默认 `route_type=report`，会被 `NOTIFICATION_REPORT_CHANNELS` 控制；如想把 AlphaSift 推送单独路由到某些渠道，可新增 Variable `ALPHASIFT_BRIEFING_ROUTE_TYPE`（例如设为自定义的 `alphasift`）并在 `NOTIFICATION_REPORT_CHANNELS` / 自定义路由里做对应配置。
