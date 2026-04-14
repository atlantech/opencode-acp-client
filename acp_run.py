#!/usr/bin/env python3
"""ACP client that saves raw output to disk and prints only a compact summary."""
import json, subprocess, sys, os, time, select, argparse

OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cwd", help="Project directory")
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--mode", default="build", help="Session mode (build/architect/plan)")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--raw-log", default="/tmp/acp-raw.jsonl", help="Path for raw JSONL log")
    args = parser.parse_args()

    proc = subprocess.Popen(
        [OPENCODE, "acp", "--cwd", args.cwd],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    raw_log = open(args.raw_log, "w")
    msg_id = 0

    def send(method, params):
        nonlocal msg_id
        msg_id += 1
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        return msg_id

    def read_until_id(tid, timeout=10):
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rlist, _, _ = select.select([proc.stdout], [], [], 1)
            if rlist:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk: break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line: continue
                    raw_log.write(line.decode(errors="replace") + "\n")
                    try:
                        obj = json.loads(line)
                        if obj.get("id") == tid:
                            return obj, buf
                    except: pass
        return None, buf

    # Init
    send("initialize", {"protocolVersion": 1, "capabilities": {}, "clientInfo": {"name": "openclaw", "version": "0.1"}})
    resp, buf = read_until_id(1, 10)
    if not resp:
        print(json.dumps({"status": "error", "error": "initialize failed"}))
        sys.exit(1)

    # Session
    send("session/new", {"cwd": args.cwd, "mcpServers": []})
    resp, buf = read_until_id(2, 10)
    if not resp or "error" in resp:
        print(json.dumps({"status": "error", "error": "session/new failed"}))
        sys.exit(1)
    sid = resp["result"]["sessionId"]

    # Set mode (if not build)
    if args.mode != "build":
        mid = send("session/set_mode", {"sessionId": sid, "modeId": args.mode})
        resp, buf = read_until_id(mid, 10)

    # Prompt
    pid = send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": args.task}]})

    # Collect ALL output but don't print streaming — just parse for summary
    files_touched = set()
    tools_used = []
    final_text_parts = []
    stop_reason = None
    total_tokens = 0
    cost = "0"

    deadline = time.time() + args.timeout
    got_prompt_response = False
    while time.time() < deadline:
        rlist, _, _ = select.select([proc.stdout], [], [], 2)
        if rlist:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk: break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line: continue
                raw_log.write(line.decode(errors="replace") + "\n")
                try:
                    obj = json.loads(line)
                except: continue

                method = obj.get("method", "")
                upd = obj.get("params", {}).get("update", {})
                ut = upd.get("sessionUpdate", "")

                if ut in ("agent_thought_chunk", "agent_message_chunk"):
                    content = upd.get("content", {})
                    text = content.get("text", "") if isinstance(content, dict) else ""
                    if text:
                        final_text_parts.append(text)

                elif ut == "tool_call_update":
                    title = upd.get("title", upd.get("kind", ""))
                    status = upd.get("status", "")
                    kind = upd.get("kind", "")
                    locs = upd.get("locations") or []
                    raw_in = upd.get("rawInput", {})

                    # Extract file paths from locations (present on in_progress)
                    for loc in locs:
                        p = loc.get("path", "")
                        if p:
                            rel = p.replace(args.cwd + "/", "")
                            if kind in ("write", "edit"):
                                files_touched.add(rel)
                            elif kind == "read":
                                files_touched.discard(rel)  # reads aren't changes

                    # Extract file paths from rawInput (present on completed)
                    if status == "completed" and raw_in:
                        # write/edit with filePath or path
                        for key in ("filePath", "path"):
                            p = raw_in.get(key, "")
                            if p and kind in ("write", "edit"):
                                files_touched.add(p.replace(args.cwd + "/", ""))
                        # bash commands — grep for file-like patterns
                        cmd = raw_in.get("command", "")
                        if cmd and kind == "execute":
                            # crude: look for > or >> redirects, cat >, tee
                            import re
                            for m in re.finditer(r'(?:cat\s*>\s*|tee\s+|>\s*)([\S]+\.\w+)', cmd):
                                files_touched.add(m.group(1))
                        tools_used.append(raw_in.get("description", title))

                elif ut == "usage_update":
                    total_tokens = upd.get("used", total_tokens)
                    cost_info = upd.get("cost", {})
                    cost = cost_info.get("amount", cost)

                elif obj.get("id") == pid:
                    result = obj.get("result", {})
                    stop_reason = result.get("stopReason")
                    total_tokens = result.get("usage", {}).get("totalTokens", total_tokens)
                    text = result.get("text", "")
                    if text:
                        final_text_parts.append(text)
                    got_prompt_response = True
                    # Drain a bit more
                    time.sleep(1)
                    while True:
                        r2, _, _ = select.select([proc.stdout], [], [], 0.5)
                        if not r2: break
                        c2 = os.read(proc.stdout.fileno(), 65536)
                        if not c2: break
                        for l in c2.split(b"\n"):
                            l = l.strip()
                            if l: raw_log.write(l.decode(errors="replace") + "\n")
                    break

        if got_prompt_response:
            break

    proc.stdin.close()
    try: proc.terminate(); proc.wait(timeout=5)
    except: proc.kill()
    raw_log.close()

    # Print only structured summary
    full_text = "".join(final_text_parts)
    summary = {
        "status": "done" if stop_reason == "end_turn" else "incomplete",
        "stopReason": stop_reason,
        "filesChanged": sorted(files_touched),
        "toolsUsed": tools_used,
        "tokensUsed": total_tokens,
        "cost": cost,
        "mode": args.mode,
        "response": full_text,
        "rawLog": args.raw_log,
    }
    print(json.dumps(summary, ensure_ascii=False))

if __name__ == "__main__":
    main()
