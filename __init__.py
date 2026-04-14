"""
wakeup plugin — auto-inject context at session start.

On session start (I/O + cache):
  1. Read ~/.hermes/MEMORY.md (full)
  2. Read ~/.hermes/DIARY.md, extract last 3 dated sections (## YYYY-MM-DD)
  3. git pull /tmp/inbox (5s timeout), read inbox.md, extract last 3 dated
     sections with only ### subheadings + first line as summary

On first LLM turn: inject the cached payload into the user message.
Subsequent turns: no-op.

Failures in any one source are isolated — a ⚠️ line is emitted, the rest
still injects. Never raises into the agent loop.
"""

import datetime as _dt
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

HERMES_HOME = Path.home() / ".hermes"
MEMORY_PATH = HERMES_HOME / "MEMORY.md"
DIARY_PATH = HERMES_HOME / "DIARY.md"
INBOX_DIR = Path("/tmp/inbox")
INBOX_FILE = INBOX_DIR / "inbox.md"

DIARY_DAYS = 3
INBOX_DAYS = 3
GIT_TIMEOUT_SEC = 5

# Matches a line like "## 2026-04-14" (optionally with trailing text)
DATE_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b.*$", re.MULTILINE)

# Module-level cache populated by on_session_start, consumed by pre_llm_call.
_cached_payload: str | None = None


# ---------- parsing helpers ----------

def _split_by_date_headers(text: str) -> list[tuple[str, str]]:
    """
    Split markdown text into (date_str, section_body) pairs based on
    ## YYYY-MM-DD headers. The section body includes the header line itself.
    Content before the first date header is discarded.
    """
    matches = list(DATE_HEADER_RE.finditer(text))
    if not matches:
        return []
    sections = []
    for i, m in enumerate(matches):
        date_str = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((date_str, text[start:end].rstrip()))
    return sections


def _last_n_by_date(sections: list[tuple[str, str]], n: int) -> list[tuple[str, str]]:
    """Sort by ISO date string (lexicographic == chronological) and take last n."""
    return sorted(sections, key=lambda p: p[0])[-n:]


def _summarize_inbox_section(body: str) -> str:
    """
    Keep the ## date header line, then for each ### subheading keep the
    subheading line plus the first non-empty line after it.
    """
    lines = body.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            out.append(line)
        elif line.startswith("### "):
            out.append(line)
            # find first non-empty line after the subheading
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and not lines[j].startswith("#"):
                out.append(lines[j].strip())
        i += 1
    return "\n".join(out)


# ---------- readers (each isolated, returns (section_text, error_or_None)) ----------

def _read_memory() -> tuple[str, str | None]:
    try:
        text = MEMORY_PATH.read_text(encoding="utf-8").strip()
        if not text:
            return "", "MEMORY.md 为空"
        return f"# MEMORY (full)\n\n{text}", None
    except FileNotFoundError:
        return "", f"MEMORY.md 不存在 ({MEMORY_PATH})"
    except Exception as e:
        return "", f"MEMORY.md 读取失败: {e}"


def _read_diary() -> tuple[str, str | None]:
    try:
        text = DIARY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", f"DIARY.md 不存在 ({DIARY_PATH})"
    except Exception as e:
        return "", f"DIARY.md 读取失败: {e}"

    sections = _split_by_date_headers(text)
    if not sections:
        return "", "DIARY.md 没有识别到 ## YYYY-MM-DD 段落"

    # Gap check: is there a segment for today? if not, how many days since the
    # most recent entry?
    today = _dt.date.today()
    today_str = today.isoformat()
    dates_present = {d for d, _ in sections}
    gap_warning = ""
    if today_str not in dates_present:
        try:
            latest_str = max(dates_present)
            latest_date = _dt.date.fromisoformat(latest_str)
            days = (today - latest_date).days
            if days <= 0:
                # Future-dated entry exists but not today — weird, skip warning
                pass
            elif days == 1:
                gap_warning = "⚠️ 你昨天没写日记。"
            else:
                gap_warning = f"⚠️ 你已经 {days} 天没写日记了(上次是 {latest_str})。"
        except ValueError:
            # Malformed date string somehow passed the regex — skip silently
            pass

    recent = _last_n_by_date(sections, DIARY_DAYS)
    body = "\n\n".join(s for _, s in recent)
    header = f"# DIARY (last {DIARY_DAYS} days)"
    if gap_warning:
        header = f"{header}\n{gap_warning}"
    return f"{header}\n\n{body}", None


def _read_inbox() -> tuple[str, str | None]:
    # Try git pull, but don't fail the whole read if pull fails — the local
    # file may still be usable.
    pull_warning = None
    if INBOX_DIR.is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", str(INBOX_DIR), "pull", "--ff-only", "--quiet"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SEC,
            )
            if result.returncode != 0:
                pull_warning = f"git pull 非零退出: {result.stderr.strip() or result.stdout.strip()}"
        except subprocess.TimeoutExpired:
            pull_warning = f"git pull 超时 ({GIT_TIMEOUT_SEC}s)"
        except Exception as e:
            pull_warning = f"git pull 异常: {e}"
    else:
        pull_warning = f"inbox 目录不存在 ({INBOX_DIR})"

    try:
        text = INBOX_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", f"inbox.md 不存在 ({INBOX_FILE})"
    except Exception as e:
        return "", f"inbox.md 读取失败: {e}"

    sections = _split_by_date_headers(text)
    if not sections:
        return "", "inbox.md 没有识别到 ## YYYY-MM-DD 段落"
    recent = _last_n_by_date(sections, INBOX_DAYS)
    summarized = "\n\n".join(_summarize_inbox_section(s) for _, s in recent)

    header = f"# INBOX (last {INBOX_DAYS} days, titles only)"
    if pull_warning:
        header += f"\n⚠️ WAKEUP: {pull_warning} — 显示的是本地缓存版本"
    return f"{header}\n\n{summarized}", None


# ---------- hook callbacks ----------

def _build_payload() -> str:
    parts: list[str] = ["=== WAKEUP CONTEXT ==="]
    for reader in (_read_memory, _read_diary, _read_inbox):
        try:
            section, err = reader()
        except Exception as e:
            # Last-resort guard — reader should handle its own errors.
            parts.append(f"⚠️ WAKEUP: {reader.__name__} 未捕获异常: {e}")
            continue
        if err:
            parts.append(f"⚠️ WAKEUP: {err}")
        if section:
            parts.append(section)
    parts.append("=== END WAKEUP CONTEXT ===")
    return "\n\n".join(parts)


def on_session_start(**kwargs):
    """Build the wakeup payload once, cache it for the first turn."""
    global _cached_payload
    try:
        _cached_payload = _build_payload()
        logger.info("wakeup: payload built (%d chars)", len(_cached_payload))
    except Exception as e:
        # Never let this bring down session creation.
        _cached_payload = f"⚠️ WAKEUP: 构建失败: {e}"
        logger.exception("wakeup: payload build failed")


def pre_llm_call(session_id=None, user_message=None, is_first_turn=False, **kwargs):
    """Inject cached payload on the first turn only."""
    global _cached_payload
    if not is_first_turn:
        return None
    if not _cached_payload:
        return None
    payload = _cached_payload
    _cached_payload = None  # belt-and-suspenders: don't re-inject
    return {"context": payload}


# ---------- plugin entry ----------

def register(ctx):
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)
