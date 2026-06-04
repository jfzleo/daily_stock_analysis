#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日 AlphaSift 选股 + 通知推送封装。

复用 ``alphasift.dsa_adapter`` 适配层执行选股，将候选结果格式化成 Markdown
报告，通过 :class:`src.notification.NotificationService` 推送到所有已配置的
通知渠道，并把同一份报告保存到仓库根目录下的 ``reports/`` 目录便于审计。

设计目标：
- 不在 ``main.py`` 主分析流程之外引入额外耦合，可以单独定时触发。
- 不绑定单一通知渠道，复用现有的 NotificationService 多渠道分发。
- 与 ``api.v1.endpoints.alphasift`` 行为保持一致：默认 ``use_llm=True``、
  数据归一化与字段命名沿用 Web 选股页的语义，便于排查问题。

用法示例::

    python scripts/run_alphasift_briefing.py
    python scripts/run_alphasift_briefing.py --strategy dual_low
    python scripts/run_alphasift_briefing.py --strategies dual_low,growth_quality
    python scripts/run_alphasift_briefing.py --dry-run
    python scripts/run_alphasift_briefing.py --no-notify
    python scripts/run_alphasift_briefing.py --force-run

环境变量（均可被 CLI 参数覆盖）：

- ``ALPHASIFT_BRIEFING_STRATEGY``：默认策略 ID。
- ``ALPHASIFT_BRIEFING_STRATEGIES``：以逗号分隔的多策略列表，优先级高于
  ``ALPHASIFT_BRIEFING_STRATEGY``。
- ``ALPHASIFT_BRIEFING_MARKET``：市场（默认 ``cn``，当前 AlphaSift 仅支持 cn）。
- ``ALPHASIFT_BRIEFING_MAX_RESULTS``：每个策略保留的候选数（默认 5）。
- ``ALPHASIFT_BRIEFING_TITLE``：报告标题（默认 ``AlphaSift 每日选股``）。
- ``ALPHASIFT_BRIEFING_ROUTE_TYPE``：通知路由类型（默认 ``report``，与每日
  分析报告共享路由策略）。
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import logging
import math
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 注意：这里刻意避免在模块加载阶段导入 ``src.notification`` 与 ``src.config``。
# 它们的导入链会拉起整套数据源 / bot / LLM 依赖（tenacity、akshare 等），
# 不适合在轻量单元测试或 ``--dry-run`` 模式下被强制加载。需要时通过下方两个
# 包装函数惰性获取，测试可以直接 patch 这两个函数。

logger = logging.getLogger("alphasift_briefing")


def _get_config() -> Any:
    from src.config import get_config  # 局部导入，避免加载副作用

    return get_config()


def _build_notification_service() -> Any:
    from src.notification import NotificationService  # 局部导入

    return NotificationService()

ADAPTER_MODULE = "alphasift.dsa_adapter"
DEFAULT_STRATEGY = "dual_low"
DEFAULT_MARKET = "cn"
DEFAULT_MAX_RESULTS = 5
DEFAULT_TITLE = "AlphaSift 每日选股"
DEFAULT_ROUTE_TYPE = "report"


class AlphaSiftBriefingError(RuntimeError):
    """AlphaSift 调用失败的统一异常，便于 CLI 与测试捕获。"""


# ---------------------------------------------------------------------------
# AlphaSift 适配层调用
# ---------------------------------------------------------------------------


def _import_adapter() -> Any:
    try:
        return importlib.import_module(ADAPTER_MODULE)
    except ModuleNotFoundError as exc:
        raise AlphaSiftBriefingError(
            "未检测到 alphasift.dsa_adapter，请先安装 ALPHASIFT_INSTALL_SPEC 指向的 AlphaSift 适配层。"
        ) from exc
    except Exception as exc:  # pragma: no cover - 仅作兜底
        raise AlphaSiftBriefingError(f"导入 alphasift 适配层失败：{exc}") from exc


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except TypeError:
            pass
    if hasattr(value, "dict") and callable(getattr(value, "dict")):
        try:
            return value.dict()
        except TypeError:
            pass
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    return value


def _remove_non_finite(value: Any) -> Any:
    if isinstance(value, list):
        return [_remove_non_finite(item) for item in value]
    if isinstance(value, tuple):
        return [_remove_non_finite(item) for item in value]
    if isinstance(value, dict):
        return {key: _remove_non_finite(item) for key, item in value.items()}
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _call_screen(adapter: Any, strategy: str, market: str, max_results: int) -> Dict[str, Any]:
    screen = getattr(adapter, "screen", None)
    if not callable(screen):
        raise AlphaSiftBriefingError("alphasift.dsa_adapter.screen 不可调用，请检查适配层版本。")

    signature = inspect.signature(screen)
    params = signature.parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    kwargs: Dict[str, Any] = {"market": market}
    if "max_results" in params or accepts_kwargs:
        kwargs["max_results"] = max_results
    elif "max_output" in params:
        kwargs["max_output"] = max_results
    else:
        kwargs["max_results"] = max_results
    if "use_llm" in params or accepts_kwargs:
        kwargs["use_llm"] = True

    try:
        raw = screen(strategy, **kwargs)
    except TypeError:
        # 适配层签名差异时退化为纯位置参数
        raw = screen(strategy, market, max_results)

    plain = _remove_non_finite(_to_plain(raw))
    if not isinstance(plain, dict):
        plain = {"candidates": plain}
    return plain


def _normalize_candidates(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: Any = data.get("candidates") if isinstance(data, dict) else data
    if not isinstance(items, list) and isinstance(data, dict):
        for key in ("picks", "items", "results", "stocks"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
    if not isinstance(items, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(items, start=1):
        candidate = _remove_non_finite(_to_plain(raw))
        if not isinstance(candidate, dict):
            candidate = {"code": str(candidate)}
        source = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else candidate
        normalized.append({
            "rank": candidate.get("rank") or source.get("rank") or index,
            "code": (
                candidate.get("code")
                or source.get("code")
                or candidate.get("symbol")
                or source.get("symbol")
                or ""
            ),
            "name": (
                candidate.get("name")
                or source.get("name")
                or candidate.get("stock_name")
                or source.get("stock_name")
                or ""
            ),
            "industry": candidate.get("industry") or source.get("industry") or "",
            "price": candidate.get("price") if candidate.get("price") is not None else source.get("price"),
            "change_pct": candidate.get("change_pct") if candidate.get("change_pct") is not None else source.get("change_pct"),
            "risk_level": candidate.get("risk_level") or source.get("risk_level") or "",
            "score": candidate.get("score") if candidate.get("score") is not None else source.get("score"),
            "llm_score": candidate.get("llm_score") if candidate.get("llm_score") is not None else source.get("llm_score"),
            "llm_thesis": candidate.get("llm_thesis") or source.get("llm_thesis") or "",
            "llm_catalysts": candidate.get("llm_catalysts") or source.get("llm_catalysts") or [],
            "llm_risks": candidate.get("llm_risks") or source.get("llm_risks") or [],
            "llm_watch_items": candidate.get("llm_watch_items") or source.get("llm_watch_items") or [],
            "reason": candidate.get("reason") or source.get("reason") or "",
        })
    return normalized


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


def _format_number(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "-"
    return f"{number:.2f}{suffix}"


def _format_block(strategy: str, data: Dict[str, Any], candidates: List[Dict[str, Any]]) -> str:
    lines: List[str] = [f"## 🎯 策略：{strategy}", ""]

    meta_bits: List[str] = []
    snapshot = data.get("snapshot_count")
    after_filter = data.get("after_filter_count")
    llm_ranked = data.get("llm_ranked")
    llm_coverage = data.get("llm_coverage")
    run_id = data.get("run_id")
    if snapshot is not None:
        meta_bits.append(f"样本 {snapshot}")
    if after_filter is not None:
        meta_bits.append(f"过滤后 {after_filter}")
    if llm_ranked:
        meta_bits.append("LLM 已重排")
    if isinstance(llm_coverage, (int, float)) and math.isfinite(float(llm_coverage)):
        meta_bits.append(f"LLM 覆盖率 {_format_number(float(llm_coverage) * 100, '%')}")
    if run_id:
        meta_bits.append(f"run_id {run_id}")
    if meta_bits:
        lines.append("- " + " · ".join(meta_bits))
        lines.append("")

    if not candidates:
        lines.append("> 当前策略没有符合条件的候选股。")
        warnings_ = data.get("warnings") or []
        if warnings_:
            lines.append("")
            lines.append("⚠️ 警告：" + "；".join(str(w) for w in warnings_))
        return "\n".join(lines).rstrip()

    for index, candidate in enumerate(candidates, start=1):
        name = candidate.get("name") or "(未知)"
        code = candidate.get("code") or "-"
        industry = candidate.get("industry") or ""
        header = f"### {index}. {name}({code})"
        if industry:
            header += f" · {industry}"
        lines.append(header)

        info_bits: List[str] = []
        if candidate.get("score") is not None:
            info_bits.append(f"评分 {_format_number(candidate.get('score'))}")
        if candidate.get("llm_score") is not None:
            info_bits.append(f"LLM {_format_number(candidate.get('llm_score'))}")
        if candidate.get("price") is not None:
            info_bits.append(f"现价 {_format_number(candidate.get('price'))}")
        if candidate.get("change_pct") is not None:
            info_bits.append(f"涨跌 {_format_number(candidate.get('change_pct'), '%')}")
        info_bits.append(f"风险 {candidate.get('risk_level') or '-'}")
        lines.append("- " + " · ".join(info_bits))

        thesis = candidate.get("llm_thesis") or candidate.get("reason")
        if thesis:
            lines.append(f"- 观点：{thesis}")
        catalysts = candidate.get("llm_catalysts") or []
        if catalysts:
            lines.append("- 催化：" + "、".join(str(item) for item in catalysts[:3]))
        risks = candidate.get("llm_risks") or []
        if risks:
            lines.append("- 风险：" + "、".join(str(item) for item in risks[:3]))
        watch = candidate.get("llm_watch_items") or []
        if watch:
            lines.append("- 关注：" + "、".join(str(item) for item in watch[:3]))
        lines.append("")

    source_errors = data.get("source_errors") or []
    if source_errors:
        lines.append("⚠️ 数据源异常：" + "；".join(str(item) for item in source_errors))
        lines.append("")
    warnings_ = data.get("warnings") or []
    if warnings_:
        lines.append("⚠️ 警告：" + "；".join(str(item) for item in warnings_))
        lines.append("")
    return "\n".join(lines).rstrip()


def build_briefing(
    strategies: Iterable[str],
    market: str,
    max_results: int,
    *,
    title: str = DEFAULT_TITLE,
) -> Dict[str, Any]:
    """调用 AlphaSift 并返回 Markdown + 元信息。"""

    strategies = [s for s in strategies if s]
    if not strategies:
        raise AlphaSiftBriefingError("至少需要一个 AlphaSift 策略。")

    adapter = _import_adapter()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    blocks: List[str] = [
        f"# 🌟 {title}",
        "",
        f"- 生成时间：{timestamp}",
        f"- 市场：{market}",
        f"- 策略：{', '.join(strategies)}",
        "",
    ]
    raw_results: List[Dict[str, Any]] = []
    candidate_total = 0

    for strategy in strategies:
        try:
            data = _call_screen(adapter, strategy, market, max_results)
        except AlphaSiftBriefingError:
            raise
        except Exception as exc:  # pragma: no cover - 适配层异常类型不确定
            logger.warning("AlphaSift 策略 %s 调用失败：%s", strategy, exc)
            blocks.append(f"## ❌ 策略：{strategy}")
            blocks.append("")
            blocks.append(f"调用失败：{exc}")
            blocks.append("")
            raw_results.append({"strategy": strategy, "error": str(exc)})
            continue

        candidates = _normalize_candidates(data)[:max_results]
        raw_results.append({
            "strategy": strategy,
            "market": data.get("market") or market,
            "run_id": data.get("run_id"),
            "snapshot_count": data.get("snapshot_count"),
            "after_filter_count": data.get("after_filter_count"),
            "llm_ranked": bool(data.get("llm_ranked")),
            "llm_coverage": data.get("llm_coverage"),
            "candidates": candidates,
            "warnings": data.get("warnings") or [],
            "source_errors": data.get("source_errors") or [],
        })
        candidate_total += len(candidates)
        blocks.append(_format_block(strategy, data, candidates))
        blocks.append("")

    blocks.append("---")
    blocks.append("> AlphaSift 第三方选股结果，仅供研究参考，不构成投资建议。")

    return {
        "markdown": "\n".join(blocks).rstrip() + "\n",
        "candidate_total": candidate_total,
        "results": raw_results,
        "generated_at": timestamp,
        "strategies": strategies,
        "market": market,
        "max_results": max_results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_strategies(args: argparse.Namespace) -> List[str]:
    if args.strategies:
        items = [item.strip() for item in args.strategies.split(",") if item.strip()]
        if items:
            return items
    if args.strategy:
        return [args.strategy.strip()]

    env_multi = os.getenv("ALPHASIFT_BRIEFING_STRATEGIES", "").strip()
    if env_multi:
        items = [item.strip() for item in env_multi.split(",") if item.strip()]
        if items:
            return items

    env_single = os.getenv("ALPHASIFT_BRIEFING_STRATEGY", "").strip()
    if env_single:
        return [env_single]

    return [DEFAULT_STRATEGY]


def _resolve_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if value <= 0:
            return default
        return value
    except ValueError:
        return default


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AlphaSift 每日选股 + 通知推送封装")
    parser.add_argument("--strategy", help="单个策略 ID（覆盖环境变量）")
    parser.add_argument("--strategies", help="多个策略 ID，使用英文逗号分隔")
    parser.add_argument("--market", default=None, help="市场代码，默认读取环境变量或 cn")
    parser.add_argument("--max-results", type=int, default=None, help="每个策略保留的候选数")
    parser.add_argument("--dry-run", action="store_true", help="不调用 AlphaSift，仅生成占位报告并打印")
    parser.add_argument("--no-notify", action="store_true", help="生成并保存报告，但不调用通知服务")
    parser.add_argument("--force-run", action="store_true", help="忽略 ALPHASIFT_ENABLED=false")
    parser.add_argument("--output", help="自定义保存路径")
    parser.add_argument(
        "--title",
        default=os.getenv("ALPHASIFT_BRIEFING_TITLE", DEFAULT_TITLE),
        help="报告标题",
    )
    parser.add_argument(
        "--route-type",
        default=os.getenv("ALPHASIFT_BRIEFING_ROUTE_TYPE", DEFAULT_ROUTE_TYPE),
        help="通知路由类型，默认 report",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def _save_report(markdown: str, *, custom_path: Optional[str] = None) -> Path:
    if custom_path:
        path = Path(custom_path)
    else:
        reports_dir = REPO_ROOT / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d_%H%M")
        path = reports_dir / f"alphasift_briefing_{date_str}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def run(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    config = _get_config()
    if not config.alphasift_enabled and not args.force_run:
        logger.error(
            "ALPHASIFT_ENABLED 未开启；如确认要运行请设置 ALPHASIFT_ENABLED=true 或使用 --force-run。"
        )
        return 2

    strategies = _resolve_strategies(args)
    market = (
        (args.market or os.getenv("ALPHASIFT_BRIEFING_MARKET") or DEFAULT_MARKET).strip()
        or DEFAULT_MARKET
    )
    max_results = (
        args.max_results
        if args.max_results is not None
        else _resolve_int_env("ALPHASIFT_BRIEFING_MAX_RESULTS", DEFAULT_MAX_RESULTS)
    )
    logger.info(
        "AlphaSift briefing 启动 | 策略=%s 市场=%s 数量=%s dry_run=%s no_notify=%s",
        strategies,
        market,
        max_results,
        args.dry_run,
        args.no_notify,
    )

    if args.dry_run:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        markdown = (
            f"# 🌟 {args.title}\n\n"
            f"- 生成时间：{timestamp}\n"
            f"- 市场：{market}\n"
            f"- 策略：{', '.join(strategies)}\n\n"
            "> Dry-run 模式：未实际调用 AlphaSift，未推送通知。\n"
        )
        candidate_total = 0
    else:
        try:
            briefing = build_briefing(strategies, market, max_results, title=args.title)
        except AlphaSiftBriefingError as exc:
            logger.error("AlphaSift 调用失败：%s", exc)
            return 3
        markdown = briefing["markdown"]
        candidate_total = briefing["candidate_total"]

    output_path = _save_report(markdown, custom_path=args.output)
    logger.info(
        "AlphaSift 选股报告已保存：%s（候选 %s 只）", output_path, candidate_total
    )

    if args.no_notify or args.dry_run:
        print(markdown)
        return 0

    service = _build_notification_service()
    if not service.is_available():
        logger.warning("未检测到任何已配置的通知渠道，仅保存报告未推送。")
        return 0

    ok = service.send(markdown, route_type=str(args.route_type or DEFAULT_ROUTE_TYPE))
    if ok:
        logger.info("AlphaSift 选股报告已推送到通知渠道。")
        return 0
    logger.error("AlphaSift 选股报告推送失败，请检查通知渠道配置。")
    return 4


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(run())
