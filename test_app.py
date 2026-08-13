from pathlib import Path

from app import (
    ClaudeCliLocator,
    ClaudeUsageSnapshot,
    UsageSnapshot,
    UsageWindow,
    crossed_five_percent_level,
    detect_cycle_reset,
    duration_label,
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


def test_manual_override_wins_over_search(tmp_path: Path, monkeypatch) -> None:
    manual = tmp_path / "somewhere" / "claude.exe"
    manual.parent.mkdir()
    manual.touch()
    monkeypatch.setenv("CLAUDE_USAGE_CLI", str(manual))
    assert ClaudeCliLocator.locate() == manual


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
