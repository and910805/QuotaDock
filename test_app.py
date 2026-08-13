from pathlib import Path

import app as app_module
from app import (
    ClaudeCliLocator,
    ClaudeUsageSnapshot,
    UsageSnapshot,
    UsageWindow,
    crossed_five_percent_level,
    detect_cycle_reset,
    duration_label,
    mini_remaining,
    reset_time_label,
)


def snapshot(used: float, resets_at: int) -> UsageSnapshot:
    return UsageSnapshot(
        plan_type="plus",
        limit_id="codex",
        limit_name=None,
        primary=UsageWindow(used, 10080, resets_at),
        secondary=None,
        has_credits=False,
        unlimited_credits=False,
        credit_balance="0",
        reset_credits=0,
        reached_type=None,
        fetched_at=1,
    )


def test_response_parsing_uses_codex_bucket() -> None:
    value = UsageSnapshot.from_response(
        {
            "result": {
                "rateLimits": {},
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "planType": "plus",
                        "primary": {
                            "usedPercent": 52,
                            "windowDurationMins": 10080,
                            "resetsAt": 2_000_000_000,
                        },
                        "secondary": None,
                        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    }
                },
                "rateLimitResetCredits": {"availableCount": 0, "credits": []},
            }
        }
    )
    assert value.plan_type == "plus"
    assert value.primary is not None
    assert value.primary.remaining_percent == 48


def test_reset_requires_new_cycle_and_usage_drop() -> None:
    old = snapshot(96, 2_000)
    assert detect_cycle_reset(old, snapshot(3, 10_000))
    assert detect_cycle_reset(old, snapshot(8, 2_000))
    assert not detect_cycle_reset(old, snapshot(92, 2_000))
    assert not detect_cycle_reset(old, snapshot(99, 10_000))


def test_every_five_percent_alert_only_when_crossing_a_boundary() -> None:
    previous = UsageWindow(24, 300, 2_000)  # 76% remaining
    assert crossed_five_percent_level(previous, UsageWindow(25, 300, 2_000)) == 75
    assert crossed_five_percent_level(previous, UsageWindow(27, 300, 2_000)) == 75
    assert crossed_five_percent_level(
        UsageWindow(25, 300, 2_000), UsageWindow(26, 300, 2_000)
    ) is None
    assert crossed_five_percent_level(
        UsageWindow(27, 300, 2_000), UsageWindow(22, 300, 2_000)
    ) is None


def test_labels() -> None:
    assert duration_label(10080) == "7 天額度"
    assert duration_label(300) == "5 小時額度"
    assert "1 天 1 小時後重置" in reset_time_label(90_000, now=0)


def test_desktop_managed_cli_prefers_newest_version(tmp_path: Path) -> None:
    root = tmp_path / "claude-code"
    for name in ("2.1.99", "2.1.227", "2.1.222"):
        (root / name).mkdir(parents=True)
    found = ClaudeCliLocator._desktop_managed(root)
    assert [path.parent.name for path in found] == ["2.1.227", "2.1.222", "2.1.99"]
    assert found[0].name == "claude.exe"


def test_desktop_managed_cli_missing_root(tmp_path: Path) -> None:
    assert ClaudeCliLocator._desktop_managed(tmp_path / "nope") == []


def test_batch_shims_run_through_an_interpreter(tmp_path: Path) -> None:
    assert ClaudeCliLocator.command(tmp_path / "claude.exe", ["auth"]) == [
        str(tmp_path / "claude.exe"),
        "auth",
    ]
    assert ClaudeCliLocator.command(tmp_path / "claude.cmd", ["auth"])[:2] == ["cmd.exe", "/c"]
    assert ClaudeCliLocator.command(tmp_path / "claude.ps1", ["auth"])[0] == "powershell.exe"


def claude_snapshot(five_hour: float, seven_day: float) -> ClaudeUsageSnapshot:
    return ClaudeUsageSnapshot.from_response(
        {
            "subscription_type": "max",
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": five_hour, "resets_at": None},
                "seven_day": {"utilization": seven_day, "resets_at": None},
            },
        },
        auth_method="claude.ai",
        logged_in=True,
    )


def test_mini_source_selects_the_right_provider() -> None:
    codex = snapshot(60, 2_000)  # 剩 40%
    claude = claude_snapshot(10, 90)  # 剩 90% / 10%
    assert mini_remaining("codex", codex, claude) == 40
    assert mini_remaining("claude", codex, claude) == 10
    assert mini_remaining("min", codex, claude) == 10


def test_mini_source_falls_back_when_provider_has_no_data() -> None:
    codex = snapshot(60, 2_000)
    empty_claude = ClaudeUsageSnapshot.unavailable(installed=True)
    assert mini_remaining("claude", codex, empty_claude) == 40
    assert mini_remaining("claude", codex, None) == 40
    assert mini_remaining("codex", None, claude_snapshot(20, 50)) == 50
    assert mini_remaining("min", None, None) == 0.0


def make_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_scan_skips_the_desktop_gui_and_picks_the_cli(tmp_path: Path) -> None:
    make_exe(tmp_path / "AnthropicClaude" / "claude.exe")
    cli = make_exe(tmp_path / "Claude" / "claude-code" / "2.1.5" / "claude.exe")
    assert ClaudeCliLocator._scan([tmp_path]) == cli


def test_scan_prefers_newest_version_and_native_exe(tmp_path: Path) -> None:
    make_exe(tmp_path / "claude-code" / "2.1.9" / "claude.cmd")
    make_exe(tmp_path / "claude-code" / "2.1.30" / "claude.exe")
    newest = make_exe(tmp_path / "claude-code" / "2.1.100" / "claude.exe")
    assert ClaudeCliLocator._scan([tmp_path]) == newest


def test_scan_respects_depth_limit(tmp_path: Path) -> None:
    deep = make_exe(tmp_path / "a" / "b" / "c" / "d" / "e" / "claude.exe")
    assert ClaudeCliLocator._scan([tmp_path], max_depth=3) is None
    assert ClaudeCliLocator._scan([tmp_path], max_depth=9) == deep


def test_scan_returns_none_when_nothing_matches(tmp_path: Path) -> None:
    make_exe(tmp_path / "other" / "codex.exe")
    assert ClaudeCliLocator._scan([tmp_path]) is None
    assert ClaudeCliLocator._scan([tmp_path / "missing"]) is None


def test_manual_override_wins_over_search(tmp_path: Path, monkeypatch) -> None:
    manual = tmp_path / "somewhere" / "claude.exe"
    manual.parent.mkdir()
    manual.touch()
    monkeypatch.setenv("CLAUDE_USAGE_CLI", str(manual))
    assert ClaudeCliLocator.locate() == manual


def isolate_lookup(monkeypatch) -> None:
    """把查找隔離在測試裡：不讀本機設定、不掃真實磁碟。"""
    monkeypatch.setattr(app_module, "_setting_str", lambda settings, key: "")
    monkeypatch.setattr(app_module.shutil, "which", lambda command: None)
    monkeypatch.setattr(ClaudeCliLocator, "_desktop_managed", staticmethod(lambda root: []))
    monkeypatch.setattr(ClaudeCliLocator, "_scan", staticmethod(lambda roots: None))


def test_packaged_desktop_cli_is_found_in_localcache(tmp_path: Path) -> None:
    """MSIX 桌面版的真身在 Packages\\<family>\\LocalCache\\Roaming 底下。"""
    base = tmp_path / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    make_exe(base / "2.1.9" / "claude.exe")
    newest = make_exe(base / "2.1.30" / "claude.exe")
    make_exe(tmp_path / "OtherApp_abc" / "LocalCache" / "Roaming" / "Claude" / "claude-code" / "9.9.9" / "claude.exe")

    found = ClaudeCliLocator._packaged_managed(tmp_path)
    assert found[0] == newest
    assert all("OtherApp_abc" not in str(path) for path in found)
    assert ClaudeCliLocator._packaged_managed(tmp_path / "missing") == []


def test_msix_projection_is_told_apart_from_the_real_file() -> None:
    projection = Path(r"C:\Users\x\AppData\Roaming\Claude\claude-code\2.1.227\claude.exe")
    real = Path(
        r"C:\Users\x\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache"
        r"\Roaming\Claude\claude-code\2.1.227\claude.exe"
    )
    assert ClaudeCliLocator._is_msix_projection(projection) is True
    assert ClaudeCliLocator._is_msix_projection(real) is False
    assert ClaudeCliLocator._is_msix_projection(Path(r"C:\tools\claude.exe")) is False


def test_unreadable_candidate_is_reported_as_inconclusive(tmp_path: Path, monkeypatch) -> None:
    """防毒或檔案鎖讓 stat 失敗時要說「問不出來」，不能報成未安裝。"""
    isolate_lookup(monkeypatch)
    manual = make_exe(tmp_path / "locked" / "claude.exe")
    monkeypatch.setenv("CLAUDE_USAGE_CLI", str(manual))
    monkeypatch.setattr(
        app_module.os, "stat", lambda path, *a, **kw: (_ for _ in ()).throw(PermissionError(5, "存取被拒"))
    )
    assert ClaudeCliLocator.locate_detailed() == (None, True)


def test_missing_candidate_is_still_reported_as_not_installed(tmp_path: Path, monkeypatch) -> None:
    isolate_lookup(monkeypatch)
    monkeypatch.setenv("CLAUDE_USAGE_CLI", str(tmp_path / "nope" / "claude.exe"))
    assert ClaudeCliLocator.locate_detailed() == (None, False)


def test_claude_usage_response_parsing() -> None:
    value = ClaudeUsageSnapshot.from_response(
        {
            "subscription_type": "max",
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": 34, "resets_at": "2033-05-18T06:33:20Z"},
                "seven_day": {"utilization": 61, "resets_at": "2033-05-20T06:33:20Z"},
            },
        },
        auth_method="claude.ai",
        logged_in=True,
    )
    assert value.subscription_type == "max"
    assert value.five_hour is not None
    assert value.five_hour.remaining_percent == 66
    assert value.seven_day is not None
    assert value.seven_day.remaining_percent == 39
