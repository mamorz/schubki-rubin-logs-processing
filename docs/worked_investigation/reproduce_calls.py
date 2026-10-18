"""Re-run the worked-investigation tool calls (read-only) and save the full MCP responses.

Usage (from the repository root, with the stack running):
    docker run --rm --network schubki-jury_default --entrypoint python \\
      -v "$PWD/docs/worked_investigation:/wi" --user "$(id -u):$(id -g)" \\
      schubki-jury-mcp-server /wi/reproduce_calls.py /wi/evidence
"""
import json, sys
import anyio
from mcp import Client

OUT = sys.argv[1]
URL = sys.argv[2] if len(sys.argv) > 2 else "http://mcp-server:8000/mcp"
CALLS = [
    ("E1_search_rts_panic", "search_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", limit=20)),
    ("E2_nearby_33937", "get_nearby_events", dict(dataset="bgl", record_id=33937, same_component=True, before=10, after=10)),
    ("T1_weekly", "summarize_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", group_by="time", interval="week", limit=100)),
    ("T2_component", "summarize_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", group_by="component")),
    ("T3_team", "summarize_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", group_by="team")),
    ("T4_severity", "summarize_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", group_by="severity")),
    ("T5_exact_message", "summarize_logs", dict(dataset="bgl", query="rts panic! - stopping execution", match_mode="exact", group_by="component")),
    ("T6_peak_week_search", "search_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", start_time="2005-06-13T00:00:00", end_time="2005-06-20T00:00:00", limit=5)),
    ("T7_nearby_382921", "get_nearby_events", dict(dataset="bgl", record_id=382921, before=10, after=10)),
    ("M2_daily", "summarize_logs", dict(dataset="bgl", query="rts panic", match_mode="phrase", group_by="time", interval="day", limit=100)),
    ("M3_bgl_fatal_team", "summarize_logs", dict(dataset="bgl", severity="FATAL", group_by="team")),
    ("M4_os_error_team", "summarize_logs", dict(dataset="openstack", severity="ERROR", group_by="team")),
    ("M5_spawn", "analyze_vm_spawn_performance", dict(dataset="openstack")),
    ("M6_os_error_search", "search_logs", dict(dataset="openstack", query="error", severity="ERROR", limit=15)),
    ("M7_spikes_hour", "detect_log_spikes", dict(dataset="bgl", interval="hour", max_findings=10)),
]

async def main():
    async with Client(URL) as client:
        for label, tool, args in CALLS:
            r = await client.call_tool(tool, args)
            text = "".join(getattr(c, "text", "") for c in r.content)
            with open(f"{OUT}/{label}.json", "w") as fh:
                json.dump({"tool": tool, "arguments": args, "is_error": r.is_error, "text": text}, fh, indent=1)
            print(label, "ERROR " + text[:150] if r.is_error else "ok")

anyio.run(main)
