# -*- coding: utf-8 -*-
"""Tests for the AlphaSift daily briefing CLI."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 部分依赖（litellm）在精简的 CI 环境里没有安装，预先 mock 掉，
# 否则 src.notification → src.config → src.llm 的导入链会失败。
try:  # pragma: no cover - 只在缺包时生效
    import litellm  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.modules["litellm"] = MagicMock()

from scripts import run_alphasift_briefing  # noqa: E402


def _adapter(screen_returns):
    """构造一个最小的 alphasift.dsa_adapter 替身。"""

    def screen(strategy, *, market="cn", max_results=5, use_llm=True):
        value = screen_returns.get(strategy)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(strategy=strategy, market=market, max_results=max_results, use_llm=use_llm)
        return value

    return SimpleNamespace(
        screen=screen,
        list_strategies=lambda: [],
        get_status=lambda: {"supported_markets": ["cn"]},
    )


class BuildBriefingTests(unittest.TestCase):
    def test_single_strategy_renders_markdown(self) -> None:
        adapter = _adapter({
            "dual_low": {
                "strategy": "dual_low",
                "market": "cn",
                "run_id": "20260604-001",
                "snapshot_count": 100,
                "after_filter_count": 5,
                "llm_ranked": True,
                "llm_coverage": 1.0,
                "candidates": [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "industry": "白酒",
                        "score": 88.5,
                        "llm_score": 90,
                        "price": 1680.5,
                        "change_pct": 1.23,
                        "risk_level": "低",
                        "llm_thesis": "高端白酒龙头，业绩稳健。",
                        "llm_catalysts": ["分红预案", "动销改善"],
                        "llm_risks": ["消费疲软"],
                        "llm_watch_items": ["关注一季报"],
                    }
                ],
                "warnings": [],
                "source_errors": [],
            }
        })

        with patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter):
            briefing = run_alphasift_briefing.build_briefing(
                ["dual_low"], "cn", 5, title="AlphaSift 每日选股"
            )

        markdown = briefing["markdown"]
        self.assertEqual(briefing["candidate_total"], 1)
        self.assertIn("AlphaSift 每日选股", markdown)
        self.assertIn("贵州茅台(600519)", markdown)
        self.assertIn("LLM 已重排", markdown)
        self.assertIn("分红预案", markdown)
        self.assertIn("消费疲软", markdown)
        self.assertIn("不构成投资建议", markdown)

    def test_multiple_strategies_merge(self) -> None:
        adapter = _adapter({
            "dual_low": {"candidates": [{"code": "600519", "name": "贵州茅台"}]},
            "growth_quality": {"candidates": [{"code": "300750", "name": "宁德时代"}]},
        })

        with patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter):
            briefing = run_alphasift_briefing.build_briefing(
                ["dual_low", "growth_quality"], "cn", 5
            )

        self.assertEqual(briefing["candidate_total"], 2)
        self.assertIn("策略：dual_low", briefing["markdown"])
        self.assertIn("策略：growth_quality", briefing["markdown"])
        self.assertIn("贵州茅台", briefing["markdown"])
        self.assertIn("宁德时代", briefing["markdown"])

    def test_source_fallback_noise_suppressed_when_candidates_exist(self) -> None:
        """有候选结果时，数据源 fallback 降级信息不应出现在报告中。"""
        adapter = _adapter({
            "dual_low": {
                "candidates": [{"code": "600519", "name": "贵州茅台"}],
                "source_errors": [
                    "efinance: Expecting value: line 1 column 1 (char 0)",
                    "akshare_em: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))",
                ],
                "warnings": [
                    "Snapshot source fallback: efinance: Expecting value: line 1 column 1 (char 0)",
                    "Snapshot source fallback: akshare_em: ('Connection aborted.', RemoteDisconnected(...))",
                ],
            }
        })

        with patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter):
            briefing = run_alphasift_briefing.build_briefing(["dual_low"], "cn", 5)

        markdown = briefing["markdown"]
        self.assertEqual(briefing["candidate_total"], 1)
        self.assertIn("贵州茅台", markdown)
        # fallback 噪音不应出现在报告中
        self.assertNotIn("数据源异常", markdown)
        self.assertNotIn("Expecting value", markdown)
        self.assertNotIn("Connection aborted", markdown)
        self.assertNotIn("source fallback", markdown)

    def test_source_errors_shown_when_no_candidates(self) -> None:
        """无候选结果时，source_errors 和 warnings 应当正常展示以帮助排查。"""
        adapter = _adapter({
            "dual_low": {
                "candidates": [],
                "source_errors": [
                    "efinance: Expecting value: line 1 column 1 (char 0)",
                ],
                "warnings": ["数据为空"],
            }
        })

        with patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter):
            briefing = run_alphasift_briefing.build_briefing(["dual_low"], "cn", 5)

        markdown = briefing["markdown"]
        self.assertEqual(briefing["candidate_total"], 0)
        # 无候选时警告应保留
        self.assertIn("数据为空", markdown)

    def test_empty_result_shows_placeholder(self) -> None:
        adapter = _adapter({
            "dual_low": {"candidates": [], "warnings": ["数据为空"]},
        })

        with patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter):
            briefing = run_alphasift_briefing.build_briefing(["dual_low"], "cn", 5)

        self.assertEqual(briefing["candidate_total"], 0)
        self.assertIn("没有符合条件的候选股", briefing["markdown"])
        self.assertIn("数据为空", briefing["markdown"])

    def test_screen_exception_does_not_abort_other_strategies(self) -> None:
        adapter = _adapter({
            "dual_low": RuntimeError("boom"),
            "growth_quality": {"candidates": [{"code": "300750", "name": "宁德时代"}]},
        })

        with patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter):
            briefing = run_alphasift_briefing.build_briefing(
                ["dual_low", "growth_quality"], "cn", 5
            )

        self.assertIn("❌ 策略：dual_low", briefing["markdown"])
        self.assertIn("调用失败：boom", briefing["markdown"])
        self.assertIn("宁德时代", briefing["markdown"])
        self.assertEqual(briefing["candidate_total"], 1)

    def test_adapter_missing_raises_briefing_error(self) -> None:
        with patch.object(
            run_alphasift_briefing,
            "_import_adapter",
            side_effect=run_alphasift_briefing.AlphaSiftBriefingError("missing"),
        ):
            with self.assertRaises(run_alphasift_briefing.AlphaSiftBriefingError):
                run_alphasift_briefing.build_briefing(["dual_low"], "cn", 5)


class RunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # 隔离临时报告输出目录，避免污染仓库 reports/
        self._tmp_dir = REPO_ROOT / "reports" / ".test_alphasift_briefing"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        for child in self._tmp_dir.glob("*"):
            try:
                child.unlink()
            except OSError:
                pass
        try:
            self._tmp_dir.rmdir()
        except OSError:
            pass

    def _output_path(self, name: str) -> str:
        return str(self._tmp_dir / name)

    def test_dry_run_does_not_call_adapter_or_notification(self) -> None:
        config = SimpleNamespace(alphasift_enabled=False)
        service = MagicMock()
        service.is_available.return_value = True
        with patch.object(run_alphasift_briefing, "_get_config", return_value=config), \
             patch.object(run_alphasift_briefing, "_import_adapter") as mock_import, \
             patch.object(run_alphasift_briefing, "_build_notification_service", return_value=service):
            exit_code = run_alphasift_briefing.run([
                "--dry-run",
                "--force-run",
                "--output", self._output_path("dry.md"),
            ])

        self.assertEqual(exit_code, 0)
        mock_import.assert_not_called()
        service.send.assert_not_called()
        self.assertTrue((self._tmp_dir / "dry.md").exists())
        self.assertIn("Dry-run", (self._tmp_dir / "dry.md").read_text(encoding="utf-8"))

    def test_alphasift_disabled_returns_error_without_force_run(self) -> None:
        config = SimpleNamespace(alphasift_enabled=False)
        with patch.object(run_alphasift_briefing, "_get_config", return_value=config), \
             patch.object(run_alphasift_briefing, "_import_adapter") as mock_import, \
             patch.object(run_alphasift_briefing, "_build_notification_service") as mock_service:
            exit_code = run_alphasift_briefing.run(["--strategy", "dual_low"])

        self.assertEqual(exit_code, 2)
        mock_import.assert_not_called()
        mock_service.assert_not_called()

    def test_no_notify_saves_report_but_skips_send(self) -> None:
        config = SimpleNamespace(alphasift_enabled=True)
        adapter = _adapter({"dual_low": {"candidates": [{"code": "600519", "name": "贵州茅台"}]}})
        service = MagicMock()
        service.is_available.return_value = True
        with patch.object(run_alphasift_briefing, "_get_config", return_value=config), \
             patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter), \
             patch.object(run_alphasift_briefing, "_build_notification_service", return_value=service):
            exit_code = run_alphasift_briefing.run([
                "--strategy", "dual_low",
                "--no-notify",
                "--output", self._output_path("no_notify.md"),
            ])

        self.assertEqual(exit_code, 0)
        service.send.assert_not_called()
        self.assertIn("贵州茅台", (self._tmp_dir / "no_notify.md").read_text(encoding="utf-8"))

    def test_send_invoked_with_report_route(self) -> None:
        config = SimpleNamespace(alphasift_enabled=True)
        adapter = _adapter({"dual_low": {"candidates": [{"code": "600519", "name": "贵州茅台"}]}})
        service = MagicMock()
        service.is_available.return_value = True
        service.send.return_value = True
        with patch.object(run_alphasift_briefing, "_get_config", return_value=config), \
             patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter), \
             patch.object(run_alphasift_briefing, "_build_notification_service", return_value=service):
            exit_code = run_alphasift_briefing.run([
                "--strategy", "dual_low",
                "--output", self._output_path("send.md"),
            ])

        self.assertEqual(exit_code, 0)
        service.send.assert_called_once()
        args, kwargs = service.send.call_args
        self.assertEqual(kwargs.get("route_type"), "report")
        self.assertIn("贵州茅台", args[0])

    def test_notification_failure_returns_nonzero(self) -> None:
        config = SimpleNamespace(alphasift_enabled=True)
        adapter = _adapter({"dual_low": {"candidates": [{"code": "600519", "name": "贵州茅台"}]}})
        service = MagicMock()
        service.is_available.return_value = True
        service.send.return_value = False
        with patch.object(run_alphasift_briefing, "_get_config", return_value=config), \
             patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter), \
             patch.object(run_alphasift_briefing, "_build_notification_service", return_value=service):
            exit_code = run_alphasift_briefing.run([
                "--strategy", "dual_low",
                "--output", self._output_path("fail.md"),
            ])

        self.assertEqual(exit_code, 4)

    def test_no_available_channel_returns_zero_with_warning(self) -> None:
        config = SimpleNamespace(alphasift_enabled=True)
        adapter = _adapter({"dual_low": {"candidates": [{"code": "600519", "name": "贵州茅台"}]}})
        service = MagicMock()
        service.is_available.return_value = False
        with patch.object(run_alphasift_briefing, "_get_config", return_value=config), \
             patch.object(run_alphasift_briefing, "_import_adapter", return_value=adapter), \
             patch.object(run_alphasift_briefing, "_build_notification_service", return_value=service):
            exit_code = run_alphasift_briefing.run([
                "--strategy", "dual_low",
                "--output", self._output_path("no_channel.md"),
            ])

        self.assertEqual(exit_code, 0)
        service.send.assert_not_called()


class ResolveStrategiesTests(unittest.TestCase):
    def test_cli_strategies_take_priority(self) -> None:
        args = run_alphasift_briefing._parse_args([
            "--strategies", "a,b,c",
            "--strategy", "ignored",
        ])
        self.assertEqual(run_alphasift_briefing._resolve_strategies(args), ["a", "b", "c"])

    def test_cli_single_strategy(self) -> None:
        args = run_alphasift_briefing._parse_args(["--strategy", "dual_low"])
        self.assertEqual(run_alphasift_briefing._resolve_strategies(args), ["dual_low"])

    def test_env_multi_then_single_then_default(self) -> None:
        args = run_alphasift_briefing._parse_args([])
        with patch.dict(
            "os.environ",
            {"ALPHASIFT_BRIEFING_STRATEGIES": "x,y", "ALPHASIFT_BRIEFING_STRATEGY": "z"},
            clear=False,
        ):
            self.assertEqual(run_alphasift_briefing._resolve_strategies(args), ["x", "y"])

        with patch.dict(
            "os.environ",
            {"ALPHASIFT_BRIEFING_STRATEGIES": "", "ALPHASIFT_BRIEFING_STRATEGY": "z"},
            clear=False,
        ):
            self.assertEqual(run_alphasift_briefing._resolve_strategies(args), ["z"])

        with patch.dict(
            "os.environ",
            {"ALPHASIFT_BRIEFING_STRATEGIES": "", "ALPHASIFT_BRIEFING_STRATEGY": ""},
            clear=False,
        ):
            self.assertEqual(
                run_alphasift_briefing._resolve_strategies(args),
                [run_alphasift_briefing.DEFAULT_STRATEGY],
            )


if __name__ == "__main__":  # pragma: no cover - 兼容直接执行
    unittest.main()
