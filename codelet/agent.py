"""The :class:`MiniAgent` orchestrates the tool loop.

The agent ties together: workspace context (``workspace``), prompt assembly
(``prompt``), parsing (``parsing``), the tool registry (``tools``), and the
durable session store (``sessions``). All knobs that used to be hard-coded
(token budgets, history caps, tool examples, rules, sandbox patterns,
retry-notice text) now flow in via the YAML-backed config object.
"""

import json
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import baseline as baseline_module
from . import compaction as compaction_module
from . import memory_files as memory_files_module
from .config import BUILTIN_DEFAULTS, deep_merge, load_project_rules
from .tasks import kill_task, spawn_task
from .commands import get_commands, run_command
from .history import append_history
from .permissions import PermissionContext, get_empty_permission_context
from .cost_tracker import CostTracker, load_cost_state, save_cost_state
from . import parsing
from .prompt import (
    build_history_text,
    build_memory_text,
    build_prefix,
    build_prompt,
)
from . import sandbox as sandbox_module
from .stop_reason import AskResult, StopReason
from .tools import (
    ToolRegistry,
    is_concurrency_safe,
    repair_tool_args,
    tool_argument_validators,
)
from .utils import clip, now

# Matches "exit_code: 0" followed by a non-digit (or end of string/line),
# i.e. an actual zero exit code rather than a code starting with "0" like "07".
_re_exit_zero = re.compile(r"exit_code:\s*0(?!\d)")


class MiniAgent:
    """A small, deterministic harness around a non-deterministic model."""

    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        approval_policy="ask",
        max_steps=None,
        max_new_tokens=None,
        depth=0,
        max_depth=None,
        read_only=False,
        allowed_ops=None,
        sandbox="lite",
        config=None,
        tool_output_callback=None,
        approve_hook=None,
    ):
        # Use packaged defaults if no config was provided. ``deep_merge``
        # gives us a fresh, mutation-safe copy.
        self.config = deep_merge(BUILTIN_DEFAULTS, config or {})
        # Apply sandbox policy from the config (if any).
        sandbox_module.apply_config(self.config.get("sandbox") or {})

        harness = self.config.get("harness", {})

        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.cwd).resolve()
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps if max_steps is not None else harness.get("max_steps", 6)
        self.max_new_tokens = (
            max_new_tokens if max_new_tokens is not None else harness.get("max_new_tokens", 512)
        )
        self.depth = depth
        self.max_depth = max_depth if max_depth is not None else harness.get("max_depth", 1)
        self.read_only = read_only
        self.allowed_ops = allowed_ops
        self.sandbox = sandbox if sandbox in ("off", "lite") else "lite"
        self.tool_output_callback = tool_output_callback
        # Optional callable invoked just before the interactive approval prompt.
        # The CLI uses this to stop the spinner so the prompt appears cleanly.
        self.approve_hook = approve_hook
        # Optional callables invoked around each model inference call.
        # The CLI uses these to show/hide the "Thinking..." spinner only
        # during actual LLM calls, so the spinner is hidden while tools run.
        self.inference_start_hook = None
        self.inference_end_hook = None
        self.last_stop_reason = None
        self.last_ask_result = None
        self.last_compaction_stages = []

        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": {"task": "", "files": [], "notes": []},
            "baseline": None,
        }

        # Cost tracking (mirrors reference agent's cost-tracker.ts)
        _model_attr = getattr(model_client, "model", "unknown")
        # MagicMock returns a new MagicMock for any attribute access; coerce to str
        model_name = str(_model_attr) if not callable(_model_attr) else "unknown"
        if "MagicMock" in model_name:
            model_name = "unknown"
        self.cost_tracker = load_cost_state(self.workspace.repo_root, self.session["id"]) or CostTracker(model_name=model_name)
        self.max_budget_usd = None

        # Permission context (mirrors reference agent's ToolPermissionContext)
        self.permission_context = get_empty_permission_context()
        self.permission_denials = []

        # Session-baseline verification: every session begins with a check
        # against the physical repository state to prevent compounding
        # hallucinations across disjointed runs.
        self.baseline_drift = baseline_module.verify_session_baseline(
            self.session,
            self.workspace.repo_root,
            watch_files=self.config.get("project_rules_files") or [],
        )

        # Query engine / context window (mirrors reference agent's query.ts)
        self.max_tokens = None
        _model_name = getattr(model_client, "model", "unknown")
        _model_name = str(_model_name) if not callable(_model_name) else "unknown"
        if "MagicMock" in _model_name:
            _model_name = "unknown"
        self._model_name = _model_name

        # Content replacement state (mirrors reference agent's content replacement)
        from .query import ContentReplacementState
        self.content_replacement_state = ContentReplacementState()

        # MCP clients (mirrors reference agent's MCP support)
        self.mcp_clients: List[Any] = []

        # Thinking config (mirrors reference agent's thinking.ts)
        self.thinking_config = {"type": "adaptive"}

        # Abort controller (mirrors reference agent's AbortController)
        from .abort_controller import AbortController
        self.abort_controller = AbortController()

        # Build the per-agent tool registry and stable prompt prefix.
        self.registry = ToolRegistry(self)
        # Progressive-disclosure skills: discover SKILL.md files BEFORE
        # tool registration so the registry can conditionally enable
        # ``load_skill``.
        from . import skills as skills_module
        self.skills = skills_module.discover_skills(self.workspace.repo_root)
        self._subdir_memory_loaded = set()
        # Guards shared-state mutations (subdir-memory loading, notes) when
        # concurrency-safe tools run in parallel during multi-tool batching.
        self._tool_lock = threading.Lock()
        # File-read dedup cache: path-range key -> (mtime, size). Lets the
        # agent return a short stub when the model re-reads an unchanged file
        # instead of spending context budget on the identical content.
        self._file_read_cache = {}
        self.tools = self.registry.build()
        # Optional hardening: inject decoy tools so the model surface area
        # advertised in the prompt does not match the *real* runtime
        # surface area.  Enabled via harness.decoy_tools=true in config.
        from . import hardening as hardening_module
        harness_cfg = self.config.get("harness", {}) or {}
        if harness_cfg.get("decoy_tools"):
            specs = harness_cfg.get("decoy_tool_specs") or ()
            hardening_module.apply_decoy_tools(self.tools, specs)
        self._hardening = hardening_module

        # Pull project rules text from workspace files declared in config.
        project_rules_text = load_project_rules(
            self.workspace.repo_root,
            self.config.get("project_rules_files") or [],
        )
        # Header-based memory retrieval: pick up to N hierarchical memory
        # files (CLAUDE.md / AGENTS.md / .claude/rules/*.md / CLAUDE.local.md)
        # and append them to the project-rules layer.
        memory_cfg = self.config.get("memory_files") or {}
        if memory_cfg.get("enabled", True):
            selected = memory_files_module.select_memory_files(
                self.workspace.repo_root,
                query=self.session["memory"].get("task", ""),
                max_files=memory_cfg.get("max_files", memory_files_module.DEFAULT_MAX_FILES),
                global_roots=memory_cfg.get("global_roots"),
                user_roots=memory_cfg.get("user_roots"),
                project_paths=memory_cfg.get("project_paths"),
                local_paths=memory_cfg.get("local_paths"),
            )
            self.memory_files = selected
            extra = memory_files_module.render_memory_files(selected)
            if extra:
                project_rules_text = (project_rules_text + "\n\n" + extra).strip()
        else:
            self.memory_files = []
        # Append the skill manifest (names + descriptions only). The bodies
        # remain on disk until ``load_skill`` is called.
        if self.skills:
            manifest = skills_module.render_skill_manifest(self.skills)
            if manifest:
                project_rules_text = (project_rules_text + "\n\n" + manifest).strip()
        # Undercover mode: if CODELET_UNDERCOVER=1, replace the identity
        # layer with a generic helpful-assistant string before the prefix
        # is assembled.  This is intentionally cheap so eval runs do not
        # need a separate config file.
        prompts_cfg = self.config.get("prompts", {}) or {}
        if hardening_module.undercover_enabled():
            prompts_cfg = hardening_module.apply_undercover_identity(prompts_cfg)
        self.prefix = build_prefix(
            prompts_cfg,
            self.tools,
            self.workspace.text(),
            project_rules_text=project_rules_text,
        )
        self.session_path = self.session_store.save(self.session)

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    @staticmethod
    def remember(bucket, item, limit):
        """Move ``item`` to the end of ``bucket`` and cap the bucket length."""
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    # ---- prompt assembly ------------------------------------------------

    def memory_text(self):
        return build_memory_text(self.session["memory"])

    def history_text(self):
        harness = self.config.get("harness", {})
        compaction_cfg = harness.get("compaction") or {}
        target_chars = compaction_cfg.get(
            "target_chars",
            compaction_module.DEFAULT_COMPACTION["target_chars"],
        )
        history = self.session["history"]
        rendered_size = compaction_module.render_history_size(history)
        current_budget = harness.get("max_tool_output", 4000)

        # Compact state: normal / warning / error
        if not hasattr(self, "compact_state"):
            self.compact_state = "normal"
        if rendered_size > target_chars * 1.5:
            self.compact_state = "error"
        elif rendered_size > target_chars * 0.9:
            self.compact_state = "warning"
        else:
            self.compact_state = "normal"

        if rendered_size > target_chars:
            # Pre-compact hook
            if getattr(self, "on_pre_compact", None):
                self.on_pre_compact()
            try:
                outcome = compaction_module.run_cascade(
                    history,
                    current_budget=current_budget,
                    config=compaction_cfg,
                    model_client=self.model_client if compaction_cfg.get("auto_compaction", True) else None,
                    autocompact_tokens=compaction_cfg.get("autocompact_tokens", 512),
                    autocompact_prompt=(self.config.get("prompts") or {}).get("autocompact"),
                )
            except compaction_module.HardHaltError:
                self.compact_state = "error"
                outcome = {
                    "history": history,
                    "budget": current_budget,
                    "stages_applied": ["hard_halt"],
                    "halted": True,
                }
            if outcome.get("halted") and not outcome.get("stages_applied", []) == ["hard_halt"]:
                self.compact_state = "error"
                print(
                    "[warning] compaction cascade could not bring transcript under target; "
                    "continuing with over-budget history",
                    file=sys.stderr,
                )
            history = outcome["history"]
            current_budget = outcome["budget"]
            self.last_compaction_stages = outcome["stages_applied"]
            # Post-compact hook
            if getattr(self, "on_post_compact", None):
                self.on_post_compact()
        else:
            self.last_compaction_stages = []
        return build_history_text(
            history,
            max_tool_output=current_budget,
            max_history=harness.get("max_history", 12000),
        )

    @property
    def context_window(self):
        from .query import get_context_window
        return get_context_window(self._model_name)

    def prompt(self, user_message):
        plan_text = self._active_plan_text()
        message = f"{plan_text}\n\n{user_message}" if plan_text else user_message
        # Inject step counter to remind the agent of its budget
        step_info = ""
        current_step = getattr(self, "_current_tool_step", 0)
        max_step = getattr(self, "_max_tool_step", self.max_steps)
        if current_step > 0:
            remaining = max(0, max_step - current_step)
            step_info = f"\n\n[STEP COUNTER] You have used {current_step} of {max_step} steps. {remaining} steps remaining."
            if current_step >= 2 and remaining > 3:
                step_info += " IMPORTANT: You should have started creating the deliverable by now. Stop inspecting and START CREATING. Use write_file + run_shell to create the output. You are FORBIDDEN from making any more read-only calls."
            if remaining <= 3:
                step_info += " CRITICAL: You are running out of steps. Issue your final answer or create the deliverable NOW."
            if current_step >= 3 and remaining > 2:
                step_info += " WARNING: You have spent too many steps on exploration. If you have not started creating, do so immediately with write_file then run_shell. Do NOT write intermediate scripts or make more inspection calls."
        built = build_prompt(self.prefix, self.memory_text(), self.history_text(), message + step_info)
        # Token budget check
        if self.max_tokens is not None:
            from .query import estimate_tokens
            if estimate_tokens(built) > self.max_tokens:
                raise RuntimeError(f"Prompt exceeds token budget: {estimate_tokens(built)} > {self.max_tokens}")
        return built

    def _active_plan_text(self):
        """Render the active ``decompose`` plan so it is re-consulted each turn.

        The reference agent keeps the explicit plan in view across the whole
        loop rather than recording it once and forgetting it. After the model
        calls ``decompose`` the plan lives at ``session["plan"]``; here we
        surface it on every subsequent prompt so the agent keeps working the
        next unfinished step instead of losing the thread.
        """
        plan = self.session.get("plan")
        if not isinstance(plan, dict) or not plan.get("steps"):
            return ""
        lines = ["<plan>", f"Active plan for: {plan.get('goal', '')}"]
        for index, step in enumerate(plan["steps"], 1):
            lines.append(f"{index}. {step}")
        lines.append(
            "Re-consult this plan each turn and work the next unfinished step. "
            "When every step is done, return your <final> answer."
        )
        lines.append("</plan>")
        return "\n".join(lines)

    # ---- session bookkeeping -------------------------------------------

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def note_tool(self, name, args, result):
        memory = self.session["memory"]
        path = args.get("path")
        if name in {"read_file", "write_file", "patch_file"} and path:
            self.remember(memory["files"], str(path), 8)
        note = f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}"
        self.remember(memory["notes"], note, 5)

    # ---- main loop ------------------------------------------------------

    def ask(self, user_message):
        """Run one user turn through the tool loop.

        Returns the final assistant message as a ``str`` for backward
        compatibility. The full structured outcome is also recorded on
        ``self.last_ask_result`` (an :class:`AskResult`) and the termination
        reason on ``self.last_stop_reason`` (a :class:`StopReason`).
        """
        memory = self.session["memory"]
        memory["task"] = clip(user_message.strip(), 300)
        self.record({"role": "user", "content": user_message, "created_at": now()})
        # Append to global history (mirrors reference agent's history.ts)
        append_history(
            self.workspace.repo_root,
            display=user_message.strip(),
            session_id=self.session["id"],
            project=self.workspace.repo_root,
        )

        retry_template = self.config.get("prompts", {}).get(
            "retry_notice", BUILTIN_DEFAULTS["prompts"]["retry_notice"]
        )
        harness = self.config.get("harness", {})
        repeated_error_threshold = int(harness.get("repeated_error_threshold", 3))
        no_progress_limit = int(harness.get("no_progress_limit", 8))
        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)
        self._current_tool_step = 0
        self._max_tool_step = self.max_steps
        # Snapshot existing deliverables so we only detect NEW ones
        self._preexisting_deliverables = set(self._check_deliverables_created())

        def _finish(final_text, reason):
            self.record({"role": "assistant", "content": final_text, "created_at": now()})
            result = AskResult(
                final=final_text,
                reason=reason,
                tool_steps=tool_steps,
                attempts=attempts,
                compaction_stages=list(self.last_compaction_stages or []),
            )
            self.last_stop_reason = reason
            self.last_ask_result = result
            # Persist cost state so it survives session resume
            save_cost_state(self.workspace.repo_root, self.session["id"], self.cost_tracker)
            return final_text

        while tool_steps < self.max_steps and attempts < max_attempts:
            # Check abort signal
            try:
                self.abort_controller.check()
            except RuntimeError:
                return _finish("Aborted by user.", StopReason.USER_INTERRUPT)
            # Budget check: stop before burning more tokens
            if self.cost_tracker.check_budget(self.max_budget_usd):
                final = (
                    f"Stopped: budget exceeded (${self.cost_tracker.state.total_cost_usd:.4f} "
                    f"/ ${self.max_budget_usd:.4f})."
                )
                return _finish(final, StopReason.BUDGET_EXCEEDED)
            attempts += 1
            try:
                prompt = self.prompt(user_message)
            except compaction_module.HardHaltError:
                final = self._force_compact_history()
                return _finish(final, StopReason.HARD_HALT_RECOVERED)
            if self.inference_start_hook:
                self.inference_start_hook()
            api_start = time.time()
            raw = self.model_client.complete(prompt, self.max_new_tokens)
            api_duration_ms = (time.time() - api_start) * 1000
            if self.inference_end_hook:
                self.inference_end_hook()
            # Track usage if the model client exposes it
            usage = getattr(self.model_client, "last_usage", None)
            if isinstance(usage, dict):
                self.cost_tracker.record_call(
                    model_name=getattr(self.model_client, "model", "unknown"),
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                    cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
                    web_search_requests=usage.get("web_search_requests", 0),
                    api_duration_ms=api_duration_ms,
                )
            # Log raw model output for debugging
            debug_log = Path(self.workspace.cwd) / "_agent_raw.log"
            try:
                with open(debug_log, "a", encoding="utf-8") as f:
                    f.write(f"\n\n=== STEP {tool_steps + 1} ===\n")
                    f.write(f"RAW:\n{raw}\n")
                    f.write(f"PARSED: {kind}\n")
                    if kind == "tool":
                        f.write(f"PAYLOAD: {json.dumps(payload)}\n")
                    elif kind == "final":
                        f.write(f"FINAL: {str(payload)[:500]}\n")
                    elif kind == "retry":
                        f.write(f"RETRY: {str(payload)}\n")
            except Exception:
                pass

            kind, payload = parsing.parse_model_output(raw, retry_template)

            if kind == "tool":
                # Multi-tool batching: a single model turn may emit several
                # independent read-only <tool> calls. Parse them all, cap to
                # the remaining step budget, and execute — parallelizing
                # maximal consecutive runs of concurrency-safe tools.
                calls = parsing.extract_all_tool_payloads(raw)
                if len(calls) <= 1:
                    calls = [{"name": payload.get("name", ""), "args": payload.get("args", {})}]
                remaining = self.max_steps - tool_steps
                if remaining > 0 and len(calls) > remaining:
                    calls = calls[:remaining]
                batch_results = self._execute_tool_batch(calls)
                name = ""
                result = ""
                for name, args, result in batch_results:
                    tool_steps += 1
                    self._current_tool_step = tool_steps
                    if self.tool_output_callback is not None:
                        self.tool_output_callback(name, args, result)
                    self.record(
                        {
                            "role": "tool",
                            "name": name,
                            "args": args,
                            "content": result,
                            "created_at": now(),
                        }
                    )
                    self.note_tool(name, args, result)
                # NEW: Early deliverable detection — if NEW deliverable files exist,
                # force a final answer to prevent wasting steps on rewrites.
                _deliverables = self._check_deliverables_created()
                _new_deliverables = [d for d in _deliverables if d not in getattr(self, "_preexisting_deliverables", set())]
                if _new_deliverables and tool_steps >= 3:
                    _deliv_msg = (
                        f"Task completed. Deliverable file(s) created: "
                        f"{', '.join(_new_deliverables)}"
                    )
                    return _finish(_deliv_msg, StopReason.FINAL)
                # Frustration / repeated-error detector: if the same tool has
                # failed N times in a row, give up the loop instead of letting
                # the model spin against an unfixable error.
                if self._tool_error_streak() >= repeated_error_threshold:
                    # For read-only verification tools (glob, list_files, etc.)
                    # that loop after a successful write/run, synthesise a
                    # positive final answer from the last real result rather
                    # than surfacing a confusing error message.
                    _verification_tools = {"glob", "list_files", "read_file", "search"}
                    if name in _verification_tools:
                        recovered = self._last_successful_action_result(
                            exclude=_verification_tools
                        )
                        if recovered:
                            return _finish(recovered, StopReason.FINAL)
                    final = (
                        f"Gave up after {repeated_error_threshold} consecutive "
                        f"tool errors. Last error from `{name}`: "
                        f"{clip(str(result), 200)}. "
                        "Please clarify the task or the path/arguments."
                    )
                    return _finish(final, StopReason.REPEATED_ERROR_GIVEUP)
                # Diminishing-returns / no-progress circuit breaker: if the
                # model keeps issuing read-only calls that surface no new
                # information (dedup stubs, repeated-call errors, duplicate
                # results), stop instead of burning the whole step budget.
                _np_streak = self._no_progress_streak()
                # DEBUG
                debug_log2 = Path(self.workspace.cwd) / "_agent_raw.log"
                try:
                    with open(debug_log2, "a", encoding="utf-8") as f:
                        f.write(f"[DEBUG] no_progress check: streak={_np_streak}, limit={no_progress_limit}, will_trigger={no_progress_limit and _np_streak >= no_progress_limit}\n")
                except Exception:
                    pass
                if no_progress_limit and _np_streak >= no_progress_limit:
                    # NEW: Auto-rescue FIRST — if the agent wrote a .py script but never ran it,
                    # execute it automatically. This takes priority over recovering past results.
                    _inspection_tools = {"glob", "list_files", "read_file", "search", "run_python"}
                    _auto_rescue_script = self._find_unexecuted_python_script()
                    if _auto_rescue_script:
                        rescue_result = self._execute_tool_batch([
                            {"name": "run_shell", "args": {"command": f"python3 {_auto_rescue_script}", "timeout": 120}}
                        ])
                        if rescue_result:
                            name, args, result = rescue_result[0]
                            self.record({
                                "role": "tool",
                                "name": name,
                                "args": args,
                                "content": result,
                                "created_at": now(),
                            })
                            self.note_tool(name, args, result)
                            # After auto-rescue, try to recover a successful result
                            recovered2 = self._last_successful_action_result(
                                exclude=_inspection_tools
                            )
                            if recovered2:
                                return _finish(recovered2, StopReason.NO_PROGRESS_GIVEUP)
                    
                    # For no-progress loops, don't recover run_python inspection calls
                    # as "successful actions" — they didn't create deliverables.
                    recovered = self._last_successful_action_result(
                        exclude=_inspection_tools
                    )
                    if recovered:
                        return _finish(recovered, StopReason.NO_PROGRESS_GIVEUP)
                    
                    final = (
                        f"Stopped after {no_progress_limit} consecutive tool "
                        "calls that produced no new information. The task may "
                        "need clarification or a different approach."
                    )
                    return _finish(final, StopReason.NO_PROGRESS_GIVEUP)
                continue

            if kind == "retry":
                self.record({"role": "assistant", "content": payload, "created_at": now()})
                continue

            final = (payload or raw).strip()
            self.remember(memory["notes"], clip(final, 220), 5)
            return _finish(final, StopReason.FINAL)

        if attempts >= max_attempts and tool_steps < self.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            return _finish(final, StopReason.ATTEMPT_LIMIT)
        final = "Stopped after reaching the step limit without a final answer."
        return _finish(final, StopReason.STEP_LIMIT)

    def _execute_tool_batch(self, calls):
        """Execute a list of ``{"name","args"}`` tool calls in order.

        Maximal consecutive runs of concurrency-safe (read-only) tools are
        dispatched in parallel via a bounded thread pool; every other tool
        (write/exec/delegate) runs serially. Results are returned as a list
        of ``(name, args, result)`` tuples in the original call order so the
        transcript stays deterministic regardless of completion order.
        """
        results = [None] * len(calls)
        max_workers = max(1, int(self.config.get("harness", {}).get("max_parallel_tools", 4)))
        index = 0
        while index < len(calls):
            name = calls[index].get("name", "")
            args = calls[index].get("args", {}) or {}
            safe = is_concurrency_safe(name) and self.tools.get(name) is not None
            if not safe:
                results[index] = (name, args, self.run_tool(name, args))
                index += 1
                continue
            # Gather the maximal consecutive run of concurrency-safe tools.
            group = []
            cursor = index
            while cursor < len(calls):
                nm = calls[cursor].get("name", "")
                if is_concurrency_safe(nm) and self.tools.get(nm) is not None:
                    group.append(cursor)
                    cursor += 1
                else:
                    break
            if len(group) == 1:
                idx = group[0]
                nm = calls[idx].get("name", "")
                ar = calls[idx].get("args", {}) or {}
                results[idx] = (nm, ar, self.run_tool(nm, ar))
            else:
                with ThreadPoolExecutor(max_workers=min(max_workers, len(group))) as pool:
                    futures = {}
                    for idx in group:
                        nm = calls[idx].get("name", "")
                        ar = calls[idx].get("args", {}) or {}
                        futures[pool.submit(self.run_tool, nm, ar)] = (idx, nm, ar)
                    for future in as_completed(futures):
                        idx, nm, ar = futures[future]
                        try:
                            res = future.result()
                        except Exception as exc:  # pragma: no cover - defensive
                            res = f"error: tool {nm} failed: {exc}"
                        results[idx] = (nm, ar, res)
            index = cursor
        return results

    def _tool_error_streak(self):
        """Count how many consecutive tool calls ended in an error result.

        A result is considered an error if it starts with ``error:`` or
        contains a non-zero subprocess exit code (``exit_code: <non-zero>``).
        Interleaved failures across different tools are counted together so
        the agent cannot bypass the threshold by alternating failing tools.
        """
        streak = 0
        for item in reversed(self.session["history"]):
            if item.get("role") != "tool":
                continue
            content = str(item.get("content", "")).lstrip().lower()
            if content.startswith("error:") or (
                content.startswith("exit_code:")
                and not _re_exit_zero.match(content)
            ):
                streak += 1
            else:
                break
        return streak

    def _no_progress_streak(self):
        """Count trailing consecutive tool calls that made no real progress.

        A result is "non-informative" when it is a file-unchanged dedup stub,
        a repeated-identical-call error, or — for a read-only tool — content
        identical to an earlier read-only result. This circuit-breaker mirrors
        the reference agent's diminishing-returns detector: it stops loops
        where the model keeps inspecting without producing new information,
        which the plain error-streak counter would miss.

        ADDED: Also detects run_python inspection loops. If the agent makes
        3+ run_python calls that only read/inspect without writing output
        within the last 8 tool calls, the loop is considered stuck.
        """
        tool_events = [
            item for item in self.session["history"] if item.get("role") == "tool"
        ]
        if not tool_events:
            return 0
        contents = [str(item.get("content", "")) for item in tool_events]

        def is_inspection_call(i):
            """Check if tool event i is any read-only call that doesn't create deliverables."""
            name = tool_events[i].get("name", "")
            # run_python without write keywords
            if name == "run_python":
                code = str(tool_events[i].get("args", {}).get("code", ""))
                has_write = any(
                    kw in code
                    for kw in [
                        "to_excel", "to_csv", "write(", "save(", "dump(", 
                        "Workbook(", "write_file", "patch_file",
                        "canvas.save", "pdf.save", "doc.save", "Document(",
                        "to_pdf", "savefig", "plt.savefig"
                    ]
                )
                return not has_write
            # All other read-only tools count as inspection
            return name in {"list_files", "read_file", "glob", "search", "web_search", "web_fetch"}

        def non_informative(i):
            content = contents[i]
            if content.startswith("[file unchanged"):
                return True
            if content.lstrip().startswith("error: repeated identical tool call"):
                return True
            name = tool_events[i].get("name", "")
            # Intermediate Python scripts are non-progress — they don't create deliverables
            if name == "write_file":
                path = str(tool_events[i].get("args", {}).get("path", ""))
                if path.endswith(".py"):
                    return True
            if is_concurrency_safe(name):
                for j in range(i):
                    if contents[j] == content and is_concurrency_safe(
                        tool_events[j].get("name", "")
                    ):
                        return True
            return False

        # Classic trailing streak
        streak = 0
        for i in range(len(tool_events) - 1, -1, -1):
            if non_informative(i):
                streak += 1
            else:
                break

        # NEW: Inspection loop detector — ANY 5+ consecutive read-only calls = stuck
        recent_window = min(8, len(tool_events))
        inspection_count = sum(
            1 for j in range(len(tool_events) - recent_window, len(tool_events))
            if is_inspection_call(j)
        )
        # DEBUG: log inspection detection
        debug_log = Path(self.workspace.cwd) / "_agent_raw.log"
        try:
            with open(debug_log, "a", encoding="utf-8") as f:
                f.write(f"\n[DEBUG] _no_progress_streak: tool_events={len(tool_events)}, recent_window={recent_window}, inspection_count={inspection_count}, streak={streak}, will_return={max(streak, 999) if inspection_count >= 5 else streak}\n")
        except Exception:
            pass
        if inspection_count >= 5:
            # Force the streak to exceed the limit so the breaker trips
            return max(streak, 999)

        return streak

    def _find_unexecuted_python_script(self):
        """Find a .py file in the workspace that was written but never executed.

        Returns the relative path of the script, or None if none found.
        """
        # First: scan filesystem for all .py files in workspace
        workspace_path = Path(self.workspace.cwd)
        all_py_files = set()
        for py_path in workspace_path.rglob("*.py"):
            if py_path.is_file() and not py_path.name.startswith("_"):
                all_py_files.add(py_path.name)

        # Find all run_shell executions from history
        executed_scripts = set()
        for item in self.session["history"]:
            if item.get("role") != "tool":
                continue
            name = item.get("name", "")
            args = item.get("args", {})
            if name == "run_shell":
                cmd = str(args.get("command", ""))
                # Extract script name from commands like "python script.py" or "python3 script.py"
                for prefix in ["python ", "python3 ", "python2 "]:
                    if prefix in cmd:
                        parts = cmd.split(prefix)
                        for part in parts[1:]:
                            token = part.strip().split()[0] if part.strip() else ""
                            if token.endswith(".py"):
                                executed_scripts.add(Path(token).name)

        # Find unexecuted scripts that still exist in the workspace
        for script_name in all_py_files:
            if script_name not in executed_scripts:
                script_path = workspace_path / script_name
                if script_path.exists():
                    return str(script_path.relative_to(workspace_path))
        return None

    def _check_deliverables_created(self):
        """Check if any deliverable files have been created in the workspace.

        Returns a list of relative paths of deliverable files found, or empty list.
        Deliverables are files with known output extensions (xlsx, pdf, docx, etc.)
        that are not hidden files or intermediate scripts.
        """
        deliverable_exts = {
            ".pdf", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp",
            ".wav", ".mp3", ".mp4", ".avi", ".mov", ".mkv",
            ".csv", ".html", ".zip", ".txt",
        }
        workspace_path = Path(self.workspace.cwd)
        found = []
        for path in workspace_path.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith("_") or path.name.startswith("."):
                continue
            if path.suffix.lower() in deliverable_exts:
                found.append(str(path.relative_to(workspace_path)))
        return found

    # ---- compaction helpers --------------------------------------------

    def _last_successful_action_result(self, exclude=frozenset()):
        """Return the content of the most recent tool call that is not in
        *exclude* and did not return an error.  Used to synthesise a final
        answer when a verification tool loops after a successful action.
        """
        for item in reversed(self.session["history"]):
            if item.get("role") != "tool":
                continue
            if item.get("name") in exclude:
                continue
            content = str(item.get("content", ""))
            if not content.lstrip().lower().startswith("error:"):
                return content
        return None

    def _force_compact_history(self):
        """Trim the durable session history to the most recent items.

        Called when the compaction cascade raises ``HardHaltError`` (i.e. the
        transcript is so large that even auto-compaction cannot recover it).
        We surgically truncate ``self.session["history"]`` in place so that
        the interactive REPL stays usable on the very next user turn.
        """
        cfg = self.config.get("harness", {}).get("compaction") or {}
        preserve = cfg.get(
            "preserve_recent",
            compaction_module.DEFAULT_COMPACTION["preserve_recent"],
        )
        history = self.session["history"]
        cutoff = max(0, len(history) - preserve)
        self.session["history"] = [
            {
                "role": "assistant",
                "content": "[session history force-compacted: earlier history trimmed to free context]",
                "compacted": True,
                "created_at": now(),
            },
            *history[cutoff:],
        ]
        self.session_path = self.session_store.save(self.session)
        return (
            f"Context limit reached and auto-compaction could not recover the transcript. "
            f"Session history has been trimmed to the {preserve} most recent items. "
            f"Please repeat your last request."
        )

    # ---- tool dispatch --------------------------------------------------

    def run_tool(self, name, args):
        tool = self.tools.get(name)
        if tool is None:
            # Give a targeted redirect when the caller tried to invoke a skill
            # name directly — a common LLM mistake with progressive-disclosure.
            skill_names = [s.name for s in getattr(self, "skills", []) or []]
            if name in skill_names:
                return (
                    f"error: '{name}' is a skill, not a callable tool. "
                    f"Call load_skill(name=\"{name}\") first to retrieve its "
                    f"instructions, then follow them."
                )
            return f"error: unknown tool '{name}'"
        # Best-effort repair of near-miss argument names/types before
        # validation so a small formatting slip does not waste a turn.
        args = repair_tool_args(tool.get("schema") or {}, args)
        try:
            self.validate_tool(name, args)
        except Exception as exc:
            example = self.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            return message
        if self.repeated_tool_call(name, args):
            return (
                f"error: repeated identical tool call for {name} — "
                "you have already seen this result. "
                "Do NOT call any more tools. "
                "Issue <final>…your answer here…</final> RIGHT NOW."
            )
        if tool["risky"] and not self.approve(name, args):
            return f"error: approval denied for {name}"
        # Walk-down lazy memory loading: when a tool touches a subdirectory
        # that carries its own AGENT.md / CLAUDE.md / AGENTS.md, surface its
        # first ~800 chars into the working memory notes once.
        self._maybe_load_subdir_memory(args)
        try:
            result = clip(tool["run"](args))
        except Exception as exc:
            return f"error: tool {name} failed: {exc}"
        return self._dedup_file_read(name, args, result)

    def _dedup_file_read(self, name, args, result):
        """Return a short stub when ``name`` re-reads an unchanged file.

        Mirrors the reference agent's ``FILE_UNCHANGED_STUB``: the first read
        of a file returns its full content; a later read of the same path and
        line range, while the file's mtime and size are unchanged, returns a
        compact pointer back to the earlier result instead of re-spending the
        context budget. Any error result is passed through untouched so the
        model still sees failures.
        """
        fileread_tools = (
            self.config.get("harness", {})
            .get("compaction", {})
            .get("fileread_tools", ["read_file"])
        )
        if name not in fileread_tools or not isinstance(args, dict):
            return result
        if isinstance(result, str) and result.startswith("error:"):
            return result
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return result
        try:
            stat = self.path(path).stat()
        except Exception:
            return result
        rng = (args.get("start"), args.get("end"))
        key = (str(path), rng)
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        with self._tool_lock:
            previous = self._file_read_cache.get(key)
            self._file_read_cache[key] = signature
        if previous is not None and previous == signature:
            where = f"`{path}`"
            if rng != (None, None):
                where += f" (lines {rng[0]}-{rng[1]})"
            return (
                f"[file unchanged since your earlier read of {where}; content "
                "omitted to save context — reuse the earlier result. If you "
                "expected a change, the file was not modified.]"
            )
        return result

    def _maybe_load_subdir_memory(self, args):
        """Pull in the nearest subdir-level AGENT.md (lazy, idempotent)."""
        if not isinstance(args, dict):
            return
        # Serialise the read-modify-write of the shared ``seen`` set and notes
        # list so parallel concurrency-safe tools cannot race here.
        with self._tool_lock:
            candidates = []
            for key in ("path", "src", "dst"):
                raw = args.get(key)
                if isinstance(raw, str) and raw:
                    candidates.append(raw)
            seen = self._subdir_memory_loaded
            notes = self.session["memory"].setdefault("notes", [])
            repo_root = Path(self.workspace.repo_root).resolve()
            for raw in candidates:
                try:
                    resolved = self.path(raw)
                except Exception:
                    continue
                anchor = resolved if resolved.is_dir() else resolved.parent
                # Walk from the anchor up to (but not including) repo root.
                cur = anchor
                while cur != repo_root and repo_root in cur.parents:
                    for fname in ("AGENT.md", "AGENTS.md", "CLAUDE.md"):
                        candidate = cur / fname
                        if not candidate.is_file():
                            continue
                        key = str(candidate)
                        if key in seen:
                            break
                        seen.add(key)
                        try:
                            body = candidate.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            break
                        snippet = body.strip().splitlines()[0] if body.strip() else ""
                        rel = candidate.relative_to(repo_root)
                        self.remember(notes, f"loaded subdir memory {rel}: {snippet[:200]}",
                                      self.config.get("harness", {}).get("notes_limit", 16))
                        return
                    cur = cur.parent

    def repeated_tool_call(self, name, args):
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if not tool_events:
            return False
        last = tool_events[-1]
        return last["name"] == name and last["args"] == args

    def tool_example(self, name):
        return (self.config.get("prompts", {}).get("examples") or {}).get(name, "")

    def validate_tool(self, name, args):
        tool_argument_validators(self, name, args)

    def approve(self, name, args):
        if self.read_only:
            self.permission_context.record_denial(name, args)
            self.permission_denials.append({"tool_name": name, "tool_input": args})
            return False

        # Check granular permission rules first
        decision = self.permission_context.check(
            name, args, self.approval_policy, self.read_only
        )
        if decision == "deny":
            self.permission_context.record_denial(name, args)
            self.permission_denials.append({"tool_name": name, "tool_input": args})
            return False
        if decision == "allow":
            return True

        # Fall back to global approval policy
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            self.permission_context.record_denial(name, args)
            self.permission_denials.append({"tool_name": name, "tool_input": args})
            return False
        # YOLO classifier: when enabled, auto-approve obviously-safe shell
        # commands so the agent does not nag on harmless `ls`/`pwd` calls.
        harness_cfg = self.config.get("harness", {}) or {}
        if harness_cfg.get("yolo_classifier") and name == "run_shell":
            if self._hardening.is_safe_command(str(args.get("command", ""))):
                return True
        if self.approve_hook is not None:
            self.approve_hook()
        try:
            answer = input(
                f"\napprove {name} {json.dumps(args, ensure_ascii=True)}\n"
                "  [y/N] "
            )
        except EOFError:
            return False
        approved = answer.strip().lower() in {"y", "yes"}
        if not approved:
            self.permission_context.record_denial(name, args)
            self.permission_denials.append({"tool_name": name, "tool_input": args})
        return approved

    # ---- parsing helpers (kept as classmethods for backward compat) -----

    @staticmethod
    def parse(raw):
        retry_template = BUILTIN_DEFAULTS["prompts"]["retry_notice"]
        return parsing.parse_model_output(raw, retry_template)

    @staticmethod
    def retry_notice(problem=None):
        return parsing.retry_notice(BUILTIN_DEFAULTS["prompts"]["retry_notice"], problem)

    @staticmethod
    def parse_xml_tool(raw):
        return parsing.parse_xml_tool(raw)

    @staticmethod
    def parse_attrs(text):
        return parsing.parse_attrs(text)

    @staticmethod
    def extract(text, tag):
        return parsing.extract(text, tag)

    @staticmethod
    def extract_raw(text, tag):
        return parsing.extract_raw(text, tag)

    # ---- session management --------------------------------------------

    def reset(self):
        self.session["history"] = []
        self.session["memory"] = {"task": "", "files": [], "notes": []}
        self.session_store.save(self.session)

    # ---- path safety ---------------------------------------------------

    def path_is_within_root(self, resolved):
        probe = resolved
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        for candidate in (probe, *probe.parents):
            try:
                if candidate.samefile(self.root):
                    return True
            except OSError:
                continue
        return False

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if not self.path_is_within_root(resolved):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    # ---- cost tracking accessors (for test compatibility) -------------

    @property
    def total_cost_usd(self):
        return self.cost_tracker.state.total_cost_usd

    @property
    def token_usage(self):
        tu = self.cost_tracker.state.token_usage
        return {
            "input_tokens": tu.input_tokens,
            "output_tokens": tu.output_tokens,
            "cache_read_input_tokens": tu.cache_read_input_tokens,
            "cache_creation_input_tokens": tu.cache_creation_input_tokens,
            "web_search_requests": tu.web_search_requests,
        }

    @property
    def model_usage(self):
        return {
            name: {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_read_input_tokens": u.cache_read_input_tokens,
                "cache_creation_input_tokens": u.cache_creation_input_tokens,
                "web_search_requests": u.web_search_requests,
                "cost_usd": u.cost_usd,
            }
            for name, u in self.cost_tracker.state.model_usage.items()
        }

    @property
    def commands(self):
        return get_commands(self)

    def spawn_task(self, task_type, description, command="", timeout=None):
        """Spawn a background task."""
        from .tasks import spawn_task as _spawn
        return _spawn(self, task_type, description, command, timeout)

    def kill_task(self, task_id):
        """Kill a background task."""
        from .tasks import kill_task as _kill
        return _kill(task_id)

    # ---- thin tool wrappers (kept for backward compatibility) ----------

    def tool_list_files(self, args):
        return self.registry.tool_list_files(args)

    def tool_read_file(self, args):
        return self.registry.tool_read_file(args)

    def tool_search(self, args):
        return self.registry.tool_search(args)

    def tool_glob(self, args):
        return self.registry.tool_glob(args)

    def tool_run_shell(self, args):
        return self.registry.tool_run_shell(args)

    def tool_run_python(self, args):
        return self.registry.tool_run_python(args)

    def tool_write_file(self, args):
        return self.registry.tool_write_file(args)

    def tool_patch_file(self, args):
        return self.registry.tool_patch_file(args)

    def tool_delegate(self, args):
        return self.registry.tool_delegate(args)

    def build_tools(self):
        """Backwards-compat shim: rebuild the tool registry."""
        return ToolRegistry(self).build()

    def build_prefix(self):
        """Backwards-compat shim: re-render the stable prompt prefix."""
        project_rules_text = load_project_rules(
            self.workspace.repo_root,
            self.config.get("project_rules_files") or [],
        )
        return build_prefix(
            self.config.get("prompts", {}),
            self.tools,
            self.workspace.text(),
            project_rules_text=project_rules_text,
        )
