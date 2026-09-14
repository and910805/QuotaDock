"""Claude Code 本機 Token 帳本的收錄與去重行為。"""
import json
from pathlib import Path

from claude_usage import ClaudeUsageCollector, claude_home
from token_usage import TokenUsageStore


def assistant_line(message_id="msg_a", request_id="req_a", session="s1",
                   model="claude-fable-5", effort="high", timestamp="2026-09-10T03:00:00Z",
                   input_tokens=2, cache_read=40, cache_write=10, output=5,
                   thinking=1, extra=None) -> str:
    record = {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": session,
        "requestId": request_id,
        "effort": effort,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": output,
                "output_tokens_details": {"thinking_tokens": thinking},
            },
        },
    }
    if extra:
        record.update(extra)
    return json.dumps(record)


def write_session(root: Path, project: str, name: str, lines: list[str]) -> Path:
    folder = root / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def collect(tmp_path: Path) -> TokenUsageStore:
    store = TokenUsageStore(tmp_path / "claude.sqlite3")
    for _ in ClaudeUsageCollector(store, tmp_path).scan():
        pass
    return store


def test_counters_follow_the_ledger_convention(tmp_path: Path) -> None:
    write_session(tmp_path, "p1", "s1", [assistant_line()])
    report = collect(tmp_path).report("all")
    group = report.groups[0]
    # 輸入包含快取讀寫；總量 = 輸入 + 輸出；推理來自 thinking_tokens。
    assert (group.model, group.effort) == ("claude-fable-5", "high")
    assert group.input_tokens == 2 + 40 + 10
    assert group.cached_input_tokens == 40
    assert group.cache_write_input_tokens == 10
    assert group.output_tokens == 5
    assert group.reasoning_output_tokens == 1
    assert group.total_tokens == 57
    assert report.responses == 1


def test_split_message_lines_count_once(tmp_path: Path) -> None:
    # 同一則訊息拆成多行（apiBlockIndex），usage 重複出現。
    write_session(tmp_path, "p1", "s1", [assistant_line(), assistant_line(), assistant_line()])
    report = collect(tmp_path).report("all")
    assert report.responses == 1
    assert report.total_tokens == 57
    assert report.issues["conflict"] == 0


def test_resume_copies_into_another_session_count_once(tmp_path: Path) -> None:
    # resume/fork 會把整段紀錄複製到新 session 檔：sessionId 變了，其餘相同。
    write_session(tmp_path, "p1", "s1", [assistant_line(session="s1")])
    write_session(tmp_path, "p1", "s2", [assistant_line(session="s2")])
    report = collect(tmp_path).report("all")
    assert report.responses == 1
    assert report.issues["conflict"] == 0


def test_streamed_snapshots_keep_the_final_usage(tmp_path: Path) -> None:
    # 子代理逐字稿會邊生成邊重複同一回應：早期行 output 較小，最後一行才是全量。
    write_session(tmp_path, "p1", "s1", [
        assistant_line(output=1, thinking=0),
        assistant_line(output=98, thinking=40),
        assistant_line(output=98, thinking=40),
    ])
    report = collect(tmp_path).report("all")
    assert report.responses == 1
    assert report.issues["conflict"] == 0
    group = report.groups[0]
    assert group.output_tokens == 98
    assert group.reasoning_output_tokens == 40
    assert group.total_tokens == 52 + 98


def test_snapshots_ignore_out_of_order_smaller_copies(tmp_path: Path) -> None:
    # resume 複本可能比既有紀錄晚被掃到；較小的舊快照不覆蓋、也不算衝突。
    write_session(tmp_path, "p1", "s1", [assistant_line(output=98)])
    write_session(tmp_path, "p1", "s2", [assistant_line(output=5, session="s2")])
    report = collect(tmp_path).report("all")
    assert report.responses == 1
    assert report.issues["conflict"] == 0
    assert report.groups[0].output_tokens == 98


def test_same_message_with_a_different_request_is_a_conflict(tmp_path: Path) -> None:
    write_session(tmp_path, "p1", "s1", [assistant_line(request_id="req_a")])
    write_session(tmp_path, "p1", "s2", [assistant_line(request_id="req_b", session="s2")])
    report = collect(tmp_path).report("all")
    assert report.responses == 0
    assert report.issues["conflict"] == 1


def test_synthetic_and_usage_free_records_are_skipped(tmp_path: Path) -> None:
    synthetic = json.loads(assistant_line(message_id="msg_x", request_id="req_x"))
    synthetic["message"]["model"] = "<synthetic>"
    no_usage = {"type": "assistant", "timestamp": "2026-09-10T03:00:00Z", "sessionId": "s1",
                "message": {"id": "msg_y", "model": "claude-fable-5"}}
    other = {"type": "user", "timestamp": "2026-09-10T03:00:00Z", "sessionId": "s1"}
    write_session(tmp_path, "p1", "s1",
                  [json.dumps(synthetic), json.dumps(no_usage), json.dumps(other), assistant_line()])
    report = collect(tmp_path).report("all")
    assert report.responses == 1
    assert report.issues["invalid_usage"] == 0
    assert report.issues["bad_lines"] == 0


def test_malformed_usage_is_reported_not_counted(tmp_path: Path) -> None:
    broken = json.loads(assistant_line(message_id="msg_bad", request_id="req_bad"))
    broken["message"]["usage"]["output_tokens"] = "many"
    write_session(tmp_path, "p1", "s1", [json.dumps(broken), assistant_line()])
    report = collect(tmp_path).report("all")
    assert report.responses == 1
    assert report.issues["invalid_usage"] == 1


def test_groups_split_by_model_and_effort(tmp_path: Path) -> None:
    write_session(tmp_path, "p1", "s1", [
        assistant_line(message_id="m1", request_id="r1", effort="high"),
        assistant_line(message_id="m2", request_id="r2", effort="xhigh"),
        assistant_line(message_id="m3", request_id="r3", model="claude-haiku-4-5", effort="high"),
    ])
    report = collect(tmp_path).report("all")
    assert {(g.model, g.effort) for g in report.groups} == {
        ("claude-fable-5", "high"), ("claude-fable-5", "xhigh"), ("claude-haiku-4-5", "high"),
    }


def test_missing_thinking_details_stay_unknown(tmp_path: Path) -> None:
    record = json.loads(assistant_line())
    del record["message"]["usage"]["output_tokens_details"]
    write_session(tmp_path, "p1", "s1", [json.dumps(record)])
    group = collect(tmp_path).report("all").groups[0]
    assert group.reasoning_output_tokens is None
    assert group.total_tokens == 57


def test_turns_use_request_identity(tmp_path: Path) -> None:
    write_session(tmp_path, "p1", "s1", [
        assistant_line(message_id="m1", request_id="r1"),
        assistant_line(message_id="m2", request_id="r1"),
        assistant_line(message_id="m3", request_id="r2"),
    ])
    store = collect(tmp_path)
    rows, count = store.turns("all", "claude-fable-5", "high")
    assert count == 2
    assert {row["responses"] for row in rows} == {1, 2}


def test_default_root_honours_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert claude_home() == tmp_path / "elsewhere"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert claude_home() == Path.home() / ".claude"
