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
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import baseline as baseline_module
from . import compaction as compaction_module
from . import memory_files as memory_files_module
from .config import BUILTIN_DEFAULTS, deep_merge, load_project_rules
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
    ):
        # Use packaged defaults if no config was provided. ``deep_merge``
        # gives us a fresh, mutation-safe copy.
        self.config = deep_merge(BUILTIN_DEFAULTS, config or {})
        # Apply sandbox policy from the config (if any).
        sandbox_module.apply_config(self.config.get("sandbox") or {})

        harness = self.config.get("harness", {})

        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
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

        # Session-baseline verification: every session begins with a check
        # against the physical repository state to prevent compounding
        # hallucinations across disjointed runs.
        self.baseline_drift = baseline_module.verify_session_baseline(
            self.session,
            self.workspace.repo_root,
            watch_files=self.config.get("project_rules_files") or [],
        )

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
        # Undercover mode: if MINI_AGENT_UNDERCOVER=1, replace the identity
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
        # Run the graduated compaction cascade over a copy of the history
        # whenever its rendered size exceeds the soft target. The agent never
        # mutates ``self.session["history"]`` here; the durable transcript
        # remains intact so resuming a session keeps full fidelity.
        target_chars = compaction_cfg.get(
            "target_chars",
            compaction_module.DEFAULT_COMPACTION["target_chars"],
        )
        history = self.session["history"]
        rendered_size = compaction_module.render_history_size(history)
        current_budget = harness.get("max_tool_output", 4000)
        if rendered_size > target_chars:
            outcome = compaction_module.run_cascade(
                history,
                current_budget=current_budget,
                config=compaction_cfg,
                model_client=self.model_client if compaction_cfg.get("auto_compaction", True) else None,
                autocompact_tokens=compaction_cfg.get("autocompact_tokens", 512),
                autocompact_prompt=(self.config.get("prompts") or {}).get("autocompact"),
            )
            if outcome.get("halted"):
                print(
                    "[warning] compaction cascade could not bring transcript under target; "
                    "continuing with over-budget history",
                    file=sys.stderr,
                )
            history = outcome["history"]
            current_budget = outcome["budget"]
            self.last_compaction_stages = outcome["stages_applied"]
        else:
            self.last_compaction_stages = []
        return build_history_text(
            history,
            max_tool_output=current_budget,
            max_history=harness.get("max_history", 12000),
        )

    def prompt(self, user_message):
        plan_text = self._active_plan_text()
        message = f"{plan_text}\n\n{user_message}" if plan_text else user_message
        return build_prompt(self.prefix, self.memory_text(), self.history_text(), message)

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

        retry_template = self.config.get("prompts", {}).get(
            "retry_notice", BUILTIN_DEFAULTS["prompts"]["retry_notice"]
        )
        harness = self.config.get("harness", {})
        repeated_error_threshold = int(harness.get("repeated_error_threshold", 3))
        no_progress_limit = int(harness.get("no_progress_limit", 8))
        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

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
            return final_text

        while tool_steps < self.max_steps and attempts < max_attempts:
            attempts += 1
            try:
                prompt = self.prompt(user_message)
            except compaction_module.HardHaltError:
                final = self._force_compact_history()
                return _finish(final, StopReason.HARD_HALT_RECOVERED)
            raw = self.model_client.complete(prompt, self.max_new_tokens)
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
                if no_progress_limit and self._no_progress_streak() >= no_progress_limit:
                    recovered = self._last_successful_action_result(
                        exclude={"glob", "list_files", "read_file", "search"}
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
        """
        tool_events = [
            item for item in self.session["history"] if item.get("role") == "tool"
        ]
        if not tool_events:
            return 0
        contents = [str(item.get("content", "")) for item in tool_events]

        def non_informative(i):
            content = contents[i]
            if content.startswith("[file unchanged"):
                return True
            if content.lstrip().startswith("error: repeated identical tool call"):
                return True
            name = tool_events[i].get("name", "")
            if is_concurrency_safe(name):
                for j in range(i):
                    if contents[j] == content and is_concurrency_safe(
                        tool_events[j].get("name", "")
                    ):
                        return True
            return False

        streak = 0
        for i in range(len(tool_events) - 1, -1, -1):
            if non_informative(i):
                streak += 1
            else:
                break
        return streak

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
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        # YOLO classifier: when enabled, auto-approve obviously-safe shell
        # commands so the agent does not nag on harmless `ls`/`pwd` calls.
        harness_cfg = self.config.get("harness", {}) or {}
        if harness_cfg.get("yolo_classifier") and name == "run_shell":
            if self._hardening.is_safe_command(str(args.get("command", ""))):
                return True
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

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
