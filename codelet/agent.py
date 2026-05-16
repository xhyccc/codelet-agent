"""The :class:`MiniAgent` orchestrates the tool loop.

The agent ties together: workspace context (``workspace``), prompt assembly
(``prompt``), parsing (``parsing``), the tool registry (``tools``), and the
durable session store (``sessions``). All knobs that used to be hard-coded
(token budgets, history caps, tool examples, rules, sandbox patterns,
retry-notice text) now flow in via the YAML-backed config object.
"""

import json
import sys
import uuid
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
from .tools import ToolRegistry, tool_argument_validators
from .utils import clip, now


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
        return build_prompt(self.prefix, self.memory_text(), self.history_text(), user_message)

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
        if not memory["task"]:
            memory["task"] = clip(user_message.strip(), 300)
        self.record({"role": "user", "content": user_message, "created_at": now()})

        retry_template = self.config.get("prompts", {}).get(
            "retry_notice", BUILTIN_DEFAULTS["prompts"]["retry_notice"]
        )
        harness = self.config.get("harness", {})
        repeated_error_threshold = int(harness.get("repeated_error_threshold", 3))
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
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                result = self.run_tool(name, args)
                if self.tool_output_callback is not None:
                    self.tool_output_callback(name, result)
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
                if self._tool_error_streak(name) >= repeated_error_threshold:
                    final = (
                        f"Gave up after {repeated_error_threshold} consecutive "
                        f"errors from `{name}`. Last error: "
                        f"{clip(str(result), 200)}. "
                        "Please clarify the task or the path/arguments."
                    )
                    return _finish(final, StopReason.REPEATED_ERROR_GIVEUP)
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

    def _tool_error_streak(self, name):
        """Count how many consecutive tool calls for ``name`` ended in an
        error result (string starting with ``error:``).
        """
        streak = 0
        for item in reversed(self.session["history"]):
            if item.get("role") != "tool":
                continue
            if item.get("name") != name:
                break
            content = str(item.get("content", ""))
            if content.lstrip().lower().startswith("error:"):
                streak += 1
            else:
                break
        return streak

    # ---- compaction helpers --------------------------------------------

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
            return f"error: unknown tool '{name}'"
        try:
            self.validate_tool(name, args)
        except Exception as exc:
            example = self.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            return message
        if self.repeated_tool_call(name, args):
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        if tool["risky"] and not self.approve(name, args):
            return f"error: approval denied for {name}"
        # Walk-down lazy memory loading: when a tool touches a subdirectory
        # that carries its own AGENT.md / CLAUDE.md / AGENTS.md, surface its
        # first ~800 chars into the working memory notes once.
        self._maybe_load_subdir_memory(args)
        try:
            return clip(tool["run"](args))
        except Exception as exc:
            return f"error: tool {name} failed: {exc}"

    def _maybe_load_subdir_memory(self, args):
        """Pull in the nearest subdir-level AGENT.md (lazy, idempotent)."""
        if not isinstance(args, dict):
            return
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
                    break
                cur = cur.parent

    def repeated_tool_call(self, name, args):
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

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
