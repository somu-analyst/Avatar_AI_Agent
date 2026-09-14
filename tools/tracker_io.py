#!/usr/bin/env python
"""Backlog/ask tracker for Krishna -- docs/IDEA_TRACKER.xlsx.

Same convention as the NYSE_DATA and job-search-copilot trackers: every
ask gets logged with a status, so there's one source of truth for what's
pending/done/dropped instead of relying on chat history. BACKLOG.md stays
as the narrative record (the WHY behind decisions, which a spreadsheet cell
can't hold); this spreadsheet is the scannable status list.

Usage:
    from tools import tracker_io
    tracker_io.add("Add true one-page resume trimming", detail="...", status="done")
    tracker_io.update(7, status="done", notes="shipped in 42f6ec2")
    tracker_io.list_pending()
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

import pandas as pd

PATH = Path(__file__).resolve().parent.parent / "docs" / "IDEA_TRACKER.xlsx"
COLUMNS = ["ID", "Date", "Ask", "Detail", "Status", "Notes"]
STATUSES = ("pending", "in_progress", "done", "waiting_on_user", "dropped")


def _load() -> pd.DataFrame:
    if PATH.exists():
        return pd.read_excel(PATH, dtype={"ID": int})
    return pd.DataFrame(columns=COLUMNS)


def _save(df: pd.DataFrame) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(PATH, index=False)


def add(ask: str, detail: str = "", status: str = "pending", notes: str = "") -> int:
    """Allocates the next ID at write time (max(ID)+1), same defense the
    NYSE tracker uses against two sessions computing a stale 'next ID'."""
    df = _load()
    new_id = int(df["ID"].max()) + 1 if len(df) else 1
    row = {"ID": new_id, "Date": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "Ask": ask, "Detail": detail, "Status": status, "Notes": notes}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _save(df)
    return new_id


def update(id_: int, **fields) -> bool:
    df = _load()
    mask = df["ID"] == id_
    if not mask.any():
        return False
    for k, v in fields.items():
        if k in COLUMNS:
            df.loc[mask, k] = v
    _save(df)
    return True


def list_pending() -> pd.DataFrame:
    df = _load()
    return df[df["Status"].isin(["pending", "in_progress", "waiting_on_user"])]


def list_all() -> pd.DataFrame:
    return _load()


if __name__ == "__main__":
    pending = list_pending()
    if pending.empty:
        print("Nothing pending.")
    else:
        for _, r in pending.iterrows():
            print(f"#{r['ID']:<3} [{r['Status']}] {r['Ask']}")
