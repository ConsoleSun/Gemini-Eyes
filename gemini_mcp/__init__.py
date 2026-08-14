"""gemini-web-mcp: MCP server for talking to gemini.google.com via its web interface.

No official API key is needed. The server "decompiles" (extracts and decrypts)
cookies from a local Chrome/Edge profile and replays them against Gemini's
internal RPC endpoints, exactly like the browser does.
"""

__version__ = "0.1.0"
