-- MCP Bridge: front-end context (main menu, staging room).
-- This file shadows Base/Assets/UI/Test.lua, a placeholder the game loads
-- into the front end on Windows. The original only prints a line.
print("Test.LUA loaded!")
include("MCPBridgeCore")
MCPBridge_Start("FrontEnd")
