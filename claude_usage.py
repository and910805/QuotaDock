"""Local Claude Code usage ledger. No prompts, credentials, or estimates.

Claude Code appends one JSONL file per session under <config>/projects/. An
assistant message is split across several lines that all repeat the same
message id and identical usage, and resuming a session copies old records into
the new file, so responses deduplicate by message id with copies expected.
"""
from __future__ import annotations

import os
from pathlib import Path

from token_usage import TokenUsageCollector, _text


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _counter(usage: dict, key: str):
    value = usage.get(key)
    if value is None:
        return 0
    if type(value) is not int:
        raise ValueError("invalid usage counter")
    return value


class ClaudeUsageCollector(TokenUsageCollector):
    FOLDERS = ("projects",)

    @staticmethod
    def default_root() -> Path:
        return claude_home()

    def _record(self, db, record: dict, state: dict):
        if record.get("type") != "assistant":
            return
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("invalid payload")
        usage = message.get("usage")
        model = _text(message.get("model"))
        # 合成訊息（API 錯誤替身）與沒有 usage 的紀錄不是模型回應。
        if not isinstance(usage, dict) or model == "<synthetic>":
            return
        cache_read = _counter(usage, "cache_read_input_tokens")
        cache_write = _counter(usage, "cache_creation_input_tokens")
        direct_input = _counter(usage, "input_tokens")
        output = _counter(usage, "output_tokens")
        details = usage.get("output_tokens_details")
        thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
        # 對齊 Codex 帳本的慣例：輸入包含快取；總量 = 輸入 + 輸出。
        input_tokens = direct_input + cache_read + cache_write
        payload = {
            "response_id": _text(message.get("id")),
            "thread_id": _text(record.get("sessionId")),
            "turn_id": _text(record.get("requestId")),
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cache_read,
                "cache_write_input_tokens": cache_write,
                "output_tokens": output,
                "reasoning_output_tokens": thinking,
                "total_tokens": input_tokens + output,
            },
        }
        self.store.context(db, payload["thread_id"], payload["turn_id"],
                           model, _text(record.get("effort")))
        self.store.response(db, payload, record.get("timestamp"), state,
                            copies_expected=True, snapshots=True)
