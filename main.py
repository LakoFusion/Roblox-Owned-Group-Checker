import asyncio
import contextlib
import io
import json
import random
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import aiohttp
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import IntPrompt
from rich.text import Text

BASE_DIR = Path(__file__).resolve().parent

# Bảng màu gradient dùng chung cho log.
GRAD_TITLE = ("#00E5FF", "#B14DFF")  # banner / rule
GRAD_MATCH = ("#00E5FF", "#39FF14")  # group đạt filter
GRAD_OK = ("#39FF14", "#00E5FF")  # thành công / reconciliation ok
GRAD_WARN = ("#FFD700", "#FF4D4D")  # cảnh báo / thất bại

FILTER_ICONS = {
    "robux": "💰",
    "pending": "⏳",
    "games": "🎮",
    "visits": "👁️",
    "clothing": "👕",
}

TOOL_VERSION = "v2.5.0"

# Bảng màu ANSI cho terminal UI mode "dashboard" (Clean Minimalist Typography).
DASH_RESET = "\033[0m"
DASH_BOLD = "\033[1;97m"   # tiêu đề & giá trị chính
DASH_CYAN = "\033[1;96m"   # label / tên trường
DASH_GREEN = "\033[1;92m"  # thành công / đang bật
DASH_YELLOW = "\033[1;93m"  # cảnh báo / đang xử lý
DASH_RED = "\033[1;91m"    # lỗi / đang tắt
DASH_DIM = "\033[90m"      # text phụ / phân cách / log cũ


class DashboardUI:
    """Dashboard terminal 4 khối dọc — Clean Minimalist Typography.

    Nguyên tắc bất di bất dịch:
    - Không ký tự vẽ khung Unicode (chỉ khoảng trắng + text cơ bản).
    - Không nhồi nhét: mỗi thông tin 1 dòng, metrics tối đa 2 cột padding rộng.
    - Chống giật: gom TOÀN BỘ nội dung thành 1 buffer rồi sys.stdout.write
      ("\\033[H\\033[J" + buffer) + flush — không clear màn hình rời rạc.
    - An toàn: mọi truy cập dữ liệu qua getattr/.get kèm fallback, render()
      bọc try/except — không bao giờ làm sập vòng lặp quét.
    """

    def __init__(self) -> None:
        self.status = "RUNNING"  # RUNNING | RE-CHECKING | STOPPED
        self.account = "N/A"
        self.user_id = 0
        self.account_index = 0
        self.total_accounts = 0
        self.groups_total = 0
        self.groups_done = 0
        self.filters_line = "N/A"
        self.tor_label = "N/A"
        self.logs: List[str] = []

    def log(self, message: str) -> None:
        """Thêm 1 dòng activity mới (giữ 5 dòng gần nhất, cắt message <=90 ký tự)."""
        try:
            clean_msg = str(message).strip()
            if len(clean_msg) > 90:
                clean_msg = clean_msg[:87] + "..."
            line = f"{datetime.now().strftime('%H:%M:%S')}  {clean_msg}"
            self.logs.append(line)
            if len(self.logs) > 5:
                del self.logs[0]
        except Exception:
            pass

    def begin_account(
        self, name: str, user_id: int, index: int, total: int, owned: int
    ) -> None:
        """Chuyển trọng tâm nghiệp vụ sang account mới (mọi field đều fallback)."""
        try:
            self.account = str(name or "N/A")
            self.user_id = int(user_id or 0)
            self.account_index = int(index or 0)
            self.total_accounts = int(total or 0)
            self.groups_total = int(owned or 0)
            self.groups_done = 0
        except Exception:
            pass

    def render(self, stats: Any) -> None:
        """Render toàn bộ dashboard thành 1 buffer duy nhất — không bao giờ raise."""
        try:

            def num(key: str) -> int:
                try:
                    return int(getattr(stats, key, 0) or 0)
                except Exception:
                    return 0

            def col(label: str, value: int, color: str = DASH_BOLD) -> str:
                return f"{DASH_CYAN}{label:<15}{DASH_RESET} {color}{value:>10,}{DASH_RESET}"

            if self.status == "RE-CHECKING":
                status_text = f"{DASH_YELLOW}RE-CHECKING{DASH_RESET}"
                queue = "re-check"
            elif self.status == "STOPPED":
                status_text = f"{DASH_RED}STOPPED{DASH_RESET}"
                queue = f"{self.groups_done}/{self.groups_total}"
            else:
                status_text = f"{DASH_GREEN}RUNNING{DASH_RESET}"
                queue = f"{self.groups_done}/{self.groups_total}"

            owned = num("owned_groups")
            matched = num("matched_filters")
            skipped = num("skipped_filters")
            failed = num("checked_failed")
            partial = num("partial_checks")
            webhooks = num("webhooks_sent")
            robux = num("total_robux")
            pending = num("total_pending")
            sales = num("total_sales")
            premium = num("total_premium")
            fake_owner = num("fake_owner_groups")

            lines: List[str] = []

            # --- KHỐI 1: HEADER & SYSTEM STATUS -----------------------------
            lines.append(
                f"{DASH_BOLD}ROBLOX GROUP OWNER SCANNER{DASH_RESET}  "
                f"{DASH_DIM}{TOOL_VERSION}{DASH_RESET}"
            )
            lines.append(f"{DASH_CYAN}Status      :{DASH_RESET} {status_text}")
            lines.append("")

            # --- KHỐI 2: CORE IDENTITY (trọng tâm nghiệp vụ) ----------------
            lines.append(
                f"{DASH_CYAN}Target Acc  :{DASH_RESET} {DASH_BOLD}{self.account}{DASH_RESET}"
                f"{DASH_DIM} (ID: {self.user_id}){DASH_RESET}"
            )
            lines.append(
                f"{DASH_CYAN}Account Turn:{DASH_RESET} {DASH_BOLD}"
                f"{self.account_index}/{self.total_accounts}{DASH_RESET}"
            )
            lines.append(
                f"{DASH_CYAN}Group Queue :{DASH_RESET} {DASH_BOLD}{queue}{DASH_RESET}"
            )
            lines.append(
                f"{DASH_CYAN}Tor Network :{DASH_RESET} {self.tor_label}"
            )
            lines.append(
                f"{DASH_CYAN}Filters     :{DASH_RESET} {DASH_DIM}{self.filters_line}{DASH_RESET}"
            )
            lines.append("")

            # --- KHỐI 3: LIVE METRICS (2 cột, padding rộng) -----------------
            lines.append(f"{DASH_DIM}-- METRICS ---------------------------------------------{DASH_RESET}")
            gap = " " * 8
            lines.append(
                "  " + col("Owner Groups", owned) + gap
                + col("Total Robux", robux, DASH_GREEN)
            )
            lines.append(
                "  " + col("Filter Match", matched, DASH_GREEN) + gap
                + col("Pending Robux", pending, DASH_YELLOW)
            )
            lines.append(
                "  " + col("Skipped", skipped, DASH_DIM) + gap
                + col("Item Sales", sales)
            )
            lines.append(
                "  " + col("Checks Fail", failed, DASH_RED if failed > 0 else DASH_DIM) + gap
                + col("Premium Pay", premium)
            )
            lines.append(
                "  " + col("Webhook Sent", webhooks, DASH_GREEN) + gap
                + col("Partial Data", partial, DASH_YELLOW if partial > 0 else DASH_DIM)
            )
            if fake_owner > 0:
                lines.append(
                    "  " + col("Lost/Fake Owner", fake_owner, DASH_RED)
                )
            lines.append("")

            # --- KHỐI 4: RECENT ACTIVITY (timeline 5 log mới nhất) ----------
            lines.append(f"{DASH_DIM}-- ACTIVITY --------------------------------------------{DASH_RESET}")
            if not self.logs:
                lines.append(f"{DASH_DIM}  (waiting for first task...){DASH_RESET}")
            else:
                for entry in self.logs:
                    lines.append(f"{DASH_DIM}>{DASH_RESET} {entry}")
            lines.append("")
            footer_hint = (
                "[Scan completed]" if self.status == "STOPPED" else "[Ctrl+C to stop]"
            )
            lines.append(f"{DASH_DIM}{footer_hint}{DASH_RESET}")

            # --- ATOMIC WRITE (chống giật: 1 buffer, 1 lần ghi) -------------
            sys.stdout.write("\033[H\033[J" + "\n".join(lines) + "\n")
            sys.stdout.flush()
        except Exception:
            # Fallback tuyệt đối: UI không bao giờ làm crash engine quét.
            pass

# Các status mà group được tính là "đạt filter".
MATCHED_STATUSES = frozenset({"SENT", "SENT_PARTIAL", "WEBHOOK_FAIL"})

# Các status vẫn coi là "chưa xong" sau re-check — cần gom vào report.
UNRESOLVED_STATUSES = frozenset({"PARTIAL", "SENT_PARTIAL", "FAIL", "WEBHOOK_FAIL"})


@dataclass
class ScanStats:
    cookies_loaded: int = 0
    valid_accounts: int = 0
    invalid_cookies: int = 0
    account_failures: int = 0
    owned_groups: int = 0
    checked_success: int = 0
    checked_failed: int = 0
    matched_filters: int = 0
    skipped_filters: int = 0
    partial_checks: int = 0
    webhooks_sent: int = 0
    webhooks_failed: int = 0
    rechecked_groups: int = 0
    recheck_recovered: int = 0
    accounts_recovered: int = 0
    total_robux: int = 0
    total_pending: int = 0
    total_sales: int = 0
    total_premium: int = 0
    total_games: int = 0
    total_visits: int = 0
    total_clothing: int = 0
    total_members: int = 0
    fake_owner_groups: int = 0
    failed_accounts: List[str] = field(default_factory=list)


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def gradient_text(text: str, start: str, end: str, base_style: str = "") -> Text:
    """Gradient text nội suy màu từng ký tự (rich 15 không có Text.gradient).

    Chỉ nhận chuỗi thuần — tên group/username đưa vào đây sẽ không bị rich
    diễn giải thành markup, tránh crash khi tên chứa '['.
    """
    rendered = Text(style=base_style)
    if not text:
        return rendered

    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    steps = max(len(text) - 1, 1)

    for index, char in enumerate(text):
        ratio = index / steps
        r = round(r1 + (r2 - r1) * ratio)
        g = round(g1 + (g2 - g1) * ratio)
        b = round(b1 + (b2 - b1) * ratio)
        rendered.append(char, style=f"#{r:02x}{g:02x}{b:02x}")
    return rendered


def format_compact(value: int) -> str:
    """1_234_567 -> '1.2M', 12_345 -> '12.3k', nhỏ hơn giữ nguyên dấu phẩy."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,}"


def matched_display_parts(
    group_info: Dict[str, Any], thresholds: Dict[str, int]
) -> List[str]:
    """Chỉ số hiển thị cạnh link group — chỉ nêu điều kiện ĐẠT filter.

    filter > 0: hiện khi value >= ngưỡng; filter = 0: hiện khi value > 0.
    Rỗng -> ['0 Robux'].
    """
    parts: List[str] = []
    for key, label in (
        ("robux", "Robux"),
        ("pending", "Pending"),
        ("games", "Games"),
        ("visits", "Vis"),
        ("clothing", "Clothing"),
    ):
        value = int(group_info.get(key, 0) or 0)
        threshold = int(thresholds.get(key, 0) or 0)
        if threshold > 0:
            if value < threshold:
                continue
        elif value <= 0:
            continue
        if key == "visits":
            parts.append(f"{label} {format_compact(value)}")
        else:
            parts.append(f"{value:,} {label}")
    return parts or ["0 Robux"]


class DomainPacer:
    """Giãn nhịp request theo domain — cooldown ổn định thay cho xoay IP.

    Mỗi domain có khoảng nghỉ tối thiểu giữa 2 request; gặp 429 thì nhân đôi
    interval (tối đa MAX_INTERVAL) và giãn dần về mức gốc sau chuỗi thành công.
    """

    DEFAULT_INTERVALS = {
        "catalog.roblox.com": 1.2,
        "economy.roblox.com": 0.9,
        "games.roblox.com": 0.6,
        "groups.roblox.com": 0.5,
        "users.roblox.com": 0.5,
        "auth.roblox.com": 0.5,
        "thumbnails.roblox.com": 0.4,
    }
    MAX_INTERVAL = 8.0
    PENALTY_FACTOR = 2.0
    RECOVERY_STREAK = 15

    def __init__(
        self,
        intervals: Optional[Dict[str, float]] = None,
        default_interval: float = 0.5,
    ):
        merged = dict(self.DEFAULT_INTERVALS)
        if intervals:
            merged.update(intervals)
        self.base: Dict[str, float] = merged
        self.default_interval = float(default_interval)
        self.current: Dict[str, float] = dict(merged)
        self._next_allowed: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._streak: Dict[str, int] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    def interval(self, domain: str) -> float:
        return self.current.get(domain, self.default_interval)

    async def wait(self, domain: str) -> None:
        """Chặn tới khi được phép gửi request tiếp theo của domain (lock từng
        domain để nhiều task đồng thời không vượt interval)."""
        async with self._lock(domain):
            loop = asyncio.get_running_loop()
            now = loop.time()
            allowed_at = self._next_allowed.get(domain, now)
            if allowed_at > now:
                await asyncio.sleep(allowed_at - now)
                now = loop.time()
            self._next_allowed[domain] = now + self.interval(domain)

    def record(self, domain: str, status: int) -> None:
        """429 → tăng interval; chuỗi RECOVERY_STREAK thành công → giảm về gốc."""
        interval = self.interval(domain)
        if status == 429:
            self.current[domain] = min(
                interval * self.PENALTY_FACTOR, self.MAX_INTERVAL
            )
            self._streak[domain] = 0
            return

        streak = self._streak.get(domain, 0) + 1
        self._streak[domain] = streak
        base = self.base.get(domain, self.default_interval)
        if streak >= self.RECOVERY_STREAK and interval > base:
            self.current[domain] = max(
                interval / self.PENALTY_FACTOR, base
            )
            self._streak[domain] = 0


class AccountStatus:
    """Embed trạng thái Discord cho MỘT account (CMD v2 — Clean CLI Terminal).

    Tạo đúng 1 message bằng POST ?wait=true, sau đó PATCH /messages/{id}
    mỗi khi có group hoàn tất hoặc group đạt filter, và 1 lần cuối khi
    account xong. Các lần PATCH được gộp (coalesce): nếu đã có lần gửi
    đang chạy thì chỉ đánh dấu pending — lần gửi đọc lại trạng thái mới
    nhất → không spam Discord (~5 req/5s) và không làm chậm quét.
    """

    COLOR_SCANNING = 0x00E5FF  # cyan — đang quét
    COLOR_RECHECKING = 0xF0B232  # amber — lượt re-check group thiếu dữ liệu
    COLOR_DONE = 0x00FF88  # green — có group đạt filter
    COLOR_EMPTY = 0x80848E  # grey — xong, không group nào đạt
    MAX_LISTED = 15  # dòng ngắn (link + chỉ số matched) nên liệt kê được nhiều hơn

    def __init__(
        self,
        scanner: "GroupScanner",
        index: int,
        total: int,
        username: str,
        user_id: int,
        owned_groups: int,
        avatar_url: Optional[str] = None,
    ):
        self.scanner = scanner
        self.index = index
        self.total = total
        self.username = username
        self.user_id = user_id
        self.owned_groups = owned_groups
        self.avatar_url = avatar_url
        self.message_id: Optional[str] = None
        self.checked = 0
        self.matched_groups: List[Dict[str, Any]] = []
        self.finalized = False
        # Đang ở pha re-check (group PARTIAL/FAIL chờ quét lại) — embed KHÔNG
        # được báo FINISHED cho tới khi run_recheck_pass chốt bằng finish_recheck.
        self.rechecking = False
        self.totals = {
            "robux": 0,
            "pending": 0,
            "sales": 0,
            "premium_payouts": 0,
            "games": 0,
            "visits": 0,
            "clothing": 0,
            "members": 0,
        }
        self._sending = False
        self._pending = False

    @property
    def active(self) -> bool:
        return self.message_id is not None

    def _url(self) -> str:
        return f"{self.scanner.webhook_url}/messages/{self.message_id}"

    @staticmethod
    def _bar(done: int, total: int, width: int = 10) -> str:
        if total <= 0:
            return "[" + "■" * width + "]"
        filled = min(width, max(0, round(done * width / total)))
        return "[" + "■" * filled + "□" * (width - filled) + "]"

    def _matched_block(self) -> str:
        if not self.matched_groups:
            return "—"
        thresholds = dict(self.scanner.filters)
        lines: List[str] = []
        shown = 0
        total_matched = len(self.matched_groups)
        # Ưu tiên group nhiều Robux + Pending lên đầu để không bị cắt vào "+N group khác…".
        sorted_groups = sorted(
            self.matched_groups,
            key=lambda g: (
                int(g.get("robux", 0) or 0) + int(g.get("pending", 0) or 0),
                int(g.get("robux", 0) or 0),
                int(g.get("pending", 0) or 0),
                int(g.get("visits", 0) or 0),
                int(g.get("clothing", 0) or 0),
                int(g.get("games", 0) or 0),
            ),
            reverse=True,
        )
        # Chừa chỗ cho dòng "+N group khác…" để field value không vượt 1024.
        max_chars = 980
        for info in sorted_groups[: self.MAX_LISTED]:
            gid = int(info.get("group_id", 0) or 0)
            owner_tag = "" if info.get("is_real_owner", True) else " [⚠ MẤT QUYỀN]"
            parts = " · ".join(matched_display_parts(info, thresholds))
            line = f"• [{gid}](https://www.roblox.com/groups/{gid}){owner_tag} — {parts}"

            # Kiểm tra kèm dòng bù "+N group khác…" (nếu group sau bị cắt).
            rest = total_matched - (shown + 1)
            suffix = len(f"\n+{rest} group khác…") if rest > 0 else 0
            if lines and len("\n".join(lines + [line])) + suffix > max_chars:
                break
            lines.append(line)
            shown += 1

        more = total_matched - shown
        if more > 0:
            lines.append(f"+{more} group khác…")
        return "\n".join(lines)

    def _description(self) -> str:
        if self.rechecking:
            status_line = "status  : RE-CHECKING"
        elif self.finalized:
            status_line = "status  : FINISHED"
        else:
            body = (
                f"status  : SCANNING\n"
                f"checked : {self.checked}/{self.owned_groups}\n"
                f"bar     : {self._bar(self.checked, self.owned_groups)}"
            )
            return f"```yaml\n{body}\n```"

        t = self.totals
        body_lines = [
            status_line,
            f"checked : {self.checked}/{self.owned_groups}",
            f"robux   : {t['robux']:,}",
            f"pending : {t['pending']:,}",
        ]
        body = "\n".join(body_lines)
        return f"```yaml\n{body}\n```"

    def _payload(self) -> Dict[str, Any]:
        matched_count = len(self.matched_groups)
        if self.rechecking:
            icon, color = "🔄", self.COLOR_RECHECKING
        elif self.finalized:
            if matched_count:
                icon, color = "✅", self.COLOR_DONE
            else:
                icon, color = "➖", self.COLOR_EMPTY
        else:
            icon, color = "⚡", self.COLOR_SCANNING

        if matched_count:
            mention = f"<@{self.scanner.discord_id}> " if self.scanner.discord_id else ""
            content = f"{mention}👑 {matched_count} group đạt filter · {self.username}"
        else:
            content = ""

        embed = {
            "title": f"{icon} [ACC {self.index}/{self.total}] {self.username}",
            "description": self._description(),
            "url": f"https://www.roblox.com/users/{self.user_id}/profile",
            "color": color,
            "fields": [
                {
                    "name": f"👑 Matched Groups ({matched_count})",
                    "value": self._matched_block(),
                    "inline": False,
                }
            ],
            "footer": {"text": TOOL_VERSION},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self.avatar_url:
            embed["thumbnail"] = {"url": self.avatar_url}
        return {"username": "Group Scanner Bot", "content": content, "embeds": [embed]}

    async def open(self, final: bool = False) -> bool:
        """Tạo message trạng thái (POST ?wait=true) — trả False nếu webhook lỗi."""
        self.finalized = final
        data, error = await self.scanner._webhook_api(
            "POST", f"{self.scanner.webhook_url}?wait=true", payload=self._payload()
        )
        message_id = data.get("id") if isinstance(data, dict) else None
        if not message_id:
            self.scanner._ui_event(
                f"Không tạo được status embed cho {self.username}: "
                f"{error or 'response thiếu id'} — fallback webhook từng group.",
                f"[yellow]Không tạo được status embed cho[/] [bold]{escape(self.username)}[/]"
                f"[yellow]: {escape(error or 'response thiếu id')} — "
                "fallback webhook từng group.[/]",
            )
            return False
        self.message_id = str(message_id)
        return True

    def add_match(self, info: Dict[str, Any]) -> None:
        """Thêm group đạt filter (dedupe theo group_id — recheck không trùng)."""
        gid = info.get("group_id")
        self.matched_groups = [
            g for g in self.matched_groups if g.get("group_id") != gid
        ]
        self.matched_groups.append(info)

    async def record(
        self,
        result: Dict[str, Any],
        *,
        recheck: bool = False,
        prev_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Ghi 1 kết quả group vào status rồi PATCH (gộp) nếu cần."""
        if not self.active:
            return
        info = result.get("group_info") or {}

        if recheck and prev_result is not None:
            # Re-check: hoàn tác số liệu cũ rồi cộng số mới (không đếm đôi).
            prev_info = prev_result.get("group_info") or {}
            for key in self.totals:
                self.totals[key] -= int(prev_info.get(key, 0) or 0)
                self.totals[key] += int(info.get(key, 0) or 0)

        if result.get("status") in MATCHED_STATUSES and info.get("group_id") is not None:
            self.add_match(info)

        if not recheck:
            self.checked = min(self.checked + 1, self.owned_groups)

        await self._notify()

    async def _notify(self) -> None:
        """PATCH trạng thái MỚI NHẤT — gộp các lần cập nhật đang chạy."""
        if not self.active:
            return
        if self._sending:
            self._pending = True
            return
        self._sending = True
        try:
            first = True
            while first or self._pending:
                first = False
                self._pending = False
                _, error = await self.scanner._webhook_api(
                    "PATCH", self._url(), payload=self._payload()
                )
                if error:
                    self.scanner._ui_event(
                        f"Cập nhật status embed {self.username} lỗi: {error}",
                        f"[dim]⚠ Cập nhật status embed {escape(self.username)} lỗi: "
                        f"{escape(error)}[/]",
                    )
        finally:
            self._sending = False

    def _recompute_from_results(self, results: List[Dict[str, Any]]) -> None:
        """Tính lại totals + matched + checked từ toàn bộ results của account."""
        totals = {key: 0 for key in self.totals}
        matched: List[Dict[str, Any]] = []
        seen: set = set()
        for result in results:
            info = result.get("group_info") or {}
            for key in totals:
                totals[key] += int(info.get(key, 0) or 0)
            if (
                result.get("status") in MATCHED_STATUSES
                and info.get("group_id") is not None
            ):
                gid = info.get("group_id")
                if gid not in seen:
                    seen.add(gid)
                    matched.append(info)

        self.totals = totals
        self.matched_groups = matched
        self.checked = len(results)

    async def finalize(self, results: List[Dict[str, Any]]) -> None:
        """Bản cuối: totals + matched lấy từ toàn bộ results của account."""
        if not self.active:
            return
        self._recompute_from_results(results)
        self.finalized = True
        self.rechecking = False

        # Chờ PATCH đang bay xong rồi gửi bản cuối (không ghi đè lẫn nhau).
        while self._sending:
            await asyncio.sleep(0.05)
        await self._notify()

    async def mark_rechecking(self, results: List[Dict[str, Any]]) -> None:
        """Đánh dấu account đang chờ re-check group thiếu dữ liệu.

        Totals/matched tính trước (giống finalize) để lượt re-check sau đó
        trừ/phụ delta trên đúng baseline — nhưng embed vẫn hiển thị
        RE-CHECKING thay vì báo FINISHED sớm.
        """
        if not self.active:
            return
        self._recompute_from_results(results)
        self.rechecking = True
        self.finalized = False

        while self._sending:
            await asyncio.sleep(0.05)
        await self._notify()

    async def finish_recheck(self) -> None:
        """Chốt trạng thái FINISHED sau khi run_recheck_pass đã xử lý xong."""
        if not self.active or not self.rechecking:
            return
        self.rechecking = False
        self.finalized = True

        while self._sending:
            await asyncio.sleep(0.05)
        await self._notify()


class GroupScanner:
    def __init__(
        self,
        webhook_url: str,
        discord_id: Optional[str] = None,
        tor_settings: Optional[Dict[str, Any]] = None,
        cooldowns: Optional[Dict[str, float]] = None,
        cooldown_default: float = 0.5,
        status_embed: bool = True,
        terminal_ui: str = "dashboard",
    ):
        self.webhook_url = webhook_url
        self.discord_id = discord_id
        self.tor_settings = tor_settings or {}
        self.cookies: List[str] = []
        self.filters = {
            "robux": 0,
            "pending": 0,
            "games": 0,
            "visits": 0,
            "clothing": 0,
        }

        # Cooldown theo domain — bộ điều tốc thật của toàn bộ request.
        self.pacer = DomainPacer(intervals=cooldowns, default_interval=cooldown_default)

        # Pool proxy Tor (HTTP tunnel) + thống kê phân bổ round-robin.
        self.proxies: List[str] = []
        self._proxy_index = 0
        self.proxy_usage: Dict[str, int] = {}
        self._tor_module = None
        # Group PARTIAL/FAIL (thường do 429) chờ lượt re-check cuối scan.
        self.recheck_queue: List[Dict[str, Any]] = []
        # Cookie lỗi mạng (429/timeout lúc auth) chờ lượt re-check cuối scan.
        self.failed_cookies_queue: List[Dict[str, Any]] = []
        # Report cuối phiên: matched (đạt filter) + unresolved (PARTIAL/
        # SENT_PARTIAL/FAIL/WEBHOOK_FAIL sau re-check) để đóng ZIP đính kèm.
        self.all_matched_groups: List[Dict[str, Any]] = []
        self.unresolved_groups: List[Dict[str, Any]] = []
        self._last_report_zip: Optional[bytes] = None

        # Status embed 1 message/account (CMD v2) — tắt qua config "status_embed".
        self.status_embeds_enabled = status_embed
        self._statuses: Dict[str, AccountStatus] = {}
        # Avatar headshot theo username — fallback thumbnail cho webhook từng group.
        self._avatars: Dict[str, Optional[str]] = {}
        # Throttle chung cho mọi call webhook Discord (~5 req/5s mỗi webhook).
        self.webhook_lock = asyncio.Lock()
        self.webhook_next = 0.0
        self.webhook_min_interval = 1.2

        # Terminal UI: "dashboard" (Clean Minimalist, mặc định) | "classic" (Rich log).
        self.terminal_ui = (
            "classic" if str(terminal_ui).lower() == "classic" else "dashboard"
        )
        self.dash: Optional[DashboardUI] = (
            DashboardUI() if self.terminal_ui == "dashboard" else None
        )

        self.console = Console()
        self.stats = ScanStats()
        self.max_retries = 5
        self.retry_base_delay = 1.25
        self.retry_max_delay = 18
        self.concurrent_groups = 3
        # Timeout tổng cho 1 group (tránh 1 group pagination lớn giữ Semaphore).
        self.group_timeout = 45.0
        # Nghỉ giữa các vòng re-check (rate-limit nguội) — test hạ về 0.
        self.recheck_cooldown = 45
        # Qua Tor có độ trễ cao hơn → nới timeout request.
        tor_enabled = bool(self.tor_settings.get("enabled"))
        self.request_timeout = aiohttp.ClientTimeout(total=60 if tor_enabled else 30)

    def _isolated_proxy(self, key: str) -> Optional[str]:
        """Round-robin tunnel + credential duy nhất theo key.

        Tor HTTPTunnelPort có IsolateSOCKSAuth: mỗi giá trị
        Proxy-Authorization khác nhau = circuit riêng = exit IP riêng.
        Để tránh build circuit ồ ạt, chỉ dùng key theo account/vòng
        re-check (không sinh key cho từng sub-request nhỏ).
        key ví dụ: 'acc3' (1 circuit cho cả account), 'grp12345' (group),
        'grp12345r1' (lượt re-check vòng 1 — exit mới hoàn toàn).
        """
        if not self.proxies:
            return None
        proxy = self.proxies[self._proxy_index % len(self.proxies)]
        self._proxy_index += 1
        scheme, _, host = proxy.partition("://")
        return f"{scheme}://{key}:x@{host}"

    def _proxy_label(self, proxy: Optional[str]) -> str:
        """'http://127.0.0.1:9051' -> ':9051'; None -> chuỗi rỗng."""
        if not proxy:
            return ""
        return f":{proxy.rsplit(':', 1)[-1]}"

    def _announce_tor(self) -> None:
        """Báo trạng thái Tor TRƯỚC khi nhập filter — Tor chỉ khởi tạo sau filter
        để user không phải chờ bootstrap trong lúc prompt đang chờ."""
        if self.tor_settings.get("enabled"):
            amount = int(self.tor_settings.get("amount", 3))
            port = int(self.tor_settings.get("port", 9050))
            self.console.print(
                gradient_text(
                    f"TOR BẬT · {amount} proxies · port gốc {port}"
                    " — token LUÔN đi IP gốc; Tor chỉ mang dữ liệu công khai",
                    *GRAD_MATCH,
                )
            )
        else:
            self.console.print(
                "[dim]Tor: TẮT — mọi request đi IP gốc với cooldown theo domain.[/]"
            )

    def _setup_tor(self) -> None:
        """Khởi tạo Tor proxies nếu config bật; fail thì chạy direct với cảnh báo."""
        if not self.tor_settings.get("enabled"):
            return

        amount = int(self.tor_settings.get("amount", 3))
        start_port = int(self.tor_settings.get("port", 9050))
        try:
            from tor import tor_manager  # lazy: chỉ cần psutil khi Tor bật

            self._tor_module = tor_manager
            proxies = tor_manager.Make_Proxies(amount, start_port)
            self.proxies = proxies
            first_port = proxies[0].rsplit(":", 1)[-1]
            last_port = proxies[-1].rsplit(":", 1)[-1]
            self.console.print(
                gradient_text(
                    f"TOR · {len(proxies)} tunnels · 127.0.0.1:{first_port}-{last_port}"
                    " · CHỈ dữ liệu công khai (không cookie) · token luôn đi IP gốc",
                    *GRAD_MATCH,
                )
            )
            if self.dash is not None:
                self.dash.log(
                    f"Tor sẵn sàng · {len(proxies)} tunnels · "
                    f"127.0.0.1:{first_port}-{last_port} · dữ liệu công khai"
                )
        except SystemExit:
            # tor_manager gọi sys.exit(1) khi bootstrap fail — không làm hỏng scan.
            self.console.print(
                "[red]⚠ Tor bootstrap thất bại — tiếp tục KHÔNG qua Tor (direct).[/]"
            )
        except Exception as exc:
            self.console.print(
                f"[red]⚠ Không khởi tạo được Tor "
                f"({type(exc).__name__}: {exc}) — tiếp tục KHÔNG qua Tor.[/]"
            )

    def _shutdown_tor(self) -> None:
        """Dừng Tor process do chính scanner spawn."""
        if self._tor_module is None:
            return
        try:
            self._tor_module.Stop_Running_Tor()
        except Exception as exc:
            self.console.print(f"[yellow]⚠ Lỗi khi dừng Tor:[/] {type(exc).__name__}: {exc}")
        finally:
            self._tor_module = None

    # ------------------------------------------------------------------
    # UI helpers (log theo dòng + gradient, không kẻ khung)
    # ------------------------------------------------------------------

    def _tor_label(self) -> str:
        """Nhãn Tor cho dashboard (classic mode không dùng)."""
        if not self.tor_settings.get("enabled"):
            return f"{DASH_DIM}Disabled (Direct IP){DASH_RESET}"
        if self.proxies:
            return f"{DASH_GREEN}Enabled ({len(self.proxies)} tunnels){DASH_RESET}"
        return f"{DASH_YELLOW}Starting / failed (Direct IP){DASH_RESET}"

    def _ui_render(self) -> None:
        """Vẽ dashboard (no-op ở classic mode). Mọi lỗi nuốt an toàn."""
        if self.dash is None:
            return
        try:
            self.dash.tor_label = self._tor_label()
            self.dash.render(self.stats)
        except Exception:
            pass

    def _ui_event(self, plain: str, rich: Optional[str] = None) -> None:
        """1 sự kiện runtime: dashboard -> Activity log + render; classic -> Rich."""
        if self.dash is None:
            self.console.print(rich if rich is not None else plain)
            return
        try:
            self.dash.log(plain)
            self._ui_render()
        except Exception:
            pass

    def _ui_section(
        self,
        title: str,
        grad: Tuple[str, str] = GRAD_TITLE,
        style: str = "dim cyan",
    ) -> None:
        """Phân cách mục: classic dùng Rich rule; dashboard dùng '-- title --' ASCII."""
        if self.dash is None:
            self.console.rule(gradient_text(title, *grad), style=style)
        else:
            # Console của Rich không render được ANSI thô (hiện ra '[90m-- ...')
            # → dùng markup Rich '[dim]' cho đúng.
            self.console.print(f"[dim]-- {title} --[/]")

    def show_header(self):
        banner = gradient_text("ROBLOX GROUP OWNER SCANNER", *GRAD_TITLE, base_style="bold")
        subtitle = gradient_text(
            "Gradient logs • Update progress • Line summary", *GRAD_MATCH
        )
        self.console.print(banner, justify="center")
        self.console.print(subtitle, justify="center")
        self.console.rule(style="bright_cyan")

    def setup_filters(self):
        """Hỏi user để setup các điều kiện lọc."""
        self.console.print(
            gradient_text(
                "Bộ lọc group — chỉ gửi webhook nếu thỏa ÍT NHẤT 1 điều kiện",
                *GRAD_TITLE,
            )
        )

        # Không nuốt KeyboardInterrupt ở đây: Ctrl+C phải dừng chương trình.
        self.filters["robux"] = IntPrompt.ask("💰 Robux tối thiểu (0 = luôn đạt)", default=100)
        self.filters["pending"] = IntPrompt.ask(
            "⏳ Pending Robux tối thiểu (0 = luôn đạt)", default=100
        )
        self.filters["games"] = IntPrompt.ask("🎮 Games tối thiểu (0 = luôn đạt)", default=5)
        self.filters["visits"] = IntPrompt.ask("👁️  Visits tối thiểu (0 = luôn đạt)", default=1000)
        self.filters["clothing"] = IntPrompt.ask("👕 Clothing tối thiểu (0 = luôn đạt)", default=10)

        for key, value in self.filters.items():
            note = " [dim](0 = mọi group đều đạt)[/]" if value == 0 else ""
            self.console.print(
                f"  {FILTER_ICONS[key]} [cyan]{key:<8}[/] [bold green]>= {value:,}[/]{note}"
            )

        if self.dash is not None:
            f = self.filters
            self.dash.filters_line = (
                f"R${f.get('robux', 0):,}+ · pending {f.get('pending', 0):,}+ · "
                f"games {f.get('games', 0):,}+ · visits {f.get('visits', 0):,}+ · "
                f"clothing {f.get('clothing', 0):,}+"
            )

    def _log_group_result(self, result: Dict[str, Any]) -> None:
        """Log 1 dòng cho group ĐẠT FILTER hoặc lỗi; SKIP im lặng (chỉ đếm ở progress)."""
        status = result.get("status")
        info = result.get("group_info", {})
        name = str(info.get("group_name", "Unknown"))
        group_id = int(info.get("group_id", 0) or 0)
        matched = " · ".join(result.get("matched") or [])

        if self.dash is not None:
            # Dashboard: 1 nhóm sự kiện -> 1 dòng Activity ngắn gọn.
            try:
                plain = ""
                if status in MATCHED_STATUSES:
                    tag = "" if info.get("is_real_owner", True) else " [MẤT QUYỀN]"
                    if (
                        int(info.get("sales", 0) or 0) > 0
                        or int(info.get("premium_payouts", 0) or 0) > 0
                    ):
                        extra = (
                            f" · sales {int(info.get('sales', 0) or 0):,}"
                            f" · prem {int(info.get('premium_payouts', 0) or 0):,}"
                        )
                    else:
                        extra = ""
                    if status == "WEBHOOK_FAIL":
                        tail = "webhook FAIL"
                    elif status == "SENT_PARTIAL":
                        tail = "webhook ok (thiếu dữ liệu)"
                    else:
                        tail = "webhook ok"
                    plain = (
                        f"MATCH {name}{tag} · id {group_id} · {matched}{extra}"
                        f" -> {tail}"
                    )
                elif status == "PARTIAL":
                    plain = (
                        f"WARN {name} · id {group_id} · thiếu dữ liệu: "
                        f"{result.get('message') or ''}"
                    )
                elif status == "FAIL":
                    plain = (
                        f"FAIL {name} · id {group_id} · "
                        f"{result.get('message') or ''}"
                    )
                if plain:
                    self.dash.log(plain)
            except Exception:
                pass
            self._ui_render()
            return

        if status in MATCHED_STATUSES:
            if status == "WEBHOOK_FAIL":
                arrow = f"→ webhook ✗ ({result.get('message') or 'failed'})"
            else:
                arrow = "→ webhook ✓"
            owner_flag = "" if info.get("is_real_owner", True) else " [bold red](⚠ MẤT QUYỀN)[/]"
            line = gradient_text(f"✓ MATCH {name}{owner_flag} · id {group_id} · {matched} {arrow}", *GRAD_MATCH)
            self.console.print(line)
            if int(info.get("sales", 0) or 0) > 0 or int(info.get("premium_payouts", 0) or 0) > 0:
                self.console.print(
                    f"    [dim]🛍️ Sales: {int(info.get('sales', 0)):,} · ⭐ Premium Pay: {int(info.get('premium_payouts', 0)):,}[/]"
                )
            if status == "SENT_PARTIAL":
                self.console.print(
                    f"    [dim]⚠ gửi webhook xong nhưng thiếu dữ liệu: "
                    f"{escape(result.get('message') or '')}[/]"
                )
        elif status == "PARTIAL":
            # Chưa đạt filter nhưng thiếu dữ liệu: có thể đã đạt nếu metric lỗi lấy được.
            self.console.print(
                f"[yellow]⚠ {escape(name)} · id {group_id} · chưa đạt filter "
                f"nhưng thiếu dữ liệu: {escape(result.get('message') or '')}[/]"
            )
        elif status == "FAIL":
            self.console.print(
                f"[red]✗ {escape(name)} · id {group_id} · "
                f"{escape(result.get('message') or '')}[/]"
            )
        # SKIP: không in dòng riêng theo yêu cầu — số liệu nằm ở progress/summary.

    def render_account_summary(self, roblox_username: str, results: List[Dict[str, Any]]):
        """Tổng kết gọn 1 account bằng các dòng log (thay cho bảng kết quả)."""
        if self.dash is not None:
            # Dashboard: nén tổng kết account thành 1 dòng Activity.
            try:
                if not results:
                    self.dash.log(f"SUMMARY {roblox_username}: không có group owner.")
                else:
                    m = sum(1 for r in results if r["status"] in MATCHED_STATUSES)
                    s = sum(1 for r in results if r["status"] in ("SENT", "SENT_PARTIAL"))

                    def total(key: str) -> int:
                        return sum(
                            int(r.get("group_info", {}).get(key, 0) or 0)
                            for r in results
                        )

                    self.dash.log(
                        f"SUMMARY {roblox_username}: matched {m} (sent {s}) · "
                        f"robux {total('robux'):,} · pend {total('pending'):,} · "
                        f"combined {total('robux') + total('pending'):,}"
                    )
            except Exception:
                pass
            self._ui_render()
            return

        if not results:
            self.console.print(
                f"[yellow]Không có group owner để hiển thị cho[/] [bold]{escape(roblox_username)}[/]."
            )
            return

        matched = sum(1 for r in results if r["status"] in MATCHED_STATUSES)
        sent = sum(1 for r in results if r["status"] in ("SENT", "SENT_PARTIAL"))
        hook_fail = sum(1 for r in results if r["status"] == "WEBHOOK_FAIL")
        partial = sum(1 for r in results if r["status"] == "PARTIAL")
        skip = sum(1 for r in results if r["status"] == "SKIP")
        fail = sum(1 for r in results if r["status"] == "FAIL")

        def total(key: str) -> int:
            return sum(int(r.get("group_info", {}).get(key, 0) or 0) for r in results)

        self.console.rule(
            gradient_text(f"{roblox_username} · {len(results)} owner groups", *GRAD_TITLE),
            style="dim cyan",
        )
        self.console.print(
            f"  ✓ matched [bold green]{matched}[/] (webhook → [bold green]{sent}[/])"
            + (f" · webhook ✗ [bold red]{hook_fail}[/]" if hook_fail else "")
            + f" · skip [dim]{skip}[/] · ⚠ partial [yellow]{partial}[/] · ✗ fail [red]{fail}[/]"
        )
        self.console.print(
            f"  Σ robux [bold green]{total('robux'):,}[/] · "
            f"pending [bold yellow]{total('pending'):,}[/] · "
            f"combined [bold bright_green]{total('robux') + total('pending'):,}[/]"
        )

    def render_summary(self):
        """Summary dạng dòng: số liệu chung, grand totals và đối soát group."""
        stats = self.stats
        processed = stats.checked_success + stats.checked_failed

        self._ui_section("Summary", GRAD_WARN, "dim yellow")
        rows = [
            ("Cookies loaded", f"{stats.cookies_loaded:,}"),
            ("Valid accounts", f"{stats.valid_accounts:,}"),
            ("Invalid cookies (401/403)", f"{stats.invalid_cookies:,}"),
            ("Account failures (chưa scan được)", f"{stats.account_failures:,}"),
            ("Owned groups found", f"{stats.owned_groups:,}"),
            ("Group checks ok", f"{stats.checked_success:,}"),
            ("Group checks failed", f"{stats.checked_failed:,}"),
            ("Matched filters", f"{stats.matched_filters:,}"),
            ("Skipped by filters", f"{stats.skipped_filters:,}"),
            ("Partial data (thiếu metric)", f"{stats.partial_checks:,}"),
            ("Webhooks sent", f"{stats.webhooks_sent:,}"),
            ("Webhooks failed", f"{stats.webhooks_failed:,}"),
            ("Rechecked groups", f"{stats.rechecked_groups:,}"),
            ("Recovered by re-check", f"{stats.recheck_recovered:,}"),
            ("Accounts recovered by re-check", f"{stats.accounts_recovered:,}"),
        ]
        for label, value in rows:
            self.console.print(f"  [cyan]{label:<32}[/] [bold white]{value}[/]")

        self._ui_section("Grand Totals", GRAD_OK, "dim green")
        totals = [
            ("💰 Robux", f"[bold green]{stats.total_robux:,}[/]"),
            ("⏳ Pending Robux", f"[bold yellow]{stats.total_pending:,}[/]"),
            ("💵 Robux + Pending", f"[bold bright_green]{stats.total_robux + stats.total_pending:,}[/]"),
        ]
        for label, value in totals:
            self.console.print(f"  {label:<18} {value}")

        if stats.fake_owner_groups > 0:
            self.console.print(
                f"  [bold red]⚠ Group mất quyền/fake owner: {stats.fake_owner_groups:,}[/] "
                f"(rank 255 nhưng không khớp Owner ID)"
            )

        # Phân bổ request qua từng proxy Tor (chỉ khi Tor đang bật).
        if self.proxies:
            usage = " · ".join(
                f"{self._proxy_label(proxy)}→{self.proxy_usage.get(proxy, 0):,} req"
                for proxy in self.proxies
            )
            self.console.print(gradient_text(f"Tor usage: {usage}", *GRAD_TITLE))

        # Đối soát: mọi group tìm thấy phải được xử lý, không account nào bị bỏ lặng.
        reconciled = processed == stats.owned_groups and not stats.failed_accounts
        line = gradient_text(
            f"Groups: found {stats.owned_groups:,} · processed {processed:,}"
            f" · matched {stats.matched_filters:,} · sent {stats.webhooks_sent:,}",
            *(GRAD_OK if reconciled else GRAD_WARN),
        )
        self.console.print(line)
        if stats.failed_accounts:
            self.console.print(
                f"[yellow]⚠ Accounts chưa scan được (group bên trong chưa được check):[/] "
                f"{escape(', '.join(stats.failed_accounts))}"
            )

        if stats.webhooks_failed:
            # Phân biệt rõ: group ĐẠT filter nhưng gửi Discord thất bại
            # (khác với group không đạt filter) — kèm hành động xử lý.
            self.console.print(
                f"[bold red]⚠ {stats.webhooks_failed} group ĐẠT filter nhưng "
                f"gửi webhook Discord THẤT BẠI[/] — kiểm tra lại "
                f"[bold]webhook_url[/] trong config.json (URL sai, đã bị "
                f"disable hoặc bị rate-limit Discord)."
            )

        if stats.checked_failed or stats.account_failures or stats.webhooks_failed:
            self.console.print(
                gradient_text(
                    "⚠ Có mục thất bại sau retry đầy đủ — xem các dòng ⚠/✗ phía trên.",
                    *GRAD_WARN,
                )
            )
        else:
            self.console.print(
                gradient_text("✔ Hoàn tất: tất cả group owner được xử lý thành công.", *GRAD_OK)
            )

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    def load_cookies_from_file(self, filename: str = "cookies.txt") -> List[str]:
        """Đọc cookies từ file cookies.txt, bỏ trùng lặp để không scan/webhook 2 lần."""
        path = BASE_DIR / filename
        cookies: List[str] = []

        if not path.exists():
            self.console.print(f"[red]Không tìm thấy file[/] [bold]{path.name}[/].")
            return cookies

        seen = set()
        duplicates = 0
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or not line.startswith("_|WARNING:-DO-NOT-SHARE-THIS."):
                    continue
                if line in seen:
                    duplicates += 1
                    continue
                seen.add(line)
                cookies.append(line)

        self.stats.cookies_loaded = len(cookies)
        self.console.print(
            f"[green]✓[/] Đã load [bold]{len(cookies)}[/] cookie từ [bold]{path.name}[/]."
        )
        if duplicates:
            self.console.print(f"[yellow]Bỏ qua {duplicates} cookie trùng lặp.[/]")
        return cookies

    def _should_retry_status(self, status: int) -> bool:
        return status in {408, 409, 425, 429, 500, 502, 503, 504}

    def _retry_delay(
        self, attempt: int, response: Optional[aiohttp.ClientResponse] = None
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    # 429: tôn trọng Retry-After tới 30s; status khác giữ cap 18s.
                    cap = 30.0 if response.status == 429 else self.retry_max_delay
                    return min(float(retry_after), cap)
                except ValueError:
                    pass

        delay = min(self.retry_base_delay * (2 ** (attempt - 1)), self.retry_max_delay)
        return delay + random.uniform(0.05, 0.45)

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        payload: Optional[Dict[str, Any]] = None,
        expected_statuses: Tuple[int, ...] = (200,),
        parse_json: bool = True,
        retries: Optional[int] = None,
        proxy: Optional[str] = None,
        fast_429: bool = False,
        file_attachment: Optional[Tuple[str, bytes]] = None,
    ) -> Tuple[Optional[Any], int, Optional[str]]:
        """HTTP helper có retry/backoff cho mọi request chính.

        proxy: HTTP proxy Tor (http://127.0.0.1:port) hoặc None để đi thẳng.
        fast_429: gặp 429 thì chỉ thử tối đa 2 attempt rồi trả lỗi ngay
        (để lượt re-check cuối scan xử lý, không đốt slot semaphore chờ 5×30s).
        file_attachment: (filename, bytes) — khi có thì gửi multipart Discord
        (payload_json + files[0]); FormData khởi tạo mới mỗi attempt vì
        aiohttp consume body sau 1 lần gửi.
        """
        max_retries = retries if retries is not None else self.max_retries
        last_status = 0
        last_error = None
        host = urlparse(url).hostname or ""

        for attempt in range(1, max_retries + 1):
            try:
                await self.pacer.wait(host)
                if file_attachment is not None:
                    # FormData mới mỗi attempt — body cũ đã bị consume.
                    form = aiohttp.FormData()
                    form.add_field(
                        "payload_json",
                        json.dumps(payload or {}),
                        content_type="application/json",
                    )
                    form.add_field(
                        "files[0]",
                        file_attachment[1],
                        filename=file_attachment[0],
                        content_type="application/zip",
                    )
                    req_kwargs: Dict[str, Any] = {
                        "headers": headers,
                        "data": form,
                    }
                else:
                    req_kwargs = {"headers": headers, "json": payload}
                async with session.request(
                    method,
                    url,
                    ssl=False,
                    timeout=self.request_timeout,
                    proxy=proxy,
                    **req_kwargs,
                ) as response:
                    if proxy:
                        # Chuẩn hóa key theo full URL gốc để khớp với
                        # self.proxies khi render summary (fix bug luôn 0).
                        scheme, _, netloc = proxy.partition("://")
                        host_port = netloc.split("@")[-1]
                        base_proxy = f"{scheme}://{host_port}"
                        self.proxy_usage[base_proxy] = self.proxy_usage.get(base_proxy, 0) + 1
                    last_status = response.status
                    self.pacer.record(host, response.status)
                    text = await response.text()

                    if response.status in expected_statuses:
                        if not parse_json:
                            return text, response.status, None

                        if not text.strip():
                            return {}, response.status, None

                        try:
                            return json.loads(text), response.status, None
                        except json.JSONDecodeError:
                            return (
                                None,
                                response.status,
                                "Response không phải JSON hợp lệ",
                            )

                    last_error = f"HTTP {response.status}"

                    # Auto-heal CSRF Token: nếu 403 mà Roblox trả x-csrf-token mới trong response header
                    if response.status == 403 and headers is not None:
                        new_csrf = response.headers.get("x-csrf-token") or response.headers.get("X-CSRF-TOKEN")
                        if new_csrf and ("x-csrf-token" in headers or "X-CSRF-TOKEN" in headers):
                            headers["x-csrf-token"] = new_csrf
                            if "X-CSRF-TOKEN" in headers:
                                headers["X-CSRF-TOKEN"] = new_csrf
                            if attempt < max_retries:
                                await asyncio.sleep(0.1)
                                continue

                    if (
                        self._should_retry_status(response.status)
                        and attempt < max_retries
                    ):
                        if fast_429 and response.status == 429 and attempt >= 2:
                            return (
                                None,
                                response.status,
                                "HTTP 429 — để lượt re-check xử lý",
                            )
                        await asyncio.sleep(self._retry_delay(attempt, response))
                        continue

                    return None, response.status, last_error

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < max_retries:
                    await asyncio.sleep(self._retry_delay(attempt))
                    continue

        return None, last_status, last_error or "Request failed"

    async def _webhook_api(
        self,
        method: str,
        url: str,
        payload: Optional[Dict[str, Any]] = None,
        file_attachment: Optional[Tuple[str, bytes]] = None,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Gọi Discord webhook (POST/PATCH) có throttle chung + retry nhẹ.

        Mọi đường webhook (status embed, group embed, grand totals) đi qua
        đây để giữ khoảng cách >= webhook_min_interval giữa 2 request —
        tránh 429 khi nhiều group xong gần như cùng lúc.
        file_attachment: (filename, bytes) — Grand Totals đính kèm ZIP report.
        """
        async with self.webhook_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self.webhook_next > now:
                await asyncio.sleep(self.webhook_next - now)
            self.webhook_next = loop.time() + self.webhook_min_interval

            async with aiohttp.ClientSession() as session:
                data, _, error = await self._request(
                    session,
                    method,
                    url,
                    payload=payload,
                    expected_statuses=(200, 204),
                    retries=2,
                    fast_429=True,
                    file_attachment=file_attachment,
                )
        if error:
            return None, error
        return data, None

    async def _status_record(
        self,
        username: str,
        result: Dict[str, Any],
        *,
        recheck: bool = False,
        prev_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Ghi 1 kết quả group vào status embed của account (nếu đang hoạt động).

        Lỗi webhook chỉ log — không bao giờ làm hỏng kết quả scan; account
        chưa có status embed (tạo thất bại / tắt config) sẽ dùng webhook
        từng group như cũ.
        """
        status = self._statuses.get(username)
        if status is None or not status.active:
            return
        try:
            await status.record(result, recheck=recheck, prev_result=prev_result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ui_event(
                f"Cập nhật status embed {username} lỗi: {type(exc).__name__}",
                f"[dim]⚠ Cập nhật status embed {escape(username)} lỗi: "
                f"{type(exc).__name__}[/]",
            )

    # ------------------------------------------------------------------
    # Account / group API
    # ------------------------------------------------------------------

    async def check_cookie(
        self,
        cookie: str,
        proxy: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """Kiểm tra cookie và lấy thông tin user.

        Trả về (user_info, http_status). status == 0 nghĩa là lỗi mạng/timeout —
        KHÔNG kết luận cookie invalid; caller phân loại theo status.
        session: tái sử dụng session đã gắn cookie của account (không tạo mới).
        """
        if session is not None:
            data, status, _ = await self._request(
                session,
                "GET",
                "https://users.roblox.com/v1/users/authenticated",
                proxy=proxy,
            )
        else:
            async with aiohttp.ClientSession(
                cookies={".ROBLOSECURITY": cookie}
            ) as own_session:
                data, status, _ = await self._request(
                    own_session,
                    "GET",
                    "https://users.roblox.com/v1/users/authenticated",
                    proxy=proxy,
                )

        # Dùng `is not None` + check id: body rỗng trả về {} là falsy nhưng không phải lỗi.
        if data is not None and data.get("id") is not None:
            return {"cookie": cookie, "name": data.get("name"), "id": data["id"]}, status
        return None, status

    async def get_csrf_token(
        self,
        cookie: str,
        proxy: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> str:
        """Lấy X-CSRF-Token có retry, đi qua DomainPacer.

        Dùng endpoint nhẹ /v2/logout (luôn 403 + header x-csrf-token) thay cho
        /v2/login để tránh Funcaptcha payload nặng.
        session: tái sử dụng session của account (đã có cookie jar) — khi đó
        không gửi header Cookie thủ công để tránh trùng cookie header.
        """
        host = "auth.roblox.com"
        headers = {} if session is not None else {"Cookie": f".ROBLOSECURITY={cookie}"}
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession()

        try:
            for attempt in range(1, self.max_retries + 1):
                try:
                    await self.pacer.wait(host)
                    async with session.post(
                        "https://auth.roblox.com/v2/logout",
                        headers=headers,
                        ssl=False,
                        timeout=self.request_timeout,
                        proxy=proxy,
                    ) as response:
                        self.pacer.record(host, response.status)
                        token = response.headers.get("x-csrf-token", "")
                        if token:
                            return token

                        if (
                            self._should_retry_status(response.status)
                            and attempt < self.max_retries
                        ):
                            await asyncio.sleep(self._retry_delay(attempt, response))
                            continue

                        # Không retryable + không token → retry vô ích, dừng ngay
                        # (tránh tight loop 5 request liên tục không nghỉ).
                        break

                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._retry_delay(attempt))
                        continue
        finally:
            if owns_session:
                await session.close()

        return ""

    async def get_user_groups(
        self,
        session: aiohttp.ClientSession,
        user_id: int,
        proxy: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Lấy danh sách groups của user với retry."""
        data, _, error = await self._request(
            session,
            "GET",
            f"https://groups.roblox.com/v2/users/{user_id}/groups/roles",
            proxy=proxy,
        )

        if data is None:
            return [], error

        return data.get("data", []), None

    async def get_user_avatar(
        self,
        session: aiohttp.ClientSession,
        user_id: int,
        proxy: Optional[str] = None,
    ) -> Optional[str]:
        """Avatar headshot 150x150 của user — thumbnail cho status embed.

        1 request duy nhất cho mỗi account (thay cho N request icon group
        không hề được hiển thị). Mọi lỗi đều trả None, không chặn quét.
        """
        if user_id <= 0:
            return None
        data, _, _ = await self._request(
            session,
            "GET",
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={user_id}&size=150x150&format=Png&isCircular=false",
            proxy=proxy,
            fast_429=True,
        )
        if isinstance(data, dict) and data.get("data"):
            url = data["data"][0].get("imageUrl")
            return url or None
        return None

    # Trần pagination cho mục đích filter: đủ vượt xa ngưỡng filter
    # thông thường mà không giữ Semaphore hàng phút.
    MAX_PAGES = 30

    async def get_group_games_summary(
        self,
        session: aiohttp.ClientSession,
        group_id: int,
        proxy: Optional[str] = None,
        fast_429: bool = False,
    ) -> Tuple[int, int, Optional[str]]:
        """Lấy toàn bộ games + visits bằng pagination.

        Lưu ý: accessFilter=2 nên API chỉ trả game public — game private
        của group không được đếm (giới hạn của Roblox API).
        """
        games_count = 0
        visits = 0
        cursor = None
        page_guard = 0

        while True:
            params = {
                "accessFilter": 2,
                "limit": 100,
                "sortOrder": "Asc",
            }
            if cursor:
                params["cursor"] = cursor

            url = f"https://games.roblox.com/v2/groups/{group_id}/gamesV2?{urlencode(params)}"
            data, _, error = await self._request(
                session, "GET", url, proxy=proxy, fast_429=fast_429
            )
            if data is None:
                return games_count, visits, error or "Không lấy được games"

            games = data.get("data", [])
            games_count += len(games)
            visits += sum(int(game.get("placeVisits") or 0) for game in games)

            cursor = data.get("nextPageCursor")
            page_guard += 1
            if not cursor or page_guard >= self.MAX_PAGES:
                break

        return games_count, visits, None

    async def get_group_clothing_count(
        self,
        session: aiohttp.ClientSession,
        group_id: int,
        proxy: Optional[str] = None,
        fast_429: bool = False,
    ) -> Tuple[int, Optional[str]]:
        """Lấy toàn bộ clothing bằng pagination."""
        clothing = 0
        cursor = None
        page_guard = 0

        while True:
            params = {
                "Category": 3,
                "SortType": "Relevance",
                "CreatorTargetId": group_id,
                "ResultsPerPage": 100,
                "CreatorType": 2,
            }
            if cursor:
                params["Cursor"] = cursor

            url = f"https://catalog.roblox.com/v1/search/items/details?{urlencode(params)}"
            data, _, error = await self._request(
                session, "GET", url, proxy=proxy, fast_429=fast_429
            )
            if data is None:
                return clothing, error or "Không lấy được clothing"

            clothing += len(data.get("data", []))
            cursor = data.get("nextPageCursor")
            page_guard += 1
            if not cursor or page_guard >= self.MAX_PAGES:
                break

        return clothing, None

    async def get_revenue_summary(
        self,
        session: aiohttp.ClientSession,
        group_id: int,
        headers: Dict[str, str],
        proxy: Optional[str] = None,
        fast_429: bool = False,
    ) -> Tuple[Dict[str, int], Optional[str]]:
        """Lấy doanh thu (Pending Robux, Premium Payouts, Item Sales) của group.

        Thử 'Year' trước (cửa sổ lớn nhất, gồm Month/Week) — group thường chỉ
        tốn đúng 1 request. Chỉ khi 'Year' trả 400/404 mới thử sang 'Month'
        (group mới/cửa sổ Year trống nhưng Month có data). 400/404 cả hai =
        không có hoạt động kinh doanh → doanh thu = 0. 429/lỗi khác thoát ngay
        để vòng re-check xử lý, không spam pacer.
        """
        metrics = {"pending": 0, "premium_payouts": 0, "sales": 0}
        for timeframe in ("Year", "Month"):
            url = f"https://economy.roblox.com/v1/groups/{group_id}/revenue/summary/{timeframe}"
            data, status, error = await self._request(
                session, "GET", url, headers=headers, proxy=proxy, fast_429=fast_429
            )

            if data is not None and isinstance(data, dict):
                metrics["pending"] = int(data.get("pendingRobux") or 0)
                metrics["premium_payouts"] = int(data.get("premiumPayouts") or 0)
                metrics["sales"] = int(data.get("itemSaleRobux") or 0)
                return metrics, None

            if status == 429:
                return metrics, error or "Revenue API HTTP 429 — để lượt re-check xử lý"

            if status in {400, 404}:
                # Year trống nhưng Month có thể có data → thử timeframe tiếp.
                continue
            return metrics, error or f"Revenue API HTTP {status}"
        # Cả Year lẫn Month đều 400/404 → group không có hoạt động kinh doanh.
        return metrics, None

    async def check_group_details(
        self,
        session: aiohttp.ClientSession,
        bare_session: aiohttp.ClientSession,
        group_id: int,
        cookie: str,
        xcsrf: str,
        current_user_id: int = 0,
        group_name_hint: Optional[str] = None,
        public_proxy: Optional[str] = None,
        prev_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Kiểm tra chi tiết thông tin của group.

        session: auth session (có cookie) — CHỈ dùng cho currency/revenue,
        luôn đi IP gốc, không bao giờ qua Tor (tránh bị flag token).
        bare_session: session KHÔNG cookie cho 4 endpoint public
        (games/clothing/thumbnail/group-info) — được phép qua Tor nếu bật.
        6 endpoint độc lập chạy song song; endpoint nào lỗi thì ghi vào errors
        và giữ giá trị 0 — group vẫn được đánh giá filter trên metric có dữ liệu.
        prev_info: kết quả lượt trước — nhánh đã nằm trong _ok được GIỮ NGUYÊN
        số liệu và KHÔNG gọi lại API (incremental re-check), chỉ fetch nhánh lỗi.
        Khi prev_info khác None (vòng re-check): direct (currency/revenue, IP gốc)
        tắt fast_429 để retry sâu (tới 5 lần, chờ Retry-After đầy đủ) và nới
        timeout nhóm ×4; nhánh public (games/clothing/basic) qua Tor luôn
        fast_429=True (bail sau 2 attempt, tránh phạt DomainPacer dùng chung),
        nếu vẫn lỗi thì fallback thẳng IP gốc (proxy=None, fast_429=False) —
        cookie/token không bao giờ rời auth session. Nhánh pagination lỗi giữa
        chừng giữ max với số liệu lượt trước (prev_games/visits/clothing).
        """
        errors: List[str] = []
        headers = {
            "Cookie": f".ROBLOSECURITY={cookie}",
            "x-csrf-token": xcsrf,
        }
        prev = prev_info or {}
        ok_set = set(prev.get("_ok") or ())
        # Vòng re-check (prev_info khác None): retry sâu thay vì bail sớm.
        is_recheck = prev_info is not None
        direct_fast_429 = not is_recheck
        # Qua Tor luôn bail nhanh (2 attempt) để tránh phạt DomainPacer dùng
        # chung lên 8.0s/request; retry sâu chỉ dành cho fallback IP gốc.
        public_fast_429 = True if public_proxy is not None else direct_fast_429
        prev_games = int(prev.get("games", 0) or 0)
        prev_visits = int(prev.get("visits", 0) or 0)
        prev_clothing = int(prev.get("clothing", 0) or 0)

        group_info = {
            "group_id": group_id,
            "group_name": prev.get("group_name") or group_name_hint or f"Group {group_id}",
            "robux": int(prev.get("robux", 0) or 0),
            "pending": int(prev.get("pending", 0) or 0),
            "premium_payouts": int(prev.get("premium_payouts", 0) or 0),
            "sales": int(prev.get("sales", 0) or 0),
            "games": int(prev.get("games", 0) or 0),
            "visits": int(prev.get("visits", 0) or 0),
            "clothing": int(prev.get("clothing", 0) or 0),
            "members": int(prev.get("members", 0) or 0),
            "thumbnail": prev.get("thumbnail"),
            "is_real_owner": prev.get("is_real_owner", True),
            "owner_id": int(prev.get("owner_id", 0) or 0),
            "owner_name": prev.get("owner_name"),
            "errors": errors,
            "_ok": ok_set,
        }

        async def fetch_games():
            if "games" in ok_set:
                return
            count, visits, error = await self.get_group_games_summary(
                bare_session, group_id, public_proxy, fast_429=public_fast_429
            )
            if error and is_recheck and public_proxy is not None:
                # Retry sâu vẫn lỗi qua Tor → thử thẳng IP gốc (không cookie).
                fb_count, fb_visits, fb_error = await self.get_group_games_summary(
                    bare_session, group_id, None, fast_429=False
                )
                if fb_error is None or (fb_visits, fb_count) > (visits, count):
                    count, visits, error = fb_count, fb_visits, fb_error
            if error:
                # Giữ số liệu pagination dở dang lượt trước, tránh mất dữ liệu.
                group_info["games"] = max(count, prev_games)
                group_info["visits"] = max(visits, prev_visits)
                errors.append(f"games: {error}")
            else:
                group_info["games"] = count
                group_info["visits"] = visits
                ok_set.add("games")

        async def fetch_currency():
            if "robux" in ok_set:
                return
            # Token-bearing: auth session + IP gốc, không proxy.
            data, _, error = await self._request(
                session,
                "GET",
                f"https://economy.roblox.com/v1/groups/{group_id}/currency",
                headers=headers,
                fast_429=direct_fast_429,
            )
            if data is not None and isinstance(data, dict):
                group_info["robux"] = int(data.get("robux") or 0)
                ok_set.add("robux")
            else:
                errors.append(f"robux: {error or 'Không lấy được Robux'}")

        async def fetch_revenue():
            if "revenue" in ok_set:
                return
            # Token-bearing: auth session + IP gốc, không proxy.
            rev, error = await self.get_revenue_summary(
                session, group_id, headers, fast_429=direct_fast_429
            )
            if error:
                errors.append(f"revenue: {error}")
                return
            group_info["pending"] = rev["pending"]
            group_info["premium_payouts"] = rev["premium_payouts"]
            group_info["sales"] = rev["sales"]
            ok_set.add("revenue")

        async def fetch_clothing():
            if "clothing" in ok_set:
                return
            clothing, error = await self.get_group_clothing_count(
                bare_session, group_id, public_proxy, fast_429=public_fast_429
            )
            if error and is_recheck and public_proxy is not None:
                # Retry sâu vẫn lỗi qua Tor → thử thẳng IP gốc (không cookie).
                fb_clothing, fb_error = await self.get_group_clothing_count(
                    bare_session, group_id, None, fast_429=False
                )
                if fb_error is None or fb_clothing > clothing:
                    clothing, error = fb_clothing, fb_error
            if error:
                # Giữ số liệu pagination dở dang lượt trước, tránh mất dữ liệu.
                group_info["clothing"] = max(clothing, prev_clothing)
                errors.append(f"clothing: {error}")
            else:
                group_info["clothing"] = clothing
                ok_set.add("clothing")

        async def fetch_thumbnail():
            if "thumbnail" in ok_set:
                return
            if self.status_embeds_enabled:
                # Embed trạng thái hiển thị avatar account — icon group không
                # được dùng tới. Bỏ N request icon/group/account; webhook lẻ
                # (fallback khi mở embed thất bại) dùng avatar thay thế.
                ok_set.add("thumbnail")
                return
            data, _, _ = await self._request(
                bare_session,
                "GET",
                f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={group_id}&size=150x150&format=Png&isCircular=false",
                proxy=public_proxy,
                fast_429=True,
            )
            if data and data.get("data"):
                group_info["thumbnail"] = data["data"][0].get("imageUrl")
                ok_set.add("thumbnail")

        async def fetch_basic():
            if "basic" in ok_set:
                return
            data, _, error = await self._request(
                bare_session,
                "GET",
                f"https://groups.roblox.com/v1/groups/{group_id}",
                proxy=public_proxy,
                fast_429=public_fast_429,
            )
            if (
                data is None or not isinstance(data, dict)
            ) and is_recheck and public_proxy is not None:
                # Retry sâu vẫn lỗi qua Tor → thử thẳng IP gốc (không cookie).
                data, _, error = await self._request(
                    bare_session,
                    "GET",
                    f"https://groups.roblox.com/v1/groups/{group_id}",
                    proxy=None,
                    fast_429=False,
                )
            if data is not None and isinstance(data, dict):
                group_info["members"] = int(data.get("memberCount") or 0)
                group_info["group_name"] = (
                    data.get("name") or group_info["group_name"]
                )
                owner = data.get("owner")
                if owner and isinstance(owner, dict):
                    group_info["owner_id"] = int(owner.get("userId") or 0)
                    group_info["owner_name"] = owner.get("username")
                    if current_user_id > 0:
                        group_info["is_real_owner"] = (
                            group_info["owner_id"] == current_user_id
                        )
                else:
                    group_info["is_real_owner"] = False
                    group_info["owner_id"] = 0
                    group_info["owner_name"] = "Không có Owner (Locked/Abandoned)"
                ok_set.add("basic")
            else:
                errors.append(f"basic: {error or 'Không lấy được group info'}")

        # Single event-loop nên list.append từ nhiều task là an toàn.
        # Bọc timeout tổng để 1 group pagination lớn không giữ Semaphore.
        # Vòng re-check retry sâu (Retry-After có thể tới 30s/lần) nên timeout ×4.
        check_timeout = self.group_timeout * 4 if is_recheck else self.group_timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    fetch_games(),
                    fetch_currency(),
                    fetch_revenue(),
                    fetch_clothing(),
                    fetch_thumbnail(),
                    fetch_basic(),
                ),
                timeout=check_timeout,
            )
        except asyncio.TimeoutError:
            errors.append(f"timeout: quá {check_timeout:g}s — để lượt re-check xử lý")

        return group_info

    def _accumulate_totals(self, group_info: Dict[str, Any]) -> None:
        """Cộng dồn các số liệu của group vào tổng toàn cục."""
        self.stats.total_robux += int(group_info.get("robux", 0) or 0)
        self.stats.total_pending += int(group_info.get("pending", 0) or 0)
        self.stats.total_sales += int(group_info.get("sales", 0) or 0)
        self.stats.total_premium += int(group_info.get("premium_payouts", 0) or 0)
        self.stats.total_games += int(group_info.get("games", 0) or 0)
        self.stats.total_visits += int(group_info.get("visits", 0) or 0)
        self.stats.total_clothing += int(group_info.get("clothing", 0) or 0)
        self.stats.total_members += int(group_info.get("members", 0) or 0)
        if not group_info.get("is_real_owner", True):
            self.stats.fake_owner_groups += 1

    def _matched_metrics(self, group_info: Dict[str, Any]) -> List[str]:
        """Các filter mà group đạt, kèm giá trị: 'robux 2,450>=100'."""
        matched: List[str] = []
        for key, threshold in self.filters.items():
            value = int(group_info.get(key, 0) or 0)
            if value >= threshold:
                matched.append(f"{key} {value:,}>={threshold:,}")
        return matched

    def check_filters(self, group_info: Dict[str, Any]) -> bool:
        """Kiểm tra xem group có thỏa mãn ít nhất 1 điều kiện không."""
        return bool(self._matched_metrics(group_info))

    async def send_to_webhook(
        self, group_info: Dict[str, Any], roblox_username: str
    ) -> Tuple[bool, Optional[str]]:
        """Gửi thông tin group vào Discord webhook (1 message duy nhất) với retry."""
        if not group_info or not self.check_filters(group_info):
            return False, None

        fields = [
            {
                "name": "💰 Robux",
                "value": f"{group_info['robux']:,}",
                "inline": True,
            },
            {
                "name": "⏳ Pending",
                "value": f"{group_info['pending']:,}",
                "inline": True,
            },
        ]
        if int(group_info.get("sales", 0) or 0) > 0:
            fields.append(
                {
                    "name": "🛍️ Item Sales",
                    "value": f"{int(group_info['sales']):,}",
                    "inline": True,
                }
            )
        if int(group_info.get("premium_payouts", 0) or 0) > 0:
            fields.append(
                {
                    "name": "⭐ Premium Pay",
                    "value": f"{int(group_info['premium_payouts']):,}",
                    "inline": True,
                }
            )
        fields.extend(
            [
                {
                    "name": "🎮 Games",
                    "value": f"{group_info['games']}",
                    "inline": True,
                },
                {
                    "name": "👁️ Visits",
                    "value": f"{group_info['visits']:,}",
                    "inline": True,
                },
                {
                    "name": "👕 Clothing",
                    "value": f"{group_info['clothing']}",
                    "inline": True,
                },
                {
                    "name": "👥 Members",
                    "value": f"{group_info['members']:,}",
                    "inline": True,
                },
            ]
        )

        gid = group_info["group_id"]
        title = "🎯 Group đạt filter"
        parts = " · ".join(matched_display_parts(group_info, self.filters))
        desc = f"[{gid}](https://www.roblox.com/groups/{gid}) — {parts}"
        if not group_info.get("is_real_owner", True):
            title = f"⚠️ [MẤT QUYỀN] {title}"
            desc += f"\n⚠️ **Cảnh báo:** User không phải Owner thực sự (Owner: `{group_info.get('owner_name')}`)"

        # Icon group (nếu có) — fallback avatar account khi icon bị bỏ
        # (status_embed bật) hoặc request icon thất bại.
        thumb_url = group_info.get("thumbnail") or self._avatars.get(roblox_username)

        embed = {
            "title": title,
            "description": desc,
            "fields": fields,
            "footer": {"text": f"{TOOL_VERSION} · {roblox_username}"},
            "thumbnail": {"url": thumb_url} if thumb_url else {},
            "url": f"https://roblox.com/groups/{group_info['group_id']}",
            "color": 0x00FF88,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        content = (
            f"<@{self.discord_id}> 🔥 **Valuable Group Detected!**\n**Account:** `{roblox_username}`"
            if self.discord_id
            else f"🔥 **Valuable Group Detected!**\n**Account:** `{roblox_username}`"
        )
        payload = {"username": "Group Scanner Bot", "content": content, "embeds": [embed]}

        _, error = await self._webhook_api("POST", self.webhook_url, payload)
        if error:
            return False, error

        return True, None

    async def check_and_send(
        self,
        session: aiohttp.ClientSession,
        bare_session: aiohttp.ClientSession,
        group: Dict[str, Any],
        cookie: str,
        xcsrf: str,
        roblox_username: str,
        current_user_id: int = 0,
        public_proxy: Optional[str] = None,
        queue_recheck: bool = True,
        prev_info: Optional[Dict[str, Any]] = None,
        already_sent: bool = False,
    ) -> Dict[str, Any]:
        """Kiểm tra một group, gửi webhook nếu đạt filter, trả result cho log/summary.

        Token (currency/revenue) luôn đi IP gốc qua auth session; dữ liệu public
        đi bare session — qua Tor nếu bật (public_proxy), không thì IP gốc.
        PARTIAL/SENT_PARTIAL/FAIL/WEBHOOK_FAIL được xếp vào recheck_queue để
        quét lại cuối scan. Stats + totals chỉ cập nhật 1 lần duy nhất (atomic)
        sau khi đã rõ status, tránh đếm đôi khi send_to_webhook raise giữa chừng.
        prev_info: dữ liệu lượt trước để incremental re-check (chỉ gọi nhánh lỗi).
        already_sent: group đã POST webhook ở lượt trước (SENT/SENT_PARTIAL) —
        lượt re-check chỉ cập nhật embed/stats, KHÔNG gửi message trùng.
        """
        group_id = 0
        group_name = "Unknown"

        try:
            group_id = int(group.get("group", {}).get("id") or 0)
            group_name = group.get("group", {}).get("name") or f"Group {group_id}"
            if public_proxy is None and self.proxies:
                public_proxy = self._isolated_proxy(f"pub{group_id}")

            group_info = await self.check_group_details(
                session,
                bare_session,
                group_id,
                cookie,
                xcsrf,
                current_user_id=current_user_id,
                group_name_hint=group_name,
                public_proxy=public_proxy,
                prev_info=prev_info,
            )
            errors = group_info.get("errors", [])
            matched = self._matched_metrics(group_info)

            # Xác định status trước, chưa đụng tới stats.
            sent_ok = False
            webhook_error: Optional[str] = None
            if matched:
                status_embed = self._statuses.get(roblox_username)
                if (status_embed is not None and status_embed.active) or already_sent:
                    # Group đã được ghi vào status embed / đã POST lượt trước —
                    # không gửi thêm message riêng để tránh spam trùng.
                    sent_ok, webhook_error = True, None
                else:
                    sent_ok, webhook_error = await self.send_to_webhook(
                        group_info, roblox_username
                    )
                if sent_ok:
                    status = "SENT_PARTIAL" if errors else "SENT"
                    message = "; ".join(errors[:3]) if errors else "Webhook sent"
                else:
                    status = "WEBHOOK_FAIL"
                    message = webhook_error or "Webhook failed"
            elif errors:
                status = "PARTIAL"
                message = "; ".join(errors[:3])
            else:
                status = "SKIP"
                message = "Không đạt filter"

            # Atomic commit: từ đây trở đi chỉ là sync, không await.
            self._accumulate_totals(group_info)
            self.stats.checked_success += 1
            if status in MATCHED_STATUSES:
                self.stats.matched_filters += 1
            elif status == "PARTIAL":
                self.stats.partial_checks += 1
            elif status == "SKIP":
                self.stats.skipped_filters += 1
            if status in ("SENT", "SENT_PARTIAL"):
                self.stats.webhooks_sent += 1
            elif status == "WEBHOOK_FAIL":
                self.stats.webhooks_failed += 1

            result = {
                "status": status,
                "group_info": group_info,
                "matched": matched,
                "message": message,
            }
            if status in MATCHED_STATUSES:
                # Report cuối phiên: giữ username để render matched_groups.txt.
                try:
                    self.all_matched_groups.append(
                        {
                            "username": roblox_username,
                            "status": status,
                            "group_info": group_info,
                            "message": message,
                        }
                    )
                except Exception:
                    pass
            if queue_recheck and status in ("PARTIAL", "SENT_PARTIAL", "FAIL", "WEBHOOK_FAIL"):
                # Append không được raise sau khi commit — nếu raise thì
                # run_group sẽ đếm lại → double-count.
                try:
                    self.recheck_queue.append(
                        {
                            "group": group,
                            "cookie": cookie,
                            "xcsrf": xcsrf,
                            "username": roblox_username,
                            "user_id": current_user_id,
                            "prev_result": result,
                        }
                    )
                except Exception:
                    pass
            return result

        except Exception as exc:
            self.stats.checked_failed += 1
            fail_result = {
                "status": "FAIL",
                "group_info": {
                    "group_id": group_id,
                    "group_name": group_name,
                    "robux": 0,
                    "pending": 0,
                    "sales": 0,
                    "premium_payouts": 0,
                    "games": 0,
                    "visits": 0,
                    "clothing": 0,
                    "members": 0,
                    "is_real_owner": True,
                },
                "matched": [],
                "message": f"{type(exc).__name__}: {exc}",
            }
            if queue_recheck:
                try:
                    self.recheck_queue.append(
                        {
                            "group": group,
                            "cookie": cookie,
                            "xcsrf": xcsrf,
                            "username": roblox_username,
                            "user_id": current_user_id,
                            "prev_result": fail_result,
                        }
                    )
                except Exception:
                    pass
            return fail_result

    def _exception_result(self, exc: BaseException) -> Dict[str, Any]:
        """Result FAIL cho group gây exception ngoài check_and_send."""
        self.stats.checked_failed += 1
        return {
            "status": "FAIL",
            "group_info": {
                "group_id": 0,
                "group_name": "Unknown",
                "robux": 0,
                "pending": 0,
                "sales": 0,
                "premium_payouts": 0,
                "games": 0,
                "visits": 0,
                "clothing": 0,
                "members": 0,
                "is_real_owner": True,
            },
            "matched": [],
            "message": f"{type(exc).__name__}: {exc}",
        }

    def _undo_result_stats(self, result: Dict[str, Any]) -> None:
        """Hoàn tác counters + totals của kết quả cũ trước khi re-check (tránh đếm đôi)."""
        status = result.get("status")
        info = result.get("group_info", {}) or {}

        if status == "FAIL":
            self.stats.checked_failed -= 1
        else:
            self.stats.checked_success -= 1

        if status in MATCHED_STATUSES:
            self.stats.matched_filters -= 1
            # Gỡ khỏi report matched để re-check append bản mới (tránh trùng).
            try:
                gid = int(info.get("group_id", 0) or 0)
                for idx, entry in enumerate(self.all_matched_groups):
                    if int(entry.get("group_info", {}).get("group_id", 0) or 0) == gid:
                        del self.all_matched_groups[idx]
                        break
            except Exception:
                pass
        elif status == "PARTIAL":
            self.stats.partial_checks -= 1
        elif status == "SKIP":
            self.stats.skipped_filters -= 1

        if status in ("SENT", "SENT_PARTIAL"):
            self.stats.webhooks_sent -= 1
        elif status == "WEBHOOK_FAIL":
            self.stats.webhooks_failed -= 1

        self.stats.total_robux -= int(info.get("robux", 0) or 0)
        self.stats.total_pending -= int(info.get("pending", 0) or 0)
        self.stats.total_sales -= int(info.get("sales", 0) or 0)
        self.stats.total_premium -= int(info.get("premium_payouts", 0) or 0)
        self.stats.total_games -= int(info.get("games", 0) or 0)
        self.stats.total_visits -= int(info.get("visits", 0) or 0)
        self.stats.total_clothing -= int(info.get("clothing", 0) or 0)
        self.stats.total_members -= int(info.get("members", 0) or 0)
        if not info.get("is_real_owner", True):
            self.stats.fake_owner_groups = max(0, self.stats.fake_owner_groups - 1)

    async def run_recheck_pass(self) -> None:
        """Quét lại group PARTIAL/SENT_PARTIAL/FAIL/WEBHOOK_FAIL (thường do 429).

        Mỗi lượt làm lại dùng exit IP mới (key proxy có suffix vòng), undo stats
        kết quả cũ trước khi chạy lại để không đếm đôi. SENT_PARTIAL được quét
        lại theo nhánh lỗi (_ok) — đã POST webhook thì already_sent chặn gửi trùng.
        Group vẫn lỗi sau vòng cuối được gom vào unresolved để báo cáo,
        không bị nuốt mất.
        """
        recheck_rounds = 2
        cooldown_seconds = self.recheck_cooldown
        unresolved: List[Dict[str, Any]] = []

        # Vòng re-check COOKIE lỗi mạng TRƯỚC — account hồi phục sẽ tự quét
        # group của nó (PARTIAL/FAIL đi vào recheck_queue cho các vòng sau).
        if self.failed_cookies_queue:
            cookie_entries, self.failed_cookies_queue = self.failed_cookies_queue, []
            if self.dash is not None:
                self.dash.status = "RE-CHECKING"
                self.dash.log(
                    f"RE-CHECK cookie · {len(cookie_entries)} account lỗi mạng"
                    f" · nghỉ {cooldown_seconds}s cho rate-limit nguội"
                )
                self._ui_render()
            else:
                self.console.rule(
                    gradient_text(
                        f"RE-CHECK cookie · {len(cookie_entries)} account lỗi mạng"
                        f" · nghỉ {cooldown_seconds}s cho rate-limit nguội",
                        *GRAD_WARN,
                    ),
                    style="dim yellow",
                )
            await asyncio.sleep(cooldown_seconds)
            for entry in cookie_entries:
                try:
                    # scan_cookie tự re-queue nếu vẫn lỗi ('retry') với
                    # valid_counted đã cập nhật — tránh cộng dồn 2 lần.
                    await self.scan_cookie(
                        entry["cookie"],
                        int(entry.get("index", 0)),
                        len(self.cookies),
                        recheck_entry=entry,
                    )
                except Exception as exc:
                    # KHÔNG re-queue: stats group có thể đã commit — quét lại
                    # sẽ double-count owned_groups. Giữ nguyên trạng thái failed.
                    self._ui_event(
                        f"Re-check cookie #{entry.get('index', '?')} lỗi "
                        f"({type(exc).__name__}: {exc}) — giữ trạng thái failed.",
                        f"[red]Re-check cookie #{entry.get('index', '?')} lỗi "
                        f"({type(exc).__name__}: {exc}) — giữ trạng thái failed.[/]",
                    )
            if self.failed_cookies_queue:
                leftover_cookies = ", ".join(
                    str(e.get("marker", f"cookie #{e.get('index', '?')}"))
                    for e in self.failed_cookies_queue
                )
                self._ui_event(
                    f"⚠ Vẫn còn {len(self.failed_cookies_queue)} account lỗi mạng "
                    f"sau re-check: {leftover_cookies}",
                    gradient_text(
                        f"⚠ Vẫn còn {len(self.failed_cookies_queue)} account lỗi mạng "
                        f"sau re-check: {escape(leftover_cookies)}",
                        *GRAD_WARN,
                    ),
                )

        for round_no in range(1, recheck_rounds + 1):
            if not self.recheck_queue:
                break

            entries, self.recheck_queue = self.recheck_queue, []
            if self.dash is not None:
                self.dash.status = "RE-CHECKING"
                self.dash.log(
                    f"RE-CHECK vòng {round_no}/{recheck_rounds} · "
                    f"{len(entries)} group thiếu dữ liệu · "
                    f"nghỉ {cooldown_seconds}s cho rate-limit nguội"
                )
                self._ui_render()
            else:
                self.console.rule(
                    gradient_text(
                        f"RE-CHECK vòng {round_no}/{recheck_rounds} · {len(entries)} group "
                        f"thiếu dữ liệu · nghỉ {cooldown_seconds}s cho rate-limit nguội",
                        *GRAD_WARN,
                    ),
                    style="dim yellow",
                )
            await asyncio.sleep(cooldown_seconds)

            semaphore = asyncio.Semaphore(self.concurrent_groups)

            async def recheck_one(entry: Dict[str, Any]) -> None:
                group = entry["group"]
                group_id = int(group.get("group", {}).get("id") or 0)
                prev_result = entry.get("prev_result") or {}
                prev_group_info = prev_result.get("group_info")
                already_sent = prev_result.get("status") in ("SENT", "SENT_PARTIAL")
                self._undo_result_stats(prev_result)
                self.stats.rechecked_groups += 1

                # Public data qua exit mới nếu Tor bật; token vẫn IP gốc.
                public_proxy = self._isolated_proxy(f"pub{group_id}r{round_no}")
                try:
                    async with semaphore:
                        async with aiohttp.ClientSession(
                            cookies={".ROBLOSECURITY": entry["cookie"]}
                        ) as auth_session, aiohttp.ClientSession() as bare_session:
                            result = await self.check_and_send(
                                auth_session,
                                bare_session,
                                group,
                                entry["cookie"],
                                entry["xcsrf"],
                                entry["username"],
                                current_user_id=entry.get("user_id", 0),
                                public_proxy=public_proxy,
                                queue_recheck=False,
                                prev_info=prev_group_info,
                                already_sent=already_sent,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = self._exception_result(exc)
                    try:
                        _gid = int(group.get("group", {}).get("id") or 0)
                        _gname = group.get("group", {}).get("name") or f"Group {_gid}"
                        result["group_info"]["group_id"] = _gid
                        result["group_info"]["group_name"] = _gname
                    except Exception:
                        pass

                self._log_group_result(result)
                # Group phục hồi được ghi thẳng vào status embed của
                # account (totals delta với prev_result — không đếm đôi).
                await self._status_record(
                    entry["username"],
                    result,
                    recheck=True,
                    prev_result=entry.get("prev_result"),
                )
                if result["status"] in ("PARTIAL", "SENT_PARTIAL", "FAIL", "WEBHOOK_FAIL"):
                    if round_no < recheck_rounds:
                        self.recheck_queue.append(
                            {**entry, "prev_result": result}
                        )
                    else:
                        try:
                            result["_username"] = entry.get("username", "?")
                        except Exception:
                            pass
                        unresolved.append(result)
                else:
                    self.stats.recheck_recovered += 1

            await asyncio.gather(
                *(recheck_one(entry) for entry in entries),
                return_exceptions=True,
            )

        pending_left = list(self.recheck_queue)
        self.recheck_queue = []
        all_unresolved_results: List[Dict[str, Any]] = list(unresolved)
        for e in pending_left:
            try:
                prev = e.get("prev_result") or {}
                if "_username" not in prev:
                    prev["_username"] = e.get("username", "?")
                all_unresolved_results.append(prev)
            except Exception:
                continue
        # Lưu cho report ZIP cuối phiên (giữ username trong _username).
        try:
            self.unresolved_groups = list(all_unresolved_results)
        except Exception:
            pass
        if all_unresolved_results:
            leftover = ", ".join(
                str(r.get("group_info", {}).get("group_name", "?"))
                for r in all_unresolved_results
            )
            self._ui_event(
                f"⚠ Vẫn còn {len(all_unresolved_results)} group thiếu dữ liệu sau "
                f"{recheck_rounds} vòng re-check: {leftover}",
                gradient_text(
                    f"⚠ Vẫn còn {len(all_unresolved_results)} group thiếu dữ liệu sau "
                    f"{recheck_rounds} vòng re-check: {escape(leftover)}",
                    *GRAD_WARN,
                ),
            )

        # Delta phục hồi — đối chiếu với log từng account in trước đó.
        if self.stats.accounts_recovered or self.stats.recheck_recovered:
            self._ui_event(
                f"✔ Phục hồi sau re-check: {self.stats.accounts_recovered:,} account · "
                f"{self.stats.recheck_recovered:,} group",
                gradient_text(
                    f"✔ Phục hồi sau re-check: {self.stats.accounts_recovered:,} account · "
                    f"{self.stats.recheck_recovered:,} group "
                    f"(log từng account phía trên là số liệu TRƯỚC re-check)",
                    *GRAD_OK,
                ),
            )

        # Chốt FINISHED cho mọi account đang RE-CHECKING — embed đồng bộ
        # với terminal: re-check xong thì cả hai cùng báo xong.
        for st in list(self._statuses.values()):
            if st.active and st.rechecking:
                try:
                    await st.finish_recheck()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._ui_event(
                        f"Không chốt status embed cuối cho {st.username}: "
                        f"{type(exc).__name__}",
                        f"[dim]⚠ Không chốt status embed cuối cho "
                        f"{escape(st.username)}: {type(exc).__name__}[/]",
                    )

        if self.dash is not None:
            self.dash.status = "RUNNING"
            self._ui_render()

    # ------------------------------------------------------------------
    # Scan orchestration
    # ------------------------------------------------------------------

    def _build_report_zip(self) -> bytes:
        """Đóng gói scan_report.zip trong RAM (1 group / 1 dòng, không kẻ bảng).

        matched_groups.txt: `link - tên acc - lý do matched` (sort giảm dần
        theo robux+pending, robux, pending, visits, clothing, games).
        unresolved_groups.txt: `link - tên acc`.
        Trống -> 1 dòng ngoặc đơn để Discord preview gọn, không vỡ khung.
        """
        try:
            sorted_matched = sorted(
                self.all_matched_groups,
                key=lambda e: (
                    int(e.get("group_info", {}).get("robux", 0) or 0)
                    + int(e.get("group_info", {}).get("pending", 0) or 0),
                    int(e.get("group_info", {}).get("robux", 0) or 0),
                    int(e.get("group_info", {}).get("pending", 0) or 0),
                    int(e.get("group_info", {}).get("visits", 0) or 0),
                    int(e.get("group_info", {}).get("clothing", 0) or 0),
                    int(e.get("group_info", {}).get("games", 0) or 0),
                ),
                reverse=True,
            )
        except Exception:
            sorted_matched = list(self.all_matched_groups)

        m_lines: List[str] = []
        thresholds = dict(self.filters)
        for entry in sorted_matched:
            try:
                info = entry.get("group_info", {}) or {}
                acc = str(entry.get("username", "?")).strip() or "?"
                gid = int(info.get("group_id", 0) or 0)
                if gid <= 0:
                    continue
                parts = " · ".join(matched_display_parts(info, thresholds))
                m_lines.append(
                    f"https://www.roblox.com/groups/{gid} - {acc} - {parts}"
                )
            except Exception:
                continue
        if not m_lines:
            m_lines.append("(khong co group dat filter)")
        matched_text = "\n".join(m_lines) + "\n"

        try:
            unresolved = list(self.unresolved_groups)
        except Exception:
            unresolved = []
        u_lines: List[str] = []
        for res in unresolved:
            try:
                info = res.get("group_info", {}) or {}
                acc = str(res.get("_username", "?")).strip() or "?"
                gid = int(info.get("group_id", 0) or 0)
                if gid <= 0:
                    continue
                u_lines.append(f"https://www.roblox.com/groups/{gid} - {acc}")
            except Exception:
                continue
        if not u_lines:
            u_lines.append("(khong co group loi)")
        unresolved_text = "\n".join(u_lines) + "\n"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("matched_groups.txt", matched_text)
            zf.writestr("unresolved_groups.txt", unresolved_text)
        return buf.getvalue()

    async def send_grand_totals_webhook(self) -> Tuple[bool, Optional[str]]:
        """Gửi webhook tổng hợp toàn bộ số liệu của tất cả account đã quét.

        Embed dùng 1 khối ```yaml``` thẳng hàng (cùng style AccountStatus)
        gọn 6 dòng cốt lõi: accounts, groups, matched, robux, pending,
        combined. Chỉ nối thêm recheck/partial/failed/fake_own vào cuối khi
        giá trị > 0 (dấu ':' thẳng cột 8). Danh sách group lỗi KHÔNG nhồi
        vào embed — đính kèm 1 file scan_report.zip (matched_groups.txt +
        unresolved_groups.txt) tạo trong RAM.
        """
        total_robux = self.stats.total_robux
        total_pending = self.stats.total_pending
        combined = total_robux + total_pending

        ping_prefix = f"<@{self.discord_id}> " if self.discord_id else ""
        content = (
            f"{ping_prefix}📊 **Scan Completed — Grand Totals**\n"
            f"Đã quét **{self.stats.valid_accounts:,}** account · "
            f"**{self.stats.checked_success + self.stats.checked_failed:,}** group được check."
        )

        s = self.stats
        body_lines = [
            f"accounts: {s.valid_accounts:,}",
            f"groups  : {s.owned_groups:,}",
            f"matched : {s.matched_filters:,}",
            f"robux   : {total_robux:,}",
            f"pending : {total_pending:,}",
            f"combined: {combined:,}",
        ]
        # Chỉ nối thêm khi > 0 để scan sạch giữ đúng 6 dòng chuẩn.
        if s.rechecked_groups > 0:
            body_lines.append(f"recheck : {s.rechecked_groups:,}")
        if s.partial_checks > 0:
            body_lines.append(f"partial : {s.partial_checks:,}")
        if s.checked_failed > 0:
            body_lines.append(f"failed  : {s.checked_failed:,}")
        if s.fake_owner_groups > 0:
            body_lines.append(f"fake_own: {s.fake_owner_groups:,}")
        description = "```yaml\n" + "\n".join(body_lines) + "\n```"

        embed = {
            "username": "Group Scanner Bot",
            "content": content,
            "embeds": [
                {
                    "title": "💎 Grand Totals — Tất cả account",
                    "description": description,
                    "footer": {
                        "text": f"{TOOL_VERSION} · Cookies: {self.stats.cookies_loaded} · Invalid: {self.stats.invalid_cookies}"
                    },
                    "color": 0xFFD700,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

        try:
            zip_bytes = self._build_report_zip()
        except Exception:
            zip_bytes = b""
        self._last_report_zip = zip_bytes
        file_attachment: Optional[Tuple[str, bytes]] = (
            ("scan_report.zip", zip_bytes) if zip_bytes else None
        )
        try:
            if file_attachment is not None:
                _, error = await self._webhook_api(
                    "POST", self.webhook_url, embed, file_attachment=file_attachment
                )
            else:
                _, error = await self._webhook_api("POST", self.webhook_url, embed)
        except TypeError:
            # Tương thích mock test cũ fake_api(method, url, payload=None).
            _, error = await self._webhook_api("POST", self.webhook_url, embed)
        if error:
            return False, error

        return True, None

    def _drop_failed_marker(self, marker: str) -> None:
        """Xóa 1 entry trong failed_accounts (idempotent)."""
        try:
            self.stats.failed_accounts.remove(marker)
        except ValueError:
            pass

    async def scan_cookie(
        self,
        cookie: str,
        index: int,
        total: int,
        recheck_entry: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Scan tất cả groups của 1 cookie, chỉ lấy group rank 255.

        Token của account luôn đi IP gốc (không qua Tor) để không bị flag;
        dữ liệu public của từng group có thể qua Tor nếu bật.

        Trả về: 'ok' (xác thực + lấy groups thành công), 'retry' (lỗi tạm
        thời — TỰ xếp lại vào failed_cookies_queue với valid_counted mới),
        'dead' (cookie không hợp lệ — không quét lại).
        recheck_entry: lượt re-check — khi 'ok' hoàn tác account_failures +
        xóa marker đã cộng ở lần đầu, KHÔNG cộng stats lần 2.
        """
        is_recheck = recheck_entry is not None
        if is_recheck:
            marker = str(recheck_entry.get("marker") or f"cookie #{index}")
            valid_counted = bool(recheck_entry.get("valid_counted"))
        else:
            marker = f"cookie #{index}"
            valid_counted = False
        label = f"Account {index}/{total}" + (" · re-check" if is_recheck else "")
        if self.dash is not None:
            self.dash.status = "RE-CHECKING" if is_recheck else "RUNNING"
            self.dash.begin_account("N/A", 0, index, total, 0)
            self._ui_render()
        else:
            self.console.rule(gradient_text(label, *GRAD_TITLE), style="dim cyan")

        def _queue_fail() -> None:
            self.failed_cookies_queue.append(
                {
                    "cookie": cookie,
                    "index": index,
                    "marker": marker,
                    "valid_counted": valid_counted,
                }
            )

        # Status embed 1 message/account — tạo sau khi có username/groups.
        status: Optional[AccountStatus] = None

        # 1 session auth duy nhất tái sử dụng cho auth + csrf + groups + currency.
        async with aiohttp.ClientSession(cookies={".ROBLOSECURITY": cookie}) as session:
            # Account-level mang token: IP gốc, không proxy.
            # Tên http_status KHÁC biến status (AccountStatus) phía dưới —
            # trùng tên sẽ ghi đè int vào AccountStatus -> crash '.active'.
            cookie_data, http_status = await self.check_cookie(cookie, session=session)
            if not cookie_data:
                if http_status in (401, 403):
                    if is_recheck:
                        # Cookie hết hạn giữa chừng: chuyển failed → invalid.
                        self.stats.account_failures = max(
                            0, self.stats.account_failures - 1
                        )
                        self._drop_failed_marker(marker)
                    self.stats.invalid_cookies += 1
                    self._ui_event(
                        f"Cookie #{index} không hợp lệ (HTTP {http_status}) — bỏ qua.",
                        f"[yellow]Cookie #{index} không hợp lệ (HTTP {http_status}) — bỏ qua.[/]",
                    )
                    return "dead"
                # Lỗi mạng/server: KHÔNG kết luận cookie invalid — account có thể
                # còn group chưa được check, phải hiện rõ để không sót group.
                if not is_recheck:
                    self.stats.account_failures += 1
                    self.stats.failed_accounts.append(marker)
                _queue_fail()
                self._ui_event(
                    f"Không xác thực được cookie #{index} "
                    f"(HTTP {http_status or 'network error'}) — group CHƯA được check.",
                    f"[red]Không xác thực được cookie #{index} "
                    f"(HTTP {http_status or 'network error'}) — group của account này CHƯA được check.[/]",
                )
                return "retry"

            if not valid_counted:
                self.stats.valid_accounts += 1
                valid_counted = True
            user_id = cookie_data["id"]
            name = cookie_data["name"]
            if not is_recheck:
                marker = name  # các lỗi sau auth dùng name làm marker

            xcsrf = await self.get_csrf_token(cookie, session=session)
            if not xcsrf:
                if not is_recheck:
                    self.stats.account_failures += 1
                    self.stats.failed_accounts.append(marker)
                _queue_fail()
                self._ui_event(
                    f"Không lấy được CSRF token cho {name} — group CHƯA được check.",
                    f"[red]Không lấy được CSRF token cho[/] [bold]{escape(name)}[/] — "
                    "group của account này CHƯA được check.",
                )
                return "retry"

            groups, error = await self.get_user_groups(session, user_id)
            if error:
                if not is_recheck:
                    self.stats.account_failures += 1
                    self.stats.failed_accounts.append(marker)
                _queue_fail()
                self._ui_event(
                    f"Không lấy được danh sách groups của {name}: {error} — "
                    "group CHƯA được check.",
                    f"[red]Không lấy được danh sách groups của[/] [bold]{escape(name)}[/]: "
                    f"{error} — group của account này CHƯA được check.",
                )
                return "retry"

            # Account xử lý thành công — lượt re-check: hoàn tác stats thất bại.
            if is_recheck:
                self.stats.account_failures = max(0, self.stats.account_failures - 1)
                self._drop_failed_marker(marker)
                self.stats.accounts_recovered += 1
                self._ui_event(
                    f"Phục hồi account {name} — quét tiếp group.",
                    f"[green]✓ Phục hồi account[/] [bold]{escape(name)}[/]"
                    f" [green]— quét tiếp group.[/]",
                )

            owned_groups = [
                group for group in groups if group.get("role", {}).get("rank") == 255
            ]
            self.stats.owned_groups += len(owned_groups)

            if self.dash is not None:
                self.dash.begin_account(name, user_id, index, total, len(owned_groups))
                self._ui_event(
                    f"Account {name} · ID {user_id} · {len(owned_groups)} owner groups."
                )
            else:
                self.console.print(
                    f"  ✓ [bold]{escape(name)}[/] · User ID [cyan]{user_id}[/] · "
                    f"Owner groups [bold green]{len(owned_groups)}[/]"
                )

            if self.status_embeds_enabled:
                # 1 request avatar/account cho thumbnail embed — mọi lỗi đều
                # suy biến về None, không bao giờ chặn luồng quét group.
                avatar_url: Optional[str] = None
                try:
                    avatar_url = await self.get_user_avatar(session, user_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    avatar_url = None
                status = AccountStatus(
                    self,
                    index,
                    total,
                    name,
                    user_id,
                    len(owned_groups),
                    avatar_url=avatar_url,
                )
                self._statuses[name] = status
                self._avatars[name] = avatar_url
                # 0 group owner → mở embed luôn ở trạng thái FINISHED (1 request).
                if not await status.open(final=not owned_groups):
                    status = None  # tạo thất bại → fallback webhook từng group

            if not owned_groups:
                return "ok"

            # Session KHÔNG cookie cho 4 endpoint public — token không rời IP gốc.
            async with aiohttp.ClientSession() as bare_session:
                results: List[Dict[str, Any]] = []
                counters = {"matched": 0, "failed": 0, "sent": 0}

                # Classic mode có Rich Progress; dashboard mode hiển thị qua Group Queue.
                progress: Optional[Progress] = None
                if self.dash is None:
                    progress = Progress(
                        SpinnerColumn(style="cyan"),
                        TextColumn("[bold cyan]{task.description}"),
                        BarColumn(bar_width=None),
                        TaskProgressColumn(),
                        TimeElapsedColumn(),
                        TimeRemainingColumn(),
                        console=self.console,
                        transient=True,
                    )

                def _bump_done() -> None:
                    if self.dash is not None:
                        self.dash.groups_done = min(
                            self.dash.groups_done + 1, self.dash.groups_total
                        )

                async def run_group(group: Dict[str, Any]) -> Dict[str, Any]:
                    """Trả về dict cho MỌI group (trừ CancelledError) — stats chỉ
                    commit đúng 1 lần bên trong check_and_send; nếu exception lọt
                    ra ngoài thì đếm qua _exception_result, không double-count."""
                    try:
                        async with semaphore:
                            result = await self.check_and_send(
                                session,
                                bare_session,
                                group,
                                cookie,
                                xcsrf,
                                name,
                                current_user_id=user_id,
                            )
                    except asyncio.CancelledError:
                        raise
                    except (KeyboardInterrupt, SystemExit):
                        # Không nuốt tín hiệu dừng chương trình.
                        raise
                    except BaseException as exc:
                        # check_and_send chỉ raise TRƯỚC khi commit stats
                        # (khối except bên trong luôn return) → đếm 1 lần tại đây.
                        result = self._exception_result(exc)
                        try:
                            self._log_group_result(result)
                            _bump_done()
                            if progress is not None:
                                progress.advance(task_id)
                        except Exception:
                            pass
                        await self._status_record(name, result)
                        return result

                    # Log/counter không được làm mất result đã commit stats:
                    # lỗi ở đây chỉ log, vẫn trả result gốc.
                    try:
                        # In qua cùng console với Progress để rich đẩy dòng lên trên live bar.
                        self._log_group_result(result)
                        _bump_done()

                        if result["status"] in MATCHED_STATUSES:
                            counters["matched"] += 1
                        if result["status"] in ("SENT", "SENT_PARTIAL"):
                            counters["sent"] += 1
                        if result["status"] == "FAIL":
                            counters["failed"] += 1

                        if progress is not None:
                            progress.update(
                                task_id,
                                description=(
                                    f"Groups ✓{counters['matched']} ✗{counters['failed']}"
                                    f" →{counters['sent']}"
                                ),
                            )
                            progress.advance(task_id)
                    except Exception as log_exc:
                        self._ui_event(
                            f"Log progress lỗi ({type(log_exc).__name__}) nhưng giữ nguyên kết quả group.",
                            f"[red]Log progress lỗi ({type(log_exc).__name__}) "
                            f"nhưng giữ nguyên kết quả group.[/]",
                        )
                        if progress is not None:
                            try:
                                progress.advance(task_id)
                            except Exception:
                                pass
                    # PATCH status embed NGOÀI semaphore: throttle chờ không giữ slot group.
                    await self._status_record(name, result)
                    return result

                # Semaphore thay vì chia batch: group xong là group kế vào ngay.
                semaphore = asyncio.Semaphore(self.concurrent_groups)
                ctx = progress if progress is not None else contextlib.nullcontext()
                with ctx:
                    task_id = None
                    if progress is not None:
                        task_id = progress.add_task(
                            "Groups ✓0 ✗0 →0", total=len(owned_groups)
                        )
                    gathered = await asyncio.gather(
                        *(run_group(group) for group in owned_groups),
                        return_exceptions=True,
                    )

                # gather bảo toàn thứ tự → results khớp thứ tự owned_groups.
                # run_group đã bọc mọi lỗi nên nhánh BaseException ở đây chỉ
                # là phòng thủ (result chưa từng commit) — vẫn chỉ đếm 1 lần.
                for item in gathered:
                    if isinstance(item, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise item
                    if isinstance(item, BaseException):
                        fail_result = self._exception_result(item)
                        results.append(fail_result)
                        try:
                            self._log_group_result(fail_result)
                            await self._status_record(name, fail_result)
                        except Exception:
                            pass
                    else:
                        results.append(item)

                self.render_account_summary(name, results)

                if status is not None and status.active:
                    try:
                        # Còn group của account này nằm trong recheck_queue →
                        # embed phải chờ, tuyệt đối không báo FINISHED sớm.
                        pending_recheck = any(
                            e.get("username") == name for e in self.recheck_queue
                        )
                        if pending_recheck:
                            await status.mark_rechecking(results)
                        else:
                            await status.finalize(results)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._ui_event(
                            f"Không gửi được status embed cuối cho {name}: {type(exc).__name__}",
                            f"[dim]⚠ Không gửi được status embed cuối cho "
                            f"{escape(name)}: {type(exc).__name__}[/]",
                        )

            return "ok"

    async def start(self):
        """Bắt đầu scan tất cả cookies."""
        interrupted = False
        try:
            try:
                self.show_header()
                self._announce_tor()

                # Đọc cookies TRƯỚC khi hỏi filter + bootstrap Tor:
                # file rỗng thì thoát ngay, không bắt user chờ vô ích.
                self.cookies = self.load_cookies_from_file()
                if not self.cookies:
                    self._ui_event(
                        "Không tìm thấy cookie hợp lệ trong cookies.txt.",
                        "[red]Không tìm thấy cookie hợp lệ trong cookies.txt.[/]",
                    )
                    return

                self.setup_filters()
                # Tor init sau khi trả lời filter: user không phải chờ bootstrap.
                self._setup_tor()

                for index, cookie in enumerate(self.cookies, 1):
                    try:
                        await self.scan_cookie(cookie, index, len(self.cookies))
                    except Exception as exc:
                        # Một account crash không được làm dừng các account còn lại.
                        self.stats.account_failures += 1
                        self.stats.failed_accounts.append(f"cookie #{index}")
                        self._ui_event(
                            f"Account #{index} gặp lỗi không xử lý được: {type(exc).__name__}: {exc}",
                            f"[red]Account #{index} gặp lỗi không xử lý được:[/] "
                            f"{type(exc).__name__}: {exc}",
                        )

                # Quét lại các group thiếu dữ liệu (429...) với exit IP mới.
                await self.run_recheck_pass()
            except KeyboardInterrupt:
                interrupted = True
                # Best-effort: chốt embed các account đang RE-CHECKING để
                # không kẹt 🔄 sau khi chương trình dừng (lỗi chỉ nuốt nội bộ).
                for st in list(self._statuses.values()):
                    if st.active and st.rechecking:
                        try:
                            await st.finish_recheck()
                        except Exception:
                            pass
                if self.dash is not None:
                    self.dash.status = "STOPPED"
                    self.dash.log("Ctrl+C — dừng scan, in summary một phần.")
                    self._ui_render()
                self.console.print(
                    "\n[yellow]⚠ Ctrl+C — dừng scan, in summary một phần.[/]"
                )

            if interrupted:
                return

            # Tổng kết chi tiết đã nằm trên Embed Grand Totals của Discord —
            # terminal giữ Dashboard gọn, chỉ log kết quả gửi webhook.
            self._ui_event(
                "Đang gửi webhook Grand Totals...",
                "[cyan]→ Đang gửi webhook Grand Totals...[/]",
            )
            sent, error = await self.send_grand_totals_webhook()
            if sent:
                self._ui_event(
                    "✓ Scan completed · Đã gửi webhook Grand Totals thành công.",
                    "[green]✓ Scan completed · Đã gửi webhook Grand Totals thành công.[/]",
                )
            else:
                fail_msg = (
                    f"✗ Scan completed · Gửi webhook Grand Totals thất bại: "
                    f"{error or 'unknown error'}"
                )
                self._ui_event(fail_msg, f"[red]{fail_msg}[/]")

            if self.dash is not None:
                self.dash.status = "STOPPED"
                self._ui_render()
            else:
                self._ui_section("Completed", GRAD_OK, "dim green")
                self.render_summary()
        finally:
            # Luôn dọn Tor process: kết thúc bình thường, Ctrl+C hay lỗi.
            self._shutdown_tor()


def load_config():
    """Load config từ config.json."""
    config_path = BASE_DIR / "config.json"

    if not config_path.exists():
        config = {
            "webhook_url": "YOUR_WEBHOOK_URL_HERE",
            "discord_id": "YOUR_DISCORD_ID_HERE",
            "status_embed": True,
            "terminal_ui": "dashboard",
        }
        with config_path.open("w", encoding="utf-8") as file:
            json.dump(config, file, indent=4)
        print("[!] Đã tạo file config.json, vui lòng điền webhook_url và discord_id!")
        return None

    try:
        with config_path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[!] Không đọc được config.json: {exc}")
        return None


def parse_tor_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Đọc + validate section 'tor' của config: enabled / amount / port.

    Thiếu section hoặc key sai → dùng mặc định và thông báo, KHÔNG ghi đè config.
    """
    defaults = {"enabled": False, "amount": 3, "port": 9050}
    settings = dict(defaults)

    raw = config.get("tor")
    if raw is None:
        return settings
    if not isinstance(raw, dict):
        print("[!] config 'tor' phải là object {enabled, amount, port} — dùng mặc định (TẮT).")
        return settings

    settings["enabled"] = raw.get("enabled", defaults["enabled"])
    if not isinstance(settings["enabled"], bool):
        print("[!] tor.enabled phải là true/false — dùng false.")
        settings["enabled"] = False

    try:
        amount = int(raw.get("amount", defaults["amount"]))
    except (TypeError, ValueError):
        print(f"[!] tor.amount không hợp lệ — dùng {defaults['amount']}.")
        amount = defaults["amount"]
    if amount < 1:
        print("[!] tor.amount tối thiểu 1 — dùng 1.")
        amount = 1
    elif amount > 100:
        print("[!] tor.amount tối đa 100 — dùng 100.")
        amount = 100
    settings["amount"] = amount

    try:
        port = int(raw.get("port", defaults["port"]))
    except (TypeError, ValueError):
        print(f"[!] tor.port không hợp lệ — dùng {defaults['port']}.")
        port = defaults["port"]
    if not (1024 <= port <= 65535):
        print(f"[!] tor.port ngoài khoảng 1024-65535 — dùng {defaults['port']}.")
        port = defaults["port"]
    settings["port"] = port

    return settings


def parse_cooldowns(config: Dict[str, Any]) -> Tuple[Optional[Dict[str, float]], float]:
    """Đọc config 'cooldowns' (tùy chọn): {domain: giây} + 'cooldown_default'."""
    default_raw = config.get("cooldown_default", 0.5)
    try:
        default = max(float(default_raw), 0.0)
    except (TypeError, ValueError):
        print("[!] cooldown_default không hợp lệ — dùng 0.5.")
        default = 0.5

    raw = config.get("cooldowns")
    if raw is None:
        return None, default
    if not isinstance(raw, dict):
        print("[!] config 'cooldowns' phải là object {domain: seconds} — dùng mặc định.")
        return None, default

    cleaned: Dict[str, float] = {}
    for domain, seconds in raw.items():
        try:
            cleaned[str(domain)] = max(float(seconds), 0.0)
        except (TypeError, ValueError):
            print(f"[!] cooldowns['{domain}'] không hợp lệ — bỏ qua key này.")
    return cleaned, default


async def main():
    config = load_config()
    if not config:
        return

    webhook_url = config.get("webhook_url")
    discord_id = config.get("discord_id")

    if not webhook_url or webhook_url == "YOUR_WEBHOOK_URL_HERE":
        print("[!] Vui lòng cấu hình webhook_url trong config.json!")
        return
    if discord_id == "YOUR_DISCORD_ID_HERE":
        print("[!] discord_id chưa cấu hình — gửi webhook không kèm mention.")
        discord_id = None

    tor_settings = parse_tor_settings(config)
    cooldowns, cooldown_default = parse_cooldowns(config)

    # Status embed 1 message/account (mặc định bật) — tắt = webhook từng group.
    status_embed = config.get("status_embed", True)
    if not isinstance(status_embed, bool):
        print("[!] status_embed phải là true/false — dùng true.")
        status_embed = True

    # Terminal UI: "dashboard" (Clean Minimalist, mặc định) | "classic" (Rich log cũ).
    terminal_ui = str(config.get("terminal_ui", "dashboard")).lower().strip()
    if terminal_ui not in ("dashboard", "classic"):
        print("[!] terminal_ui phải là 'dashboard' hoặc 'classic' — dùng 'dashboard'.")
        terminal_ui = "dashboard"

    scanner = GroupScanner(
        webhook_url,
        discord_id,
        tor_settings=tor_settings,
        cooldowns=cooldowns,
        cooldown_default=cooldown_default,
        status_embed=status_embed,
        terminal_ui=terminal_ui,
    )
    await scanner.start()


if __name__ == "__main__":
    asyncio.run(main())
