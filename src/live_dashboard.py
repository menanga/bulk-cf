"""Optional Rich live dashboard for Cloudflare bulk runs.

Inspired by ZemCFLare's Rich dashboard, but kept dependency-light and safe:
if Rich is unavailable, callers can keep normal console output.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock


try:
    from rich import box
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
except Exception:  # pragma: no cover - optional dependency
    HAS_RICH = False
    Console = Live = Panel = Table = box = None  # type: ignore


STAGE_PERCENT = {
    "queued": 0,
    "signup": 15,
    "turnstile": 30,
    "email_verify": 55,
    "token": 75,
    "validate": 90,
    "done": 100,
    "failed": 100,
}


@dataclass
class WorkerView:
    worker_id: int
    index: int = 0
    email: str = ""
    stage: str = "queued"
    status: str = "Idle"
    success: bool | None = None


@dataclass
class DashboardState:
    total: int
    workers: int
    start_time: float = field(default_factory=time.time)
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    worker_views: dict[int, WorkerView] = field(default_factory=dict)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    lock: Lock = field(default_factory=Lock)

    def __post_init__(self):
        for i in range(1, self.workers + 1):
            self.worker_views[i] = WorkerView(worker_id=i)

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)

    def update(self, worker_id: int, stage: str, status: str, email: str = "", index: int = 0) -> None:
        with self.lock:
            view = self.worker_views.setdefault(worker_id, WorkerView(worker_id=worker_id))
            view.stage = stage
            view.status = status
            if email:
                view.email = email
            if index:
                view.index = index
            self.logs.append(f"W{worker_id}: {status}" + (f" — {email}" if email else ""))

    def finish(self, worker_id: int, success: bool, status: str) -> None:
        with self.lock:
            self.completed += 1
            if success:
                self.succeeded += 1
            else:
                self.failed += 1
            view = self.worker_views.setdefault(worker_id, WorkerView(worker_id=worker_id))
            view.stage = "done" if success else "failed"
            view.status = status
            view.success = success
            self.logs.append(f"W{worker_id}: {'OK' if success else 'FAIL'} — {status}")


def _bar(percent: int, width: int = 18) -> str:
    filled = max(0, min(width, int(width * percent / 100)))
    return "█" * filled + "░" * (width - filled)


class LiveDashboard:
    """Context manager around Rich Live; no-op if Rich unavailable or disabled."""

    def __init__(self, state: DashboardState, enabled: bool = True):
        self.state = state
        self.enabled = enabled and HAS_RICH
        self.console = Console(highlight=False) if self.enabled else None
        self.live = None

    def __enter__(self):
        if self.enabled:
            self.live = Live(self.render(), console=self.console, refresh_per_second=4)
            self.live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.live:
            self.live.update(self.render(), refresh=True)
            self.live.__exit__(exc_type, exc, tb)

    def refresh(self) -> None:
        if self.live:
            self.live.update(self.render(), refresh=True)

    def render(self):
        if not self.enabled:
            return None
        elapsed = int(time.time() - self.state.start_time)
        with self.state.lock:
            total = max(self.state.total, 1)
            pct = int(self.state.completed * 100 / total)

            table = Table.grid(expand=True)
            table.add_column(ratio=1)
            table.add_row(
                f"[bold cyan]☁️ Cloudflare Auto Signup v1 — LIVE[/bold cyan]  "
                f"[magenta]Elapsed: {elapsed}s[/magenta]\n"
                f"Overall [{_bar(pct)}] {pct}%  "
                f"[green]✔ {self.state.succeeded}[/green]  "
                f"[red]✘ {self.state.failed}[/red]  "
                f"({self.state.completed}/{self.state.total})"
            )

            worker_table = Table(show_header=True, header_style="bold white", box=box.SIMPLE_HEAVY)
            worker_table.add_column("Worker")
            worker_table.add_column("Account")
            worker_table.add_column("Stage")
            worker_table.add_column("Progress")
            worker_table.add_column("Status")
            for wid, view in sorted(self.state.worker_views.items()):
                wpct = STAGE_PERCENT.get(view.stage, 0)
                style = "green" if view.success is True else "red" if view.success is False else "yellow"
                worker_table.add_row(
                    f"W{wid}",
                    f"{view.index}/{self.state.total} {view.email}" if view.email else "Idle",
                    view.stage,
                    f"[{style}]{_bar(wpct, 10)}[/] {wpct}%",
                    view.status,
                )
            table.add_row(worker_table)

            log_lines = "\n".join(self.state.logs) if self.state.logs else "Menunggu aktivitas..."
            table.add_row(f"[bold]Logs[/bold]\n{log_lines}")

        return Panel(table, title="[bold]Automation Dashboard[/bold]", border_style="cyan")
