"""Put a file on the Desktop while the bot is waiting for a human.

The fail-safe design of the unattended loop is that an ATTENTION marker stops every
later run until someone reads it. That worked exactly as designed on 2026-09-05 - and
nobody read it for 20 days, because the marker lives in a hidden folder. A safety
mechanism that no one notices is only half a mechanism.

So: when a marker exists, write "BINANCE BOT - CAN XEM.txt" on the Desktop with the
reason and the runbook pointer; when none exists, remove it. Hung off the paper task,
which runs every morning and does not touch the exchange. Best-effort Windows toast on
top; the file is the part that cannot fail.

    python notify_markers.py            # exit 0 always
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKERS = {
    "ATTENTION": ROOT / ".execution" / "ATTENTION",
    "canary_ALERT": ROOT / ".execution" / "canary_ALERT",
    "testnet_daily.lock": ROOT / ".execution" / "testnet_daily.lock",
}
FLAG_NAME = "BINANCE BOT - CAN XEM.txt"


def desktop() -> Path:
    for candidate in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"):
        if candidate.is_dir():
            return candidate
    return Path.home()


def present() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, path in MARKERS.items():
        if not path.exists():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace").strip()
            try:
                body = json.loads(body).get("reason", body) if body.startswith("{") else body
            except ValueError:
                pass
        except OSError:
            body = "(khong doc duoc)"
        found[name] = str(body)[:200]
    return found


def render(found: dict[str, str]) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"BINANCE BOT dang cho ban xem  ({now})",
        "",
        "Vong lap testnet se KHONG chay lai cho toi khi marker duoi day duoc xu ly.",
        "Moi ngay bi chan la mot ngay mat cho cong 20 lan COMPLETE.",
        "",
    ]
    for name, why in found.items():
        lines.append(f"  .execution/{name}: {why}")
    lines += [
        "",
        "Lam gi: doc EXECUTION_RUNBOOK.md (muc 'Doc status - va lam gi'), ghi quyet dinh vao",
        "carry_paper_incidents.md, roi xoa marker. File nay tu bien mat khi khong con marker.",
        "",
        "Nhanh: python status.py",
    ]
    return "\n".join(lines) + "\n"


def toast(title: str, text: str) -> None:
    """Best effort. A scheduled task may have no interactive session; the file still exists."""
    if not sys.platform.startswith("win"):
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) > $null;"
        f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{text}')) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Binance Analyst').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001 - notification is a courtesy, never a failure
        pass


def main() -> int:
    flag = desktop() / FLAG_NAME
    found = present()
    if not found:
        try:
            if flag.exists():
                flag.unlink()
                print("marker cleared - desktop flag removed")
            else:
                print("no markers")
        except OSError as exc:
            print(f"could not remove desktop flag: {exc}")
        return 0
    body = render(found)
    try:
        tmp = flag.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, flag)
        print(f"desktop flag written: {flag}")
    except OSError as exc:
        print(f"could not write desktop flag: {exc}")
    toast("Binance bot dang cho ban", ", ".join(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
