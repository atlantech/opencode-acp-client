#!/usr/bin/env python3
"""
ACP client for opencode: JSON-RPC over stdio.

Supports multi-turn conversations, auto-approve, and question handling.

Usage:
  # Turn 1 — creates session:
  python3 acp_run.py /repo --task "implement X"
  # → {"status": "done", "sessionId": "ses_abc", ...}

  # Turn 2 — reuses session:
  python3 acp_run.py /repo --session-id ses_abc --task "use arrow functions"
"""
import json, subprocess, sys, os, time, select, argparse, re

OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cwd", help="Project directory")
    p.add_argument("--task", required=True)
    p.add_argument("--session-id", default=None, help="Reuse existing session")
    p.add_argument("--mode", default="build")
    p.add_argument("--timeout", type=int, default=0, help="0 = unlimited")
    p.add_argument("--raw-log", default="/tmp/acp-raw.jsonl")
    p.add_argument("--question-file", default="/tmp/acp-question.json")
    p.add_argument("--answer-file", default="/tmp/acp-answer.json")
    p.add_argument("--approve", default="all", choices=["all", "none", "safe"])
    p.add_argument("--opencode-bin", default=OPENCODE)
    args = p.parse_args()

    proc = subprocess.Popen(
        [args.opencode_bin, "acp", "--cwd", args.cwd],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    msg_id = 0
    buf = b""

    def send(method, params):
        nonlocal msg_id
        msg_id += 1
        m = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(m) + "\n").encode())
        proc.stdin.flush()
        return msg_id

    def wait_for(tid, timeout=10):
        nonlocal buf
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 1)
            if r:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk: break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line: continue
                    raw_log.write(line.decode(errors="replace") + "\n")
                    raw_log.flush()
                    try:
                        obj = json.loads(line)
                        if obj.get("id") == tid: return obj
                    except: pass
        return None

    raw_log = open(args.raw_log, "a" if args.session_id else "w")

    # Init
    send("initialize", {"protocolVersion": 1, "capabilities": {},
             "clientInfo": {"name": "openclaw-acp", "version": "0.3"}})
    if not wait_for(1, 10):
        raw_log.close(); proc.kill()
        print(json.dumps({"status": "error", "error": "initialize failed"})); sys.exit(1)

    # Session
    if args.session_id:
        sid = args.session_id
        send("session/load", {"sessionId": sid, "cwd": args.cwd, "mcpServers": []})
        resp = wait_for(2, 30)  # may replay history, give more time
        if not resp or "error" in resp:
            raw_log.close(); proc.kill()
            print(json.dumps({"status": "error", "error": f"session/load failed: {resp}"})); sys.exit(1)
        sid = resp["result"]["sessionId"]
    else:
        send("session/new", {"cwd": args.cwd, "mcpServers": []})
        resp = wait_for(2, 10)
        if not resp or "error" in resp:
            raw_log.close(); proc.kill()
            print(json.dumps({"status": "error", "error": f"session/new failed: {resp}"})); sys.exit(1)
        sid = resp["result"]["sessionId"]
        # Set mode
        if args.mode != "build":
            send("session/set_mode", {"sessionId": sid, "modeId": args.mode})
            wait_for(3, 10)

    # Prompt
    pid = send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": args.task}]})

    # Event loop
    files_touched = set()
    tools_used = []
    text_parts = []
    stop_reason = None
    tokens = 0
    cost = "0"
    deadline = time.time() + args.timeout if args.timeout > 0 else float('inf')
    got_response = False

    while time.time() < deadline and not got_response:
        r, _, _ = select.select([proc.stdout], [], [], 5)
        if not r: continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk: break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line: continue
            raw_log.write(line.decode(errors="replace") + "\n")
            raw_log.flush()
            try: obj = json.loads(line)
            except: continue

            method = obj.get("method", "")
            params = obj.get("params", {})
            upd = params.get("update", {})
            ut = upd.get("sessionUpdate", "")

            # Streaming text
            if ut in ("agent_thought_chunk", "agent_message_chunk"):
                c = upd.get("content", {})
                t = c.get("text", "") if isinstance(c, dict) else ""
                if t: text_parts.append(t)

            # Tool calls
            elif ut == "tool_call_update":
                kind = upd.get("kind", "")
                status = upd.get("status", "")
                for loc in (upd.get("locations") or []):
                    path = loc.get("path", "")
                    if path:
                        rel = path.replace(args.cwd + "/", "")
                        files_touched.add(rel) if kind in ("write", "edit") else files_touched.discard(rel)
                if status == "completed" and upd.get("rawInput"):
                    ri = upd["rawInput"]
                    for k in ("filePath", "path"):
                        pv = ri.get(k, "")
                        if pv and kind in ("write", "edit"):
                            files_touched.add(pv.replace(args.cwd + "/", ""))
                    tools_used.append(ri.get("description", upd.get("title", "")))

            # Usage
            elif ut == "usage_update":
                tokens = upd.get("used", tokens)
                ci = upd.get("cost", {})
                cost = ci.get("amount", cost)

            # Permission / question
            elif method == "session/request_permission":
                tc = params.get("toolCall", {})
                tc_id = tc.get("toolCallId", "")
                tc_kind = tc.get("kind", "")
                options = params.get("options", [])
                is_q = tc_kind in ("question", "ask", "switch_mode") or \
                       any(o.get("kind") not in ("allow_once","allow_always","reject_once","reject_always") for o in options)

                if is_q:
                    question = {"type": "question", "toolCallId": tc_id,
                                "title": tc.get("title", ""),
                                "content": "\n".join(c.get("text","") if isinstance(c,dict) else str(c) for c in tc.get("content",[])),
                                "options": [{"id": o.get("optionId"), "name": o.get("name"), "kind": o.get("kind")} for o in options]}
                    with open(args.question_file, "w") as f:
                        json.dump(question, f, indent=2, ensure_ascii=False)
                    try: os.unlink(args.answer_file)
                    except: pass
                    cleanup(proc); raw_log.close()
                    print(json.dumps({"status": "question", "sessionId": sid, "question": question,
                                      "filesChanged": sorted(files_touched), "response": "".join(text_parts),
                                      "rawLog": args.raw_log}, ensure_ascii=False))
                    return
                else:
                    chosen = auto_approve(tc_kind, options, args.approve)
                    msg_id += 1
                    proc.stdin.write((json.dumps({"jsonrpc":"2.0","id":msg_id,"method":"session/permission_response",
                        "params":{"sessionId":sid,"toolCallId":tc_id,"optionId":chosen}}) + "\n").encode())
                    proc.stdin.flush()

            # Final response
            elif obj.get("id") == pid:
                res = obj.get("result", {})
                stop_reason = res.get("stopReason")
                tokens = res.get("usage", {}).get("totalTokens", tokens)
                t = res.get("text", "")
                if t: text_parts.append(t)
                got_response = True

    # Drain
    time.sleep(0.3)
    for _ in range(5):
        r, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not r: break
        c = os.read(proc.stdout.fileno(), 65536)
        if not c: break
        for ln in c.split(b"\n"):
            if ln.strip(): raw_log.write(ln.decode(errors="replace") + "\n")

    cleanup(proc); raw_log.close()
    print(json.dumps({"status": "done" if stop_reason == "end_turn" else "incomplete",
        "stopReason": stop_reason, "sessionId": sid,
        "filesChanged": sorted(files_touched), "toolsUsed": tools_used,
        "tokensUsed": tokens, "cost": cost, "mode": args.mode,
        "response": "".join(text_parts), "rawLog": args.raw_log}, ensure_ascii=False))


def auto_approve(kind, options, policy):
    if policy == "all":
        for pref in ("allow_always", "allow_once"):
            for o in options:
                if o.get("kind") == pref: return o["optionId"]
        return options[0]["optionId"] if options else "allow"
    elif policy == "safe":
        if kind in ("read","list","glob","grep","todowrite","skill"):
            for o in options:
                if "allow" in o.get("kind",""): return o["optionId"]
        for o in options:
            if o.get("kind") == "reject_once": return o["optionId"]
        return options[0]["optionId"] if options else "reject"
    else:
        for o in options:
            if o.get("kind") == "reject_once": return o["optionId"]
        return options[0]["optionId"] if options else "reject"


def cleanup(proc):
    try: proc.stdin.close()
    except: pass
    try:
        proc.terminate(); proc.wait(timeout=5)
    except:
        try: proc.kill()
        except: pass


if __name__ == "__main__":
    main()
