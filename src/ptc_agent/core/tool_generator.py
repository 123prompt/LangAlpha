"""Tool Function Generator - MCP tool schemas to sandbox wrapper modules.

The sandbox MCP client itself is NOT generated: it ships verbatim from
``sandbox/mcp_client_runtime.py`` (a static, lintable, directly-testable
module). ``generate_mcp_client_code`` composes that source with a JSON
config epilogue carrying all per-workspace variance; this module otherwise
generates only the per-server wrapper functions and their docs.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

from ptc_agent.agent.provenance.types import RESULT_BODY_MAX_BYTES
from ptc_agent.config.core import MCPServerConfig

from .mcp_registry import MCPToolInfo
from .mcp_sanitize import (
    discovery_should_use_secrets,
    is_user_server,
    sanitize_tool_name,
    sanitize_tool_set,
    sanitize_tool_text,
)

logger = structlog.get_logger(__name__)


_RUNTIME_FILE = Path(__file__).parent / "sandbox" / "mcp_client_runtime.py"


@lru_cache(maxsize=1)
def client_runtime_source() -> str:
    """The static sandbox client runtime, uploaded verbatim (plus epilogue)."""
    return _RUNTIME_FILE.read_text(encoding="utf-8")


# Version of the uploaded mcp_client.py. The manifest hashes generation
# *inputs* (MCP server files, tool schemas, user config) — not this package's
# source — so a pure client/codegen change is otherwise invisible to
# sync_sandbox_assets and never reaches a reused sandbox. Folding this value
# into the tool_modules version forces the regenerated client onto every
# workspace on its next sync (see sandbox/assets.py:_compute_sandbox_manifest).
#
# The hash suffix tracks mcp_client_runtime.py automatically, so editing the
# runtime needs no manual bump. Bump the major only when the WRAPPER/composition
# logic in this file changes in a way that must reach existing sandboxes.
# "4": MCP 2026-07-28 client. "5": client extracted to the static runtime
# module; per-workspace config moved to a JSON epilogue.
_WRAPPER_CODEGEN_MAJOR = "5"

MCP_CLIENT_CODEGEN_VERSION = "{}.{}".format(
    _WRAPPER_CODEGEN_MAJOR,
    hashlib.sha256(client_runtime_source().encode("utf-8")).hexdigest()[:12],
)

# Aggregate per-execution ceiling on result_body bytes emitted BY THE SANDBOX
# CLIENT, shipped to it via the config epilogue. This keeps a cooperative run's
# trace small (per call still capped at RESULT_BODY_MAX_BYTES; this bounds
# their sum to ~4 MiB). It is NOT a host-memory security bound: MCP_TRACE_FILE
# is visible to agent code, which can append to the JSONL directly and bypass
# this counter. The hard host-side bound on what _collect_mcp_trace reads lives
# in ptc_sandbox (it sizes the file and skips one far past any legit trace).
RESULT_BODY_TRACE_BUDGET_BYTES = 4 * 1024 * 1024


def _safe_func_name(name: str) -> str:
    """Map an MCP tool name to a wrapper function name.

    Uses the shared identifier sanitizer; falls back to a stable placeholder so
    codegen never emits an illegal ``def`` (collision detection happens upstream
    in :func:`mcp_sanitize.sanitize_tool_set`).
    """
    return sanitize_tool_name(name) or "_invalid_tool"


class ToolFunctionGenerator:
    """Generates Python function code from MCP tool schemas."""

    def generate_tool_module(
        self, server_name: str, tools: list[MCPToolInfo], *, untrusted: bool = False
    ) -> str:
        """Generate a complete Python module for a server's tools.

        Args:
            server_name: Name of the MCP server
            tools: List of tools from this server
            untrusted: True for user-configured servers (any non-builtin
                source): tool names are validated/de-collided and descriptions
                sanitized. Trusted builtins keep their text verbatim.

        Returns:
            Complete Python module code as string
        """
        logger.debug(
            "Generating tool module",
            server=server_name,
            tool_count=len(tools),
        )

        code = f'''"""
Auto-generated tool functions for MCP server: {server_name}

This module provides Python functions that call tools on the {server_name} MCP server.
Functions are automatically generated from the MCP tool schemas.
"""

from typing import Any, List, Dict
import json

# Import MCP client
try:
    from .mcp_client import _call_mcp_tool
except ImportError:
    # Fallback for when mcp_client is not available
    def _call_mcp_tool(server_name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError(
            "MCP client not initialized. "
            "This module must be used within a PTC sandbox with mcp_client.py installed."
        )


'''

        # For untrusted servers, validate + de-collide tool names so one
        # hostile/duplicate name can't break the module (builtins keep their
        # historical behavior; they are trusted and already collision-free).
        if untrusted:
            sanitized = sanitize_tool_set(tools)
            if sanitized.skipped:
                logger.warning(
                    "Skipped invalid tools for untrusted MCP server",
                    server=server_name,
                    skipped=sanitized.skipped,
                )
            tools = sanitized.kept

        # Generate functions for each tool
        for tool in tools:
            code += self._generate_function(tool, server_name, untrusted)
            code += "\n\n"

        return code

    def _generate_function(
        self, tool: MCPToolInfo, server_name: str, untrusted: bool = False
    ) -> str:
        """Generate Python function for a single tool.

        Args:
            tool: Tool information from MCP server
            server_name: Name of the MCP server this tool belongs to
            untrusted: sanitize names/text for user-configured servers;
                trusted builtin output is unchanged

        Returns:
            Python function code
        """
        # Generate function signature
        func_name = _safe_func_name(tool.name)
        params = tool.get_parameters()

        # For untrusted servers, coerce each param NAME into a legal
        # identifier (a hostile schema key could otherwise inject code or break
        # the module); skip names that can't be salvaged. Builtins keep the raw
        # key verbatim.
        if untrusted:
            usable: dict[str, dict[str, Any]] = {}
            for param_name, param_info in params.items():
                safe_param = sanitize_tool_name(param_name)
                if safe_param is None or safe_param in usable:
                    logger.warning(
                        "Skipped invalid/colliding param for untrusted MCP tool",
                        server=server_name,
                        tool=tool.name,
                        param=param_name,
                    )
                    continue
                usable[safe_param] = param_info
            params = usable

        # Build parameter list - required parameters must come before optional
        param_list = []

        # First add required parameters
        for param_name, param_info in params.items():
            if param_info["required"]:
                param_type = self._map_json_type_to_python(param_info["type"])
                param_list.append(f"{param_name}: {param_type}")

        # Then add optional parameters
        for param_name, param_info in params.items():
            if not param_info["required"]:
                param_type = self._map_json_type_to_python(param_info["type"])
                default = param_info.get("default")
                if default is None:
                    param_list.append(f"{param_name}: {param_type} | None = None")
                else:
                    default_repr = repr(default)
                    param_list.append(f"{param_name}: {param_type} = {default_repr}")

        param_str = ", ".join(param_list)

        # Generate docstring
        docstring = self._generate_docstring(tool, params, untrusted)

        # Generate function body. For untrusted servers the arg-dict KEY is
        # emitted via repr (the param name is untrusted text); builtins keep
        # the historical double-quoted literal.
        if untrusted:
            arg_dict_entries = [
                f"        {param_name!r}: {param_name}," for param_name in params
            ]
        else:
            arg_dict_entries = [
                f'        "{param_name}": {param_name},' for param_name in params
            ]

        args_dict = "\n".join(arg_dict_entries)

        # Extract return type from description for better type hints
        return_type, _ = self._extract_return_info(tool.description)

        # Untrusted tool names are hostile-capable text — emit server/tool via
        # repr so a hostile name can't escape the string literal and inject
        # code. Builtins keep the historical double-quoted literal.
        if untrusted:
            call_line = (
                f"    return _call_mcp_tool({server_name!r}, {tool.name!r}, arguments)"
            )
        else:
            call_line = (
                f'    return _call_mcp_tool("{server_name}", "{tool.name}", arguments)'
            )

        return f'''def {func_name}({param_str}) -> {return_type}:
    """{docstring}"""
    arguments = {{
{args_dict}
    }}

    # Remove None values
    arguments = {{k: v for k, v in arguments.items() if v is not None}}

{call_line}'''

    def _generate_docstring(
        self, tool: MCPToolInfo, params: dict[str, Any], untrusted: bool = False
    ) -> str:
        """Generate docstring for a tool function.

        Args:
            tool: Tool information
            params: Parameter information
            untrusted: full untrusted-text sanitization for user-configured
                servers; trusted builtins keep the backslash-only escape

        Returns:
            Formatted docstring
        """

        def _escape(text: str) -> str:
            # Untrusted text is fully sanitized — triple-quote breakouts,
            # control chars, length cap. Builtins keep the historical
            # backslash-only escape (sanitization could truncate legitimate
            # long builtin docstrings).
            if untrusted:
                return sanitize_tool_text(text)
            return text.replace("\\", "\\\\")

        lines = []

        # Add description
        if tool.description:
            lines.append(_escape(tool.description))
            lines.append("")

        # Add parameters
        if params:
            lines.append("Args:")
            for param_name, param_info in params.items():
                param_desc = param_info.get("description", "")
                escaped_desc = _escape(param_desc)
                # The schema `type` field is untrusted text like the
                # description — escape it (and coerce non-str values) so it
                # can't terminate the docstring.
                param_type = _escape(str(param_info["type"]))
                required = " (required)" if param_info["required"] else ""
                lines.append(
                    f"    {param_name} ({param_type}){required}: {escaped_desc}"
                )
            lines.append("")

        # Add returns - extract from description if available
        return_type, return_desc = self._extract_return_info(tool.description)
        lines.append("Returns:")
        # Format multiline return descriptions properly
        return_lines = return_desc.split("\n")
        first_line = return_lines[0].strip()
        if return_type != "Any":
            lines.append(f"    {return_type}: {first_line}")
        else:
            lines.append(f"    {first_line}")
        # Add remaining lines with proper indentation
        for line in return_lines[1:]:
            stripped = line.strip()
            if stripped:
                lines.append(f"    {stripped}")
        lines.append("")

        # Add example
        example_args = []
        for param_name, param_info in params.items():
            if param_info["required"]:
                example_val = self._generate_example_value(param_info["type"])
                example_args.append(f"{param_name}={example_val}")

        if example_args:
            func_name = _safe_func_name(tool.name)
            example_call = (
                f"{func_name}({', '.join(example_args[:2])})"  # Limit to 2 args
            )
            lines.append("Example:")
            lines.append(f"    result = {example_call}")

        return "\n    ".join(lines)

    def _map_json_type_to_python(self, json_type: str) -> str:
        """Map JSON schema type to Python type hint.

        Args:
            json_type: JSON schema type

        Returns:
            Python type hint string
        """
        type_map = {
            "string": "str",
            "number": "float",
            "integer": "int",
            "boolean": "bool",
            "array": "List",
            "object": "Dict",
            "null": "None",
        }

        # A hostile schema may carry a non-str (unhashable) `type`.
        if not isinstance(json_type, str):
            return "Any"
        return type_map.get(json_type, "Any")

    def _generate_example_value(self, param_type: str) -> str:
        """Generate example value for a parameter type.

        Args:
            param_type: Parameter type

        Returns:
            Example value as string
        """
        examples = {
            "string": '"example"',
            "number": "42.0",
            "integer": "42",
            "boolean": "True",
            "array": "[]",
            "object": "{}",
        }

        # A hostile schema may carry a non-str (unhashable) `type`.
        if not isinstance(param_type, str):
            return '""'
        return examples.get(param_type, '""')

    def _extract_return_info(self, description: str) -> tuple[str, str]:
        """Extract return type info from tool description's Returns: section.

        Parses the description to find a Returns: section and extracts:
        - return_type: A type hint string (e.g., "dict", "list[dict]")
        - return_description: The description of what's returned

        Args:
            description: Tool description that may contain Returns: section

        Returns:
            Tuple of (return_type, return_description)
            Returns ("Any", "Tool execution result") if no Returns: section found
        """
        import re

        if not description:
            return ("Any", "Tool execution result")

        # Look for "Returns:" section in description
        # Pattern matches "Returns:" followed by content until next section or end
        returns_pattern = r"Returns?:\s*\n?\s*(.*?)(?:\n\s*(?:Args?:|Example|Note|Raises?:|HIGH PTC|VERY HIGH|MEDIUM PTC|$)|\Z)"
        match = re.search(returns_pattern, description, re.IGNORECASE | re.DOTALL)

        if not match:
            return ("Any", "Tool execution result")

        returns_text = match.group(1).strip()

        # If returns_text is empty, return default
        if not returns_text:
            return ("Any", "Tool execution result")

        # Try to extract type hint from common patterns:
        # "dict: {...}" or "dict with..." or "Dictionary containing..."
        # "list[dict]" or "List of dicts"
        type_hint = "Any"

        type_patterns = [
            (r"^(dict|Dict)\s*[:{]", "dict"),
            (r"^(list|List)\s*\[?\s*(dict|Dict)", "list[dict]"),
            (r"^(list|List)\b", "list"),
            (r"^(str|string)\b", "str"),
            (r"^(int|integer)\b", "int"),
            (r"^(float|number)\b", "float"),
            (r"^(bool|boolean)\b", "bool"),
            (r"[Dd]ictionary\s+(?:with|containing)", "dict"),
            (r"[Ll]ist\s+of\s+(?:dict|record)", "list[dict]"),
        ]

        for pattern, hint in type_patterns:
            if re.search(pattern, returns_text, re.IGNORECASE):
                type_hint = hint
                break

        return (type_hint, returns_text)

    def generate_tool_documentation(
        self, tool: MCPToolInfo, *, untrusted: bool = False
    ) -> str:
        """Generate markdown documentation for a tool.

        Args:
            tool: Tool information
            untrusted: sanitize description text for user-configured servers;
                trusted builtin output is unchanged

        Returns:
            Markdown documentation string
        """
        func_name = _safe_func_name(tool.name)
        params = tool.get_parameters()
        description = (
            sanitize_tool_text(tool.description) if untrusted else tool.description
        )

        # Build signature
        param_list = []
        for param_name, param_info in params.items():
            param_type = self._map_json_type_to_python(param_info["type"])
            if param_info["required"]:
                param_list.append(f"{param_name}: {param_type}")
            else:
                default = param_info.get("default", "None")
                param_list.append(f"{param_name}: {param_type} = {default}")

        signature = f"{func_name}({', '.join(param_list)})"

        # Build documentation
        doc = f"# {signature}\n\n"

        if description:
            doc += f"{description}\n\n"

        doc += "## Parameters\n\n"
        if params:
            for param_name, param_info in params.items():
                required_marker = (
                    "**Required**" if param_info["required"] else "Optional"
                )
                param_type = str(param_info["type"])
                param_desc = param_info.get("description", "")
                if untrusted:
                    param_type = sanitize_tool_text(param_type)
                    param_desc = sanitize_tool_text(param_desc)
                doc += f"- `{param_name}` ({param_type}) - {required_marker}\n"
                if param_desc:
                    doc += f"  {param_desc}\n"
                doc += "\n"
        else:
            doc += "No parameters\n\n"

        doc += "## Returns\n\n"
        return_type, return_desc = self._extract_return_info(tool.description)
        if untrusted:
            return_desc = sanitize_tool_text(return_desc)
        doc += f"**Type:** `{return_type}`\n\n"
        doc += f"{return_desc}\n\n"

        doc += "## Example\n\n"
        doc += "```python\n"
        doc += f"from tools.{tool.server_name} import {func_name}\n\n"

        # Generate example call
        example_args = []
        for param_name, param_info in params.items():
            if param_info["required"]:
                example_val = self._generate_example_value(param_info["type"])
                example_args.append(f"{param_name}={example_val}")

        if example_args:
            doc += f"result = {func_name}({', '.join(example_args)})\n"
        else:
            doc += f"result = {func_name}()\n"

        doc += "print(result)  # noqa: T201\n"
        doc += "```\n"

        return doc

    def generate_client_config(
        self,
        server_configs: list[MCPServerConfig],
        working_dir: str = "/home/workspace",
    ) -> dict[str, Any]:
        """Build the per-workspace config dict the client runtime consumes.

        Builtin servers embed only env key NAMES — never values (the sandbox
        already has the resolved values in os.environ, injected at creation
        time). Untrusted servers embed their full env/header mappings, whose
        values are either non-secret literals or ``${vault:NAME}`` placeholders
        resolved in-sandbox against the vault file only. OAuth-connected
        servers are relay-bound: the config carries a grant reference and the
        runtime dials the egress relay — the vendor URL and every token stay
        host-side.
        """
        servers: dict[str, dict[str, Any]] = {}
        for server in server_configs:
            untrusted = is_user_server(server)
            if server.oauth_connection_id:
                servers[server.name] = {
                    "transport": "http",
                    "source": server.source,
                    "relay_bound": True,
                    "relay_grant_id": server.egress_grant_id,
                }
                continue
            if server.transport in ("sse", "http"):
                entry: dict[str, Any] = {
                    "transport": server.transport,
                    "url": server.url or "",
                }
                if untrusted:
                    entry["source"] = server.source
                    entry["headers"] = dict(server.headers or {})
                    entry["discovery_uses_secrets"] = discovery_should_use_secrets(
                        server
                    )
                servers[server.name] = entry
                continue

            # Stdio transport. `uv run python mcp_servers/<file>.py` paths are
            # rewritten to the sandbox's copy of the server file.
            command = server.command
            args = [str(a) for a in server.args]
            if (
                command == "uv"
                and len(args) >= 3
                and args[0] == "run"
                and args[1] == "python"
            ):
                filename = Path(args[2]).name
                args = ["run", "python", f"{working_dir}/mcp_servers/{filename}"]
                logger.debug(
                    "Transformed MCP server command for sandbox",
                    server=server.name,
                    original_args=server.args,
                    sandbox_args=args,
                )
            entry = {"transport": "stdio", "command": command, "args": args}
            if untrusted:
                entry["source"] = server.source
                entry["env"] = dict(server.env or {})
                entry["discovery_uses_secrets"] = discovery_should_use_secrets(server)
            else:
                entry["env_keys"] = list(server.env.keys()) if server.env else []
            servers[server.name] = entry

        return {
            "working_dir": working_dir,
            "servers": servers,
            "result_body_max_bytes": RESULT_BODY_MAX_BYTES,
            "result_body_trace_budget_bytes": RESULT_BODY_TRACE_BUDGET_BYTES,
        }

    def generate_mcp_client_code(
        self,
        server_configs: list[MCPServerConfig],
        working_dir: str = "/home/workspace",
    ) -> str:
        """Compose the uploadable mcp_client.py: static runtime + config epilogue.

        The double json.dumps makes the embedded config injection-safe by
        construction: the inner call serializes the config, the outer one turns
        that JSON text into a valid Python string literal with all quoting and
        control characters escaped — hostile server names can never terminate
        the literal.
        """
        config = self.generate_client_config(server_configs, working_dir=working_dir)
        config_json = json.dumps(config, sort_keys=True)
        return (
            client_runtime_source()
            + "\n\n# --- Per-workspace configuration (generated epilogue). ---\n"
            + f"_apply_config_dict(json.loads({json.dumps(config_json)}))\n"
        )
