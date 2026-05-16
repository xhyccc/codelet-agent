"""Parsing of model output into tool/final/retry events."""

import json
import re


def retry_notice(template, problem=None):
    """Render the retry-notice template with an optional problem suffix."""
    suffix = f": {problem}" if problem else ": model returned malformed tool output"
    return template.format(problem_suffix=suffix)


def parse_attrs(text):
    """Return a dict of ``key="value"`` / ``key='value'`` attributes from `text`."""
    attrs = {}
    for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
        attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return attrs


def extract(text, tag):
    """Return the contents of the first ``<tag>...</tag>``, stripped."""
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return text[start:].strip()
    return text[start:end].strip()


def extract_raw(text, tag):
    """Return the contents of the first ``<tag>...</tag>`` without stripping."""
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return text[start:]
    return text[start:end]


def parse_xml_tool(raw):
    """Parse the XML-style ``<tool name="...">...</tool>`` form, or return None."""
    match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
    if not match:
        return None
    attrs = parse_attrs(match.group("attrs"))
    name = str(attrs.pop("name", "")).strip()
    if not name:
        return None

    body = match.group("body")
    args = dict(attrs)
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        if f"<{key}>" in body:
            args[key] = extract_raw(body, key)

    body_text = body.strip("\n")
    if name == "write_file" and "content" not in args and body_text:
        args["content"] = body_text
    if name == "delegate" and "task" not in args and body_text:
        args["task"] = body_text.strip()
    return {"name": name, "args": args}


def parse_model_output(raw, retry_template):
    """Classify a raw model response.

    Returns ``("tool", payload)``, ``("final", text)``, or
    ``("retry", retry_notice_text)``.
    """
    raw = str(raw)
    if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
        body = extract(raw, "tool")
        try:
            payload = json.loads(body)
        except Exception:
            return "retry", retry_notice(retry_template, "model returned malformed tool JSON")
        if not isinstance(payload, dict):
            return "retry", retry_notice(retry_template, "tool payload must be a JSON object")
        if not str(payload.get("name", "")).strip():
            return "retry", retry_notice(retry_template, "tool payload is missing a tool name")
        args = payload.get("args", {})
        if args is None:
            payload["args"] = {}
        elif not isinstance(args, dict):
            return "retry", retry_notice(retry_template)
        return "tool", payload
    if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
        payload = parse_xml_tool(raw)
        if payload is not None:
            return "tool", payload
        return "retry", retry_notice(retry_template)
    if "<final>" in raw:
        final = extract(raw, "final").strip()
        if final:
            return "final", final
        return "retry", retry_notice(retry_template, "model returned an empty <final> answer")
    raw = raw.strip()
    if raw:
        return "final", raw
    return "retry", retry_notice(retry_template, "model returned an empty response")
