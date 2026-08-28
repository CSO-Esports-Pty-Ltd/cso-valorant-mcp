import asyncio
import os
import subprocess
import sys
import unittest

from valorant_mcp_server.server import REDUNDANT_TOOL_NAMES, mcp


class ToolVisibilityTests(unittest.TestCase):
    def test_default_registry_excludes_redundant_tools(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}

        self.assertEqual(len(names), 54)
        self.assertTrue(REDUNDANT_TOOL_NAMES.isdisjoint(names))

    def test_legacy_flag_restores_compatibility_tools(self) -> None:
        env = os.environ.copy()
        env["MCP_EXPOSE_LEGACY_TOOLS"] = "true"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import asyncio; "
                    "from valorant_mcp_server.server import REDUNDANT_TOOL_NAMES, mcp; "
                    "names={tool.name for tool in asyncio.run(mcp.list_tools())}; "
                    "assert len(names) == 76; "
                    "assert REDUNDANT_TOOL_NAMES <= names"
                ),
            ],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
