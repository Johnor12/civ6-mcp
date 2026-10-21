-- ===========================================================================
-- MCP Bridge core (shared by the FrontEnd and InGame contexts)
--
-- Every POLL_SECONDS the bridge re-includes "mcp_bridge_cmd" (a file the
-- civ6-mcp server rewrites; include() reads it from disk every time) and,
-- when it carries a new sequence number addressed to this context, runs its
-- code with print() captured. Every output line goes to Lua.log as
--     MCPB|<seq>|O|<text>      one line of print() output
--     MCPB|<seq>|P|<text>      a partial line (Lua.log cuts lines at ~2 KB;
--                              the server joins P fragments with the final O)
--     MCPB|<seq>|E|<error>     the chunk failed to compile or raised
--     MCPB|<seq>|X             end of output
-- The server tails Lua.log for these lines.
-- ===========================================================================

MCPB_VERSION = 2
local POLL_SECONDS = 0.2
local MAX_LINE = 1800 -- Lua.log truncates longer lines

local m_context = "?"
local m_lastSeq = -1
local m_acc = 0

local function emit(seq, kind, text)
	print("MCPB|" .. tostring(seq) .. "|" .. kind .. "|" .. tostring(text))
end

local function runCommand(cmd)
	local seq = cmd.seq
	local chunk, cerr = loadstring(cmd.code, "mcp_cmd_" .. tostring(seq))
	if chunk == nil then
		emit(seq, "E", "compile: " .. string.sub(tostring(cerr), 1, MAX_LINE))
		print("MCPB|" .. tostring(seq) .. "|X")
		return
	end
	local realPrint = print
	print = function(...)
		local parts = {}
		for i = 1, select("#", ...) do parts[#parts + 1] = tostring(select(i, ...)) end
		local text = table.concat(parts, "\t")
		-- one Lua.log line per print line, long lines split into P fragments
		for line in string.gmatch(text .. "\n", "([^\n]*)\n") do
			while #line > MAX_LINE do
				realPrint("MCPB|" .. tostring(seq) .. "|P|" .. string.sub(line, 1, MAX_LINE))
				line = string.sub(line, MAX_LINE + 1)
			end
			realPrint("MCPB|" .. tostring(seq) .. "|O|" .. line)
		end
	end
	local ok, err = pcall(chunk)
	print = realPrint
	if not ok then
		emit(seq, "E", "runtime: " .. string.sub(tostring(err), 1, MAX_LINE))
	end
	realPrint("MCPB|" .. tostring(seq) .. "|X")
end

local function readCommand()
	MCP_CMD = nil
	local ok = pcall(include, "mcp_bridge_cmd")
	local cmd = MCP_CMD
	MCP_CMD = nil
	if not ok or type(cmd) ~= "table" then
		return nil
	end
	if type(cmd.seq) ~= "number" or type(cmd.code) ~= "string" then
		return nil
	end
	return cmd
end

local function poll()
	local cmd = readCommand()
	if cmd == nil then
		return
	end
	if cmd.context ~= nil and cmd.context ~= m_context then
		return
	end
	if cmd.seq <= m_lastSeq then
		return
	end
	m_lastSeq = cmd.seq
	runCommand(cmd)
end

local function onUpdate(dt)
	m_acc = m_acc + dt
	if m_acc < POLL_SECONDS then
		return
	end
	m_acc = 0
	poll()
end

function MCPBridge_Start(contextName)
	m_context = contextName
	-- Ignore whatever command is already in the file when this context
	-- starts (for example the command that loaded this very game).
	local stale = readCommand()
	if stale ~= nil then
		m_lastSeq = stale.seq
	end
	print("MCPB|0|S|MCP Bridge v" .. MCPB_VERSION .. " started in " .. contextName)
	-- Mod contexts start hidden and hidden contexts get no update ticks.
	-- This context draws nothing, so showing it is invisible to the player.
	ContextPtr:SetHide(false)
	ContextPtr:SetUpdate(onUpdate)
end
