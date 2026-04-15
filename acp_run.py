#!/usr/bin/env python3
"""
ACP client for opencode: JSON-RPC over stdio.

Handles three interaction types:
1. Tool permissions → auto-approve (configurable via --approve)
2. Elicitation (elicitation/create) → write to question file, wait for answer
3. Question tool → surfaces as session/request_permission with decision options

When the agent asks a question, the script:
- Writes the question to --question-file as JSON
- Polls --answer-file until the caller writes the answer
- Sends the answer back to opencode and continues

Usage:
  # Simple run (auto-approve everything):
  python3 acp_run.py /path/to/repo --task "do something"

  # With interactive questions:
  python3 acp_run.py /path/to/repo --task "do something" \
    --question-file /tmp/q.json --answer-file /tmp/a.json

  # Resume after answering a question (same raw-log, appends):
  python3 acp_run.py --resume /tmp/acp-state.json
"""
import json, subprocess, sys, os, time, select, argparse, re, signal

OPENCODE = os.path.expanduser("~/.opencode/bin/opencode")
DEFAULT_OPENCODE = OPENCODE

def main():
    parser = argparse.ArgumentParser(description="ACP client for opencode")
    parser.add_argument("cwd", nargs="?", help="Project directory")
    parser.add_argument("--task", help="Task description (not needed for --resume)")
    parser.add_argument("--mode", default="build", help="Session mode (build/architect/plan)")
    parser.add_argument("--timeout", type=int, default=0, help="Safety timeout in seconds (0 = unlimited)")
    parser.add_argument("--raw-log", default="/tmp/acp-raw.jsonl", help="Path for raw JSONL log")
    parser.add_argument("--question-file", default="/tmp/acp-question.json", help="File to write questions to")
    parser.add_argument("--answer-file", default="/tmp/acp-answer.json", help="File to watch for answers")
    parser.add_argument("--state-file", default="/tmp/acp-state.json", help="State file for resume after question")
    parser.add_argument("--approve", default="all", choices=["all", "none", "safe"],
                        help="Auto-approve: all=approve everything, none=reject everything, safe=approve reads only")
    parser.add_argument("--resume", metavar="STATE_FILE", help="Resume from a paused state")
    parser.add_argument("--opencode-bin", default=DEFAULT_OPENCODE, help="Path to opencode binary")
    args = parser.parse_args()

    if args.resume:
        return run_resume(args)

    if not args.cwd or not args.task:
        parser.error("cwd and --task are required (unless using --resume)")

    return run_new(args)


def run_new(args):
    """Start a fresh opencode session."""
    proc = subprocess.Popen(
        [args.opencode_bin, "acp", "--cwd", args.cwd],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    raw_log = open(args.raw_log, "w")
    msg_id = 0
    buf = b""

    def send(method, params):
        nonlocal msg_id
        msg_id += 1
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        return msg_id

    def read_until_id(tid, timeout=10):
        nonlocal buf
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
                    raw_log.flush()
                    try:
                        obj = json.loads(line)
                        if obj.get("id") == tid:
                            return obj
                    except: pass
        return None

    # Init
    send("initialize", {"protocolVersion": 1, "capabilities": {}, "clientInfo": {"name": "openclaw-acp-client", "version": "0.2"}})
    resp = read_until_id(1, 10)
    if not resp:
        print(json.dumps({"status": "error", "error": "initialize failed"}))
        sys.exit(1)

    # Session
    send("session/new", {"cwd": args.cwd, "mcpServers": []})
    resp = read_until_id(2, 10)
    if not resp or "error" in resp:
        print(json.dumps({"status": "error", "error": f"session/new failed: {resp.get('error') if resp else 'timeout'}"}))
        sys.exit(1)
    sid = resp["result"]["sessionId"]

    # Set mode
    if args.mode != "build":
        mid = send("session/set_mode", {"sessionId": sid, "modeId": args.mode})
        read_until_id(mid, 10)

    # Prompt
    pid = send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": args.task}]})

    # Process events
    result = process_events(proc, raw_log, buf, args, sid, pid, msg_id)

    # Cleanup
    raw_log.close()

    if result.get("status") == "question":
        # Save state for resume — keep process alive
        state = {
            "pid": proc.pid,
            "sessionId": sid,
            "promptId": pid,
            "cwd": args.cwd,
            "mode": args.mode,
            "rawLog": args.raw_log,
            "approve": args.approve,
            "questionFile": args.question_file,
            "answerFile": args.answer_file,
            "stateFile": args.state_file,
            "opencodeBin": args.opencode_bin,
            "timeout": args.timeout,
            "deadlineRemaining": max(0, result.get("_deadline", 0) - time.time()) if result.get("_deadline") else 0,
            **{k: v for k, v in result.items() if k != "_deadline"},
        }
        # Remove internal fields
        state.pop("_deadline", None)
        with open(args.state_file, "w") as f:
            json.dump(state, f, indent=2)
        print(json.dumps(result, ensure_ascii=False))
        # Don't kill the process — it's waiting for an answer
        return

    # Normal completion
    cleanup_proc(proc)
    print(json.dumps(result, ensure_ascii=False))


def run_resume(args):
    """Resume a paused session after answering a question."""
    state_file = args.resume
    with open(state_file) as f:
        state = json.load(f)

    # Read the answer
    answer_file = state.get("answerFile", args.answer_file)
    with open(answer_file) as f:
        answer = json.load(f)

    # Find the process (it's still alive, waiting)
    pid = state["pid"]
    try:
        os.kill(pid, 0)  # Check if alive
    except ProcessLookupError:
        print(json.dumps({"status": "error", "error": f"Process {pid} is dead"}))
        sys.exit(1)

    # Reattach to the process via /proc
    proc = _reattach_process(pid)

    raw_log = open(state["rawLog"], "a")
    sid = state["sessionId"]
    prompt_id = state["promptId"]

    # Send the answer as a permission response
    tool_call_id = answer.get("toolCallId")
    option_id = answer.get("optionId", answer.get("answer", "allow"))
    custom_text = answer.get("text")

    if tool_call_id:
        msg = {"jsonrpc": "2.0", "id": 100, "method": "session/permission_response",
               "params": {"sessionId": sid, "toolCallId": tool_call_id, "optionId": option_id}}
        if custom_text:
            msg["params"]["content"] = {"type": "text", "text": custom_text}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

    # Continue processing events
    result = process_events(proc, raw_log, b"", args, sid, prompt_id, 101,
                            deadline_secs=state.get("deadlineRemaining") or 0)

    raw_log.close()
    cleanup_proc(proc)

    # Clean up state file
    try: os.unlink(state_file)
    except: pass
    try: os.unlink(answer_file)
    except: pass

    print(json.dumps(result, ensure_ascii=False))


def _reattach_process(pid):
    """Reattach stdin/stdout to a running process via /proc."""
    class Proc:
        def __init__(self, p):
            self.pid = p
            self.stdin = open(f"/proc/{p}/fd/0", "w")
            self.stdout = open(f"/proc/{p}/fd/1", "r")
        def terminate(self): os.kill(self.pid, signal.SIGTERM)
        def kill(self): os.kill(self.pid, signal.SIGKILL)
        def wait(self, timeout=5): pass
    return Proc(pid)


def process_events(proc, raw_log, buf, args, sid, prompt_id, start_msg_id, deadline_secs=0):
    """Main event loop. Returns summary dict."""
    nonlocal_buf = bytearray(buf)

    files_touched = set()
    tools_used = []
    final_text_parts = []
    stop_reason = None
    total_tokens = 0
    cost = "0"
    questions_asked = []

    if deadline_secs and deadline_secs > 0:
        deadline = time.time() + deadline_secs
    elif args.timeout and args.timeout > 0:
        deadline = time.time() + args.timeout
    else:
        deadline = float('inf')

    msg_id_counter = [start_msg_id]

    def send_resp(method, params):
        msg_id_counter[0] += 1
        msg = {"jsonrpc": "2.0", "id": msg_id_counter[0], "method": method, "params": params}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

    got_prompt_response = False
    while time.time() < deadline:
        rlist, _, _ = select.select([proc.stdout], [], [], 5)
        if rlist:
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk: break
            nonlocal_buf += chunk
            while b"\n" in nonlocal_buf:
                line, nonlocal_buf = nonlocal_buf.split(b"\n", 1)
                line = line.strip()
                if not line: continue
                raw_log.write(line.decode(errors="replace") + "\n")
                raw_log.flush()
                try:
                    obj = json.loads(line)
                except: continue

                method = obj.get("method", "")
                params = obj.get("params", {})
                upd = params.get("update", {})
                ut = upd.get("sessionUpdate", "")

                # --- Thought/message streaming ---
                if ut in ("agent_thought_chunk", "agent_message_chunk"):
                    content = upd.get("content", {})
                    text = content.get("text", "") if isinstance(content, dict) else ""
                    if text:
                        final_text_parts.append(text)

                # --- Tool call updates ---
                elif ut == "tool_call_update":
                    title = upd.get("title", upd.get("kind", ""))
                    status = upd.get("status", "")
                    kind = upd.get("kind", "")
                    locs = upd.get("locations") or []
                    raw_in = upd.get("rawInput", {})

                    for loc in locs:
                        p = loc.get("path", "")
                        if p:
                            rel = p.replace(args.cwd + "/", "") if hasattr(args, 'cwd') else p
                            if kind in ("write", "edit"):
                                files_touched.add(rel)
                            elif kind == "read":
                                files_touched.discard(rel)

                    if status == "completed" and raw_in:
                        for key in ("filePath", "path"):
                            p = raw_in.get(key, "")
                            if p and kind in ("write", "edit"):
                                rel = p.replace(args.cwd + "/", "") if hasattr(args, 'cwd') else p
                                files_touched.add(rel)
                        cmd = raw_in.get("command", "")
                        if cmd and kind == "execute":
                            for m in re.finditer(r'(?:cat\s*>\s*|tee\s+|>\s*)([\S]+\.\w+)', cmd):
                                files_touched.add(m.group(1))
                        tools_used.append(raw_in.get("description", title))

                # --- Usage ---
                elif ut == "usage_update":
                    total_tokens = upd.get("used", total_tokens)
                    cost_info = upd.get("cost", {})
                    cost = cost_info.get("amount", cost)

                # --- Permission request (tool approval OR question/decision) ---
                elif method == "session/request_permission":
                    tc = params.get("toolCall", {})
                    tc_id = tc.get("toolCallId", "")
                    tc_title = tc.get("title", "")
                    tc_kind = tc.get("kind", "")
                    tc_content = tc.get("content", [])
                    options = params.get("options", [])

                    # Determine if this is a question or a tool permission
                    is_question = _is_question_request(tc_kind, options, tc_content)

                    if is_question:
                        # Write question to file and wait for answer
                        question = {
                            "type": "question",
                            "toolCallId": tc_id,
                            "title": tc_title,
                            "content": _extract_text(tc_content),
                            "options": [_format_option(o) for o in options],
                            "raw": tc,
                        }
                        questions_asked.append(question)

                        # Write question file
                        q_file = args.question_file if hasattr(args, 'question_file') else "/tmp/acp-question.json"
                        a_file = args.answer_file if hasattr(args, 'answer_file') else "/tmp/acp-answer.json"

                        with open(q_file, "w") as f:
                            json.dump(question, f, indent=2, ensure_ascii=False)

                        # Remove stale answer file
                        try: os.unlink(a_file)
                        except: pass

                        # Signal question to caller, then wait
                        return {
                            "status": "question",
                            "stopReason": None,
                            "filesChanged": sorted(files_touched),
                            "toolsUsed": tools_used,
                            "tokensUsed": total_tokens,
                            "cost": cost,
                            "mode": args.mode if hasattr(args, 'mode') else "build",
                            "question": question,
                            "response": "".join(final_text_parts),
                            "rawLog": args.raw_log if hasattr(args, 'raw_log') else "/tmp/acp-raw.jsonl",
                            "_deadline": deadline,
                        }
                    else:
                        # Auto-approve based on policy
                        chosen = _auto_approve(tc_kind, options, args.approve if hasattr(args, 'approve') else "all")
                        send_resp("session/permission_response", {
                            "sessionId": sid,
                            "toolCallId": tc_id,
                            "optionId": chosen,
                        })

                # --- Elicitation (RFD, future support) ---
                elif method == "elicitation/create":
                    # Handle elicitation same as question
                    elicitation_id = obj.get("id")
                    message = params.get("message", "")
                    schema = params.get("requestedSchema", {})
                    mode = params.get("mode", "form")

                    question = {
                        "type": "elicitation",
                        "elicitationId": elicitation_id,
                        "mode": mode,
                        "message": message,
                        "schema": schema,
                        "options": list(schema.get("properties", {}).keys()) if schema else [],
                    }
                    questions_asked.append(question)

                    q_file = args.question_file if hasattr(args, 'question_file') else "/tmp/acp-question.json"
                    a_file = args.answer_file if hasattr(args, 'answer_file') else "/tmp/acp-answer.json"

                    with open(q_file, "w") as f:
                        json.dump(question, f, indent=2, ensure_ascii=False)
                    try: os.unlink(a_file)
                    except: pass

                    return {
                        "status": "question",
                        "stopReason": None,
                        "filesChanged": sorted(files_touched),
                        "toolsUsed": tools_used,
                        "tokensUsed": total_tokens,
                        "cost": cost,
                        "mode": args.mode if hasattr(args, 'mode') else "build",
                        "question": question,
                        "response": "".join(final_text_parts),
                        "rawLog": args.raw_log if hasattr(args, 'raw_log') else "/tmp/acp-raw.jsonl",
                        "_deadline": deadline,
                    }

                # --- Prompt response (final) ---
                elif obj.get("id") == prompt_id:
                    result = obj.get("result", {})
                    stop_reason = result.get("stopReason")
                    total_tokens = result.get("usage", {}).get("totalTokens", total_tokens)
                    text = result.get("text", "")
                    if text:
                        final_text_parts.append(text)
                    got_prompt_response = True
                    # Drain remaining
                    time.sleep(1)
                    while True:
                        r2, _, _ = select.select([proc.stdout], [], [], 0.5)
                        if not r2: break
                        c2 = os.read(proc.stdout.fileno(), 65536)
                        if not c2: break
                        for l in c2.split(b"\n"):
                            l = l.strip()
                            if l:
                                raw_log.write(l.decode(errors="replace") + "\n")
                    break

        if got_prompt_response:
            break

    full_text = "".join(final_text_parts)
    return {
        "status": "done" if stop_reason == "end_turn" else ("incomplete" if stop_reason else "timeout"),
        "stopReason": stop_reason,
        "filesChanged": sorted(files_touched),
        "toolsUsed": tools_used,
        "tokensUsed": total_tokens,
        "timeout": args.timeout if hasattr(args, 'timeout') else None,
        "cost": cost,
        "mode": args.mode if hasattr(args, 'mode') else "build",
        "questionsAsked": len(questions_asked),
        "response": full_text,
        "rawLog": args.raw_log if hasattr(args, 'raw_log') else "/tmp/acp-raw.jsonl",
    }


def _is_question_request(kind, options, content):
    """Determine if a permission request is actually a question/decision."""
    # Question tool uses kind="question" or "ask"
    if kind in ("question", "ask", "switch_mode"):
        return True
    # Check if content looks like a question (has multiple named options, not just allow/reject)
    non_approval = [o for o in options if o.get("kind") not in ("allow_once", "allow_always", "reject_once", "reject_always")]
    if len(non_approval) > 0:
        return True
    # If only 2 options and they're allow/reject, it's a permission
    return False


def _format_option(opt):
    """Format an option for the question file."""
    return {
        "id": opt.get("optionId"),
        "name": opt.get("name"),
        "kind": opt.get("kind"),
    }


def _extract_text(content_list):
    """Extract text from content blocks."""
    parts = []
    for c in content_list:
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif "text" in c:
                parts.append(c["text"])
        elif isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


def _auto_approve(kind, options, policy):
    """Choose an option based on approval policy."""
    if policy == "all":
        # Prefer allow_always > allow_once > first option
        for pref in ("allow_always", "allow_once"):
            for o in options:
                if o.get("kind") == pref:
                    return o["optionId"]
        return options[0]["optionId"] if options else "allow"
    elif policy == "safe":
        if kind in ("read", "list", "glob", "grep"):
            for o in options:
                if "allow" in o.get("kind", ""):
                    return o["optionId"]
        # For write/edit/bash, reject
        for o in options:
            if o.get("kind") == "reject_once":
                return o["optionId"]
        return options[0]["optionId"] if options else "reject"
    else:  # "none"
        for o in options:
            if o.get("kind") == "reject_once":
                return o["optionId"]
        return options[0]["optionId"] if options else "reject"


def cleanup_proc(proc):
    """Safely terminate the subprocess."""
    try:
        proc.stdin.close()
    except:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except:
        try: proc.kill()
        except: pass


if __name__ == "__main__":
    main()
