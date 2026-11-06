"""Concurrency budgets are derived from the real board, including after restart."""

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("hermes_cli.kanban_db", reason="needs a Hermes Agent checkout")

from hermes_worker_runtime import hermes_api as h
from hermes_worker_runtime.config import parse_config
from hermes_worker_runtime.scheduler import Scheduler

from conftest import make_task


@pytest.mark.parametrize("split_boards, boards", [
    (False, "all"), (True, "all"), (True, ["default", "infra"]),
])
def test_global_cap_across_lanes_and_boards(board, monkeypatch, split_boards, boards):
    config = parse_config({"boards": boards, "max_concurrent_runs": 2, "lanes": {
        name: {"assignee": f"wr:{name}", "adapter": "command", "command": ["true"],
               "concurrency": 2} for name in ("a", "b")}})
    # Exercise real claims without starting supervisors that could finish mid-tick.
    monkeypatch.setattr(Scheduler, "_spawn", lambda *args: SimpleNamespace(pid=os.getpid()))
    monkeypatch.setattr(h, "record_worker_pid", lambda *args: True)
    if split_boards:
        h.kb.create_board("infra")
    other = h.connect("infra" if split_boards else "default")
    try:
        existing = make_task(other, assignee="wr:b")
        run = h.claim_task(other, existing, claimer=h.new_claimer(), ttl_seconds=60)
        a = make_task(board, assignee="wr:a")
        b = make_task(board, assignee="wr:b")
        sched = Scheduler(config)
        first = sched.tick()
        assert [c[1] for c in first.claimed] == [a]
        assert b in first.capped and h.get_task(board, b).status == "ready"
        assert sched.tick().claimed == []
        assert h.complete(other, existing, run_id=run.current_run_id, summary="done", metadata={})
        # A fresh scheduler must count the surviving run and see the freed slot.
        restarted = Scheduler(config)
        assert [c[1] for c in restarted.tick().claimed] == [b]
        assert h.get_task(board, a).status == h.get_task(board, b).status == "running"
    finally:
        other.close()
