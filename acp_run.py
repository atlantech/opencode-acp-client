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
from __future__ import annotations

import argparse
import atexit
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Any

OPENCODE_BINARY = os.path.expanduser("~/.opencode/bin/opencode")

PROTOCOL_VERSION = 1
CLIENT_NAME = "openclaw-acp"
CLIENT_VERSION = "0.3"

RAW_LOG_DEFAULT = "/tmp/acp-raw.jsonl"
QUESTION_FILE_DEFAULT = "/tmp/acp-question.json"
ANSWER_FILE_DEFAULT = "/tmp/acp-answer.json"

CHUNK_SIZE = 65536
SELECT_TIMEOUT = 5
INITIALIZATION_TIMEOUT = 10
SESSION_TIMEOUT = 30
PROMPT_TIMEOUT = 10
DRAIN_WAIT_TIME = 0.3
DRAIN_ITERATIONS = 5
TERMINATE_TIMEOUT = 5
MAX_BUFFER_SIZE = 10 * 1024 * 1024
MAX_EMPTY_RETRIES = 5
RETRY_BASE_DELAY = 1.0
MAX_RETRIES = 3

UPDATE_TYPE_AGENT_THOUGHT = "agent_thought_chunk"
UPDATE_TYPE_AGENT_MESSAGE = "agent_message_chunk"
UPDATE_TYPE_TOOL_CALL = "tool_call_update"
UPDATE_TYPE_USAGE = "usage_update"

TOOL_KIND_WRITE = "write"
TOOL_KIND_EDIT = "edit"
TOOL_KIND_READ = "read"
TOOL_KIND_LIST = "list"
TOOL_KIND_GLOB = "glob"
TOOL_KIND_GREP = "grep"
TOOL_KIND_TODOWRITE = "todowrite"
TOOL_KIND_SKILL = "skill"

SAFE_TOOLS = frozenset({
    TOOL_KIND_READ, TOOL_KIND_LIST, TOOL_KIND_GLOB,
    TOOL_KIND_GREP, TOOL_KIND_TODOWRITE, TOOL_KIND_SKILL
})

OPTION_ALLOW_ALWAYS = "allow_always"
OPTION_ALLOW_ONCE = "allow_once"
OPTION_REJECT_ONCE = "reject_once"

APPROVE_ALL = "all"
APPROVE_SAFE = "safe"
APPROVE_NONE = "none"

STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_QUESTION = "question"
STATUS_INCOMPLETE = "incomplete"

REASON_END_TURN = "end_turn"

MODE_BUILD = "build"

_client_instance: Optional["ACPClient"] = None


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments for the ACP client."""
    parser = argparse.ArgumentParser(
        description="ACP client for opencode: JSON-RPC over stdio"
    )
    parser.add_argument("cwd", help="Project directory")
    parser.add_argument("--task", required=True, help="Task to execute")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Reuse existing session"
    )
    parser.add_argument("--mode", default=MODE_BUILD, help="Session mode")
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Timeout in seconds (0 = unlimited)"
    )
    parser.add_argument("--raw-log", default=RAW_LOG_DEFAULT, help="Path to raw log file")
    parser.add_argument(
        "--question-file",
        default=QUESTION_FILE_DEFAULT,
        help="Path to question file"
    )
    parser.add_argument(
        "--answer-file",
        default=ANSWER_FILE_DEFAULT,
        help="Path to answer file"
    )
    parser.add_argument(
        "--approve",
        default=APPROVE_ALL,
        choices=[APPROVE_ALL, APPROVE_NONE, APPROVE_SAFE],
        help="Auto-approve policy"
    )
    parser.add_argument("--opencode-bin", default=OPENCODE_BINARY, help="Path to opencode binary")
    return parser.parse_args()


def determine_auto_approve(kind: str, options: list[dict], policy: str) -> str:
    """
    Determine which option to auto-approve based on policy.

    Args:
        kind: The tool call kind (e.g., 'read', 'write', 'edit').
        options: List of available permission options.
        policy: Approval policy ('all', 'safe', 'none').

    Returns:
        The optionId to approve.
    """
    if not options:
        return OPTION_ALLOW_ONCE if policy == APPROVE_ALL else OPTION_REJECT_ONCE

    if policy == APPROVE_ALL:
        for pref in (OPTION_ALLOW_ALWAYS, OPTION_ALLOW_ONCE):
            for option in options:
                if option.get("kind") == pref:
                    return option["optionId"]
        return options[0]["optionId"]

    if policy == APPROVE_SAFE:
        if kind in SAFE_TOOLS:
            for option in options:
                if "allow" in option.get("kind", ""):
                    return option["optionId"]
        for option in options:
            if option.get("kind") == OPTION_REJECT_ONCE:
                return option["optionId"]
        return options[0]["optionId"]

    for option in options:
        if option.get("kind") == OPTION_REJECT_ONCE:
            return option["optionId"]
    return options[0]["optionId"]


def cleanup_process(proc: Optional[subprocess.Popen]) -> None:
    """Clean up the subprocess by closing stdin and terminating it gracefully."""
    if proc is None:
        return
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=TERMINATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    except OSError:
        pass


def _signal_handler(signum: int, frame: Any) -> None:
    """Handle SIGTERM/SIGINT signals to clean up the subprocess."""
    global _client_instance
    if _client_instance:
        cleanup_process(_client_instance.process)
        if _client_instance.raw_log_file:
            try:
                _client_instance.raw_log_file.close()
            except OSError:
                pass
    sys.exit(128 + signum)


def is_question_tool(kind: str, options: list[dict]) -> bool:
    """Determine if a tool call is a question that requires user interaction."""
    question_kinds = frozenset({"question", "ask", "switch_mode"})
    if kind in question_kinds:
        return True
    for option in options:
        option_kind = option.get("kind", "")
        if option_kind not in (
            OPTION_ALLOW_ONCE, OPTION_ALLOW_ALWAYS,
            OPTION_REJECT_ONCE, "reject_always"
        ):
            return True
    return False


def format_question(params: dict, cwd: str) -> dict:
    """Format a question from permission request parameters."""
    tool_call = params.get("toolCall", {})
    options = params.get("options", [])
    tool_kind = tool_call.get("kind", "")

    content_parts = []
    for item in tool_call.get("content", []):
        if isinstance(item, dict):
            content_parts.append(item.get("text", ""))
        else:
            content_parts.append(str(item))

    return {
        "type": "question",
        "toolCallId": tool_call.get("toolCallId", ""),
        "title": tool_call.get("title", ""),
        "content": "\n".join(content_parts),
        "options": [
            {
                "id": option.get("optionId"),
                "name": option.get("name"),
                "kind": option.get("kind")
            }
            for option in options
        ],
        "_kind": tool_kind
    }


def strip_cwd_prefix(path: str, cwd: str) -> str:
    """Remove the cwd prefix from a file path."""
    prefix = cwd.rstrip("/") + "/"
    return path.replace(prefix, "", 1) if path.startswith(prefix) else path


class ACPClient:
    """
    ACP (Agent Communication Protocol) client for opencode.

    Handles JSON-RPC communication over stdio with the opencode process,
    including session management, tool call permissions, and response streaming.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        """
        Initialize the ACP client.

        Args:
            args: Parsed command line arguments.
        """
        global _client_instance
        _client_instance = self

        self.args = args
        self.process: Optional[subprocess.Popen] = None
        self.message_id = 0
        self.buffer = b""
        self.raw_log_file: Optional[Any] = None
        self.session_id: Optional[str] = None

        self.files_touched: set[str] = set()
        self.tools_used: list[str] = []
        self.text_parts: list[str] = []
        self.stop_reason: Optional[str] = None
        self.tokens_used = 0
        self.cost = "0"

        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_done = threading.Event()

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        atexit.register(self._atexit_cleanup)

    def _atexit_cleanup(self) -> None:
        """Clean up on exit via atexit."""
        cleanup_process(self.process)
        if self.raw_log_file:
            try:
                self.raw_log_file.close()
            except OSError:
                pass

    def _stderr_reader(self) -> None:
        """Read and discard stderr in a separate thread to prevent deadlock."""
        try:
            while not self._stderr_done.is_set():
                try:
                    chunk = os.read(self.process.stderr.fileno(), CHUNK_SIZE)
                    if not chunk:
                        break
                except OSError:
                    break
        finally:
            self._stderr_done.set()

    def _send_message(self, method: str, params: dict) -> int:
        """
        Send a JSON-RPC message to the opencode process.

        Args:
            method: The RPC method name.
            params: The method parameters.

        Returns:
            The message ID assigned to this message.
        """
        self.message_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self.message_id,
            "method": method,
            "params": params
        }
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            self.process.stdin.flush()
        except BrokenPipeError:
            pass
        return self.message_id

    def _parse_buffer(self) -> list[tuple[bytes, dict]]:
        """
        Parse complete JSON messages from buffer, handling partial reads.

        Returns:
            List of (raw_line, parsed) tuples for complete messages.
        """
        complete_messages = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue

            if len(self.buffer) > MAX_BUFFER_SIZE:
                sys.stderr.write(f"Buffer exceeded max size, truncating\n")
                self.buffer = self.buffer[-CHUNK_SIZE:]

            try:
                msg = json.loads(line)
                complete_messages.append((line, msg))
            except json.JSONDecodeError as e:
                sys.stderr.write(
                    f"JSON decode error: {e.msg} at position {e.pos}, "
                    f"raw: {line[:200]!r}\n"
                )
        return complete_messages

    def _wait_for_response(self, message_id: int, timeout: float = 10) -> Optional[dict]:
        """
        Wait for a response with a specific message ID.

        Args:
            message_id: The message ID to wait for.
            timeout: Maximum time to wait in seconds.

        Returns:
            The response object, or None if timeout occurred.
        """
        deadline = time.time() + timeout
        empty_retries = 0
        while time.time() < deadline:
            read_ready, _, _ = select.select([self.process.stdout.fileno()], [], [], 1)
            if not read_ready:
                continue

            chunk = os.read(self.process.stdout.fileno(), CHUNK_SIZE)
            if not chunk:
                empty_retries += 1
                if empty_retries >= MAX_EMPTY_RETRIES:
                    break
                continue
            empty_retries = 0

            self.buffer += chunk
            for _, msg in self._parse_buffer():
                if msg.get("id") == message_id:
                    return msg
        return None

    def _start_process(self) -> None:
        """Start the opencode subprocess."""
        self.process = subprocess.Popen(
            [self.args.opencode_bin, "acp", "--cwd", self.args.cwd],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_done.clear()
        self._stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        self._stderr_thread.start()

    def _initialize(self) -> None:
        """Initialize the ACP connection with retry logic."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            self._send_message(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION}
                }
            )
            response = self._wait_for_response(self.message_id, INITIALIZATION_TIMEOUT)
            if response:
                if "error" in response:
                    last_error = response["error"]
                else:
                    return
            else:
                last_error = "no response from server"

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (attempt + 1))

        cleanup_process(self.process)
        print(json.dumps({
            "status": STATUS_ERROR,
            "error": f"initialize failed: {last_error}"
        }))
        sys.exit(1)

    def _load_or_create_session(self) -> str:
        """
        Load an existing session or create a new one.

        Returns:
            The session ID.
        """
        if self.args.session_id:
            return self._load_session()
        return self._create_session()

    def _load_session(self) -> str:
        """Load an existing session by ID with retry logic."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            self._send_message(
                "session/load",
                {
                    "sessionId": self.args.session_id,
                    "cwd": self.args.cwd,
                    "mcpServers": []
                }
            )
            response = self._wait_for_response(self.message_id, SESSION_TIMEOUT)
            if response and "error" not in response:
                result = response.get("result")
                if result and "sessionId" in result:
                    return result["sessionId"]
                last_error = f"invalid response structure: {response}"
            else:
                last_error = str(response) if response else "no response"

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (attempt + 1))

        cleanup_process(self.process)
        print(json.dumps({
            "status": STATUS_ERROR,
            "error": f"session/load failed: {last_error}"
        }))
        sys.exit(1)

    def _create_session(self) -> str:
        """Create a new session with retry logic."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            self._send_message(
                "session/new",
                {"cwd": self.args.cwd, "mcpServers": []}
            )
            response = self._wait_for_response(self.message_id, SESSION_TIMEOUT)
            if response and "error" not in response:
                result = response.get("result")
                if result and "sessionId" in result:
                    session_id = result["sessionId"]

                    if self.args.mode != MODE_BUILD:
                        self._send_message(
                            "session/set_mode",
                            {"sessionId": session_id, "modeId": self.args.mode}
                        )
                        self._wait_for_response(self.message_id, PROMPT_TIMEOUT)

                    return session_id
                last_error = f"invalid response structure: {response}"
            else:
                last_error = str(response) if response else "no response"

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (attempt + 1))

        cleanup_process(self.process)
        print(json.dumps({
            "status": STATUS_ERROR,
            "error": f"session/new failed: {last_error}"
        }))
        sys.exit(1)

    def _submit_prompt(self) -> int:
        """
        Submit the task prompt.

        Returns:
            The message ID of the prompt request.
        """
        return self._send_message(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": self.args.task}]
            }
        )

    def _process_update(self, update_type: str, update_data: dict) -> bool:
        """
        Process a session update.

        Args:
            update_type: The type of update.
            update_data: The update data.

        Returns:
            True if the update was handled, False if it should be processed further.
        """
        if update_type in (UPDATE_TYPE_AGENT_THOUGHT, UPDATE_TYPE_AGENT_MESSAGE):
            self._process_text_chunk(update_data)
            return True

        if update_type == UPDATE_TYPE_TOOL_CALL:
            self._process_tool_update(update_data)
            return True

        if update_type == UPDATE_TYPE_USAGE:
            self._process_usage_update(update_data)
            return True

        return False

    def _log_progress(self, msg: str) -> None:
        """Write a progress line to stderr for the caller to monitor."""
        sys.stderr.write(f"[acp] {msg}\n")
        sys.stderr.flush()

    def _process_text_chunk(self, update_data: dict) -> None:
        """Process a text chunk from the agent."""
        content = update_data.get("content", {})
        if isinstance(content, dict):
            text = content.get("text", "")
            if text:
                self.text_parts.append(text)

    def _process_tool_update(self, update_data: dict) -> None:
        """Process a tool call update."""
        tool_kind = update_data.get("kind", "")
        tool_status = update_data.get("status", "")
        tool_title = update_data.get("title", tool_kind)

        if tool_status == "started":
            self._log_progress(f"tool started: {tool_title}")

        locations = update_data.get("locations") or []
        for location in locations:
            path = location.get("path", "")
            if path:
                relative_path = strip_cwd_prefix(path, self.args.cwd)
                if tool_kind in (TOOL_KIND_WRITE, TOOL_KIND_EDIT):
                    self.files_touched.add(relative_path)
                else:
                    self.files_touched.discard(relative_path)

        if tool_status == "completed" and update_data.get("rawInput"):
            raw_input = update_data["rawInput"]
            for key in ("filePath", "path"):
                file_path = raw_input.get(key, "")
                if file_path and tool_kind in (TOOL_KIND_WRITE, TOOL_KIND_EDIT):
                    self.files_touched.add(strip_cwd_prefix(file_path, self.args.cwd))

            description = raw_input.get(
                "description",
                update_data.get("title", "")
            )
            if description:
                self.tools_used.append(description)

        if tool_status == "completed":
            self._log_progress(f"tool done: {tool_title}")

    def _process_usage_update(self, update_data: dict) -> None:
        """Process a usage/cost update."""
        self.tokens_used = update_data.get("used", self.tokens_used)
        cost_info = update_data.get("cost", {})
        self.cost = cost_info.get("amount", self.cost)
        self._log_progress(f"tokens: {self.tokens_used}, cost: {self.cost}")

    def _handle_permission_request(self, params: dict) -> bool:
        """
        Handle a permission request from the server.

        Args:
            params: The request parameters.

        Returns:
            True if question was handled (exited), False otherwise.
        """
        tool_call = params.get("toolCall", {})
        tool_call_id = tool_call.get("toolCallId", "")
        tool_kind = tool_call.get("kind", "")
        options = params.get("options", [])

        if is_question_tool(tool_kind, options):
            question = format_question(params, self.args.cwd)

            with open(self.args.question_file, "w") as f:
                json.dump(question, f, indent=2, ensure_ascii=False)

            try:
                os.unlink(self.args.answer_file)
            except OSError:
                pass

            cleanup_process(self.process)
            self.raw_log_file.close()

            result = {
                "status": STATUS_QUESTION,
                "sessionId": self.session_id,
                "question": question,
                "filesChanged": sorted(self.files_touched),
                "response": "".join(self.text_parts),
                "rawLog": self.args.raw_log
            }
            print(json.dumps(result, ensure_ascii=False))
            return True

        chosen_option = determine_auto_approve(tool_kind, options, self.args.approve)
        self._send_message(
            "session/permission_response",
            {
                "sessionId": self.session_id,
                "toolCallId": tool_call_id,
                "optionId": chosen_option
            }
        )
        return False

    def _process_response(self, result: dict) -> bool:
        """
        Process a method response.

        Args:
            result: The response result.

        Returns:
            True if processing should stop, False otherwise.
        """
        self.stop_reason = result.get("stopReason")

        usage = result.get("usage", {})
        if usage:
            self.tokens_used = usage.get("totalTokens", self.tokens_used)

        response_text = result.get("text", "")
        if response_text:
            self.text_parts.append(response_text)

        return True

    def _drain_output(self) -> None:
        """Drain any remaining output from the process."""
        time.sleep(DRAIN_WAIT_TIME)
        for _ in range(DRAIN_ITERATIONS):
            ready, _, _ = select.select([self.process.stdout.fileno()], [], [], DRAIN_WAIT_TIME)
            if not ready:
                break

            chunk = os.read(self.process.stdout.fileno(), CHUNK_SIZE)
            if not chunk:
                break

            for line in chunk.split(b"\n"):
                if line.strip():
                    self.raw_log_file.write(line.decode(errors="replace") + "\n")

    def run(self) -> None:
        """Run the ACP client main loop."""
        self._start_process()
        self.raw_log_file = open(
            self.args.raw_log,
            "a" if self.args.session_id else "w"
        )

        self._log_progress("initializing opencode...")
        self._initialize()
        self._log_progress(f"initialized, session: {self.session_id or 'creating...'}")
        self.session_id = self._load_or_create_session()
        self._log_progress(f"session: {self.session_id}")
        prompt_id = self._submit_prompt()
        self._log_progress("task submitted, waiting for response...")

        deadline = (
            time.time() + self.args.timeout
            if self.args.timeout > 0
            else float("inf")
        )
        got_response = False

        while time.time() < deadline and not got_response:
            ready, _, _ = select.select([self.process.stdout.fileno()], [], [], SELECT_TIMEOUT)
            if not ready:
                continue

            chunk = os.read(self.process.stdout.fileno(), CHUNK_SIZE)
            if not chunk:
                continue

            self.buffer += chunk

            for raw_line, message in self._parse_buffer():
                self.raw_log_file.write(raw_line.decode(errors="replace") + "\n")
                self.raw_log_file.flush()

                if message.get("method") == "session/request_permission":
                    if self._handle_permission_request(message.get("params", {})):
                        self._stderr_done.set()
                        return
                    continue

                if message.get("id") == prompt_id:
                    if "result" in message:
                        self._process_response(message["result"])
                        got_response = True
                    continue

                params = message.get("params", {})
                update_type = params.get("update", {}).get("sessionUpdate", "")
                update_data = params.get("update", {})
                self._process_update(update_type, update_data)

        self._drain_output()
        self._stderr_done.set()
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)
        cleanup_process(self.process)
        self.raw_log_file.close()

        self._log_progress(f"finished: {self.stop_reason}")
        status = STATUS_DONE if self.stop_reason == REASON_END_TURN else STATUS_INCOMPLETE
        result = {
            "status": status,
            "stopReason": self.stop_reason,
            "sessionId": self.session_id,
            "filesChanged": sorted(self.files_touched),
            "toolsUsed": self.tools_used,
            "tokensUsed": self.tokens_used,
            "cost": self.cost,
            "mode": self.args.mode,
            "response": "".join(self.text_parts),
            "rawLog": self.args.raw_log
        }
        print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    """Main entry point for the ACP client."""
    args = parse_arguments()
    client = ACPClient(args)
    client.run()


if __name__ == "__main__":
    main()