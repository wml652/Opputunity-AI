"""On-disk flight recorder per campaign:

* `campaign_{id}.jsonl`: one JSON record per event (machine-readable, full traces)
* `campaign_{id}.log`: one human-readable line per event

Records pass through the same redaction as application logs, so secrets can never land on disk."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from app.infrastructure.logging import redact_value


class FileCampaignLog:
    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir) / "campaigns"
        self._lock = threading.Lock()

    def path_for(self, campaign_id: uuid.UUID) -> str:
        return str(self._base / f"campaign_{campaign_id}.log")

    def write(self, campaign_id: uuid.UUID, record: dict[str, Any]) -> None:
        safe = redact_value(record)
        self._base.mkdir(parents=True, exist_ok=True)
        line = json.dumps(safe, default=str, ensure_ascii=False)
        human = _human(safe)
        with self._lock:
            with (self._base / f"campaign_{campaign_id}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            with (self._base / f"campaign_{campaign_id}.log").open("a", encoding="utf-8") as fh:
                fh.write(human + "\n")


def _human(r: dict[str, Any]) -> str:
    ts = str(r.get("ts", ""))[11:19]
    kind = str(r.get("kind", "event")).upper()
    if kind in ("PAGE", "PAGE_EXTRACTION"):
        label = "PAGE " if kind == "PAGE" else "EXTR "
        line = f"{ts} {label} {r.get('status'):<12} {r.get('url')}"
        if r.get("error_code"):
            line += f"  [{r.get('error_stage')}:{r.get('error_code')}] {r.get('error')}"
        if r.get("query"):
            line += f"  (via {r.get('discovery_source')}: {r.get('query')!r})"
        steps = r.get("trace") or []
        for step in steps:
            detail = step.get("detail") or {}
            brief = ", ".join(
                f"{k}={detail[k]}"
                for k in (
                    "http_status",
                    "final_url",
                    "html_bytes",
                    "markdown_chars",
                    "error",
                    "finish_reason",
                    "chunks",
                    "opportunities",
                )
                if k in detail and detail[k] not in (None, "", [])
            )
            stage, status = step.get("stage"), step.get("status")
            took = step.get("duration_ms", 0)
            line += f"\n           - {stage:<22} {status:<5} {took:>6} ms  {brief}"
        return line
    rest = {k: v for k, v in r.items() if k not in ("ts", "kind", "campaign_id", "trace")}
    return f"{ts} {kind:<16} " + json.dumps(rest, default=str, ensure_ascii=False)[:600]
