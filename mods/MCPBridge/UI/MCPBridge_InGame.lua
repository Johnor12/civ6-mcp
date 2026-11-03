-- MCP Bridge: in-game context (single player, hotseat, any session type).
include("MCPBridgeCore")
MCPBridge_Start("InGame")

-- Turn lines: one line in Lua.log whenever a hotseat turn starts, so the
-- tooling can tell whose turn it is by reading the log instead of asking
-- the game (civ_mcp.turn_watch reads them):
--     MCPB|0|T|<unix time>|<turn>|<slot>|<leader type>|<player name>|begin
-- "load" in place of "begin" is a save loaded into a turn already under way,
-- which fires no LocalPlayerTurnBegin; the game's PlayerChange.lua handles
-- that case the same way. The block runs once per Lua state: the tooling
-- also sends it through the bridge into a game that loaded an older copy.
if MCPB_TURN_LINES == nil then
	MCPB_TURN_LINES = 1
	local function turnLine(why)
		if not GameConfiguration.IsHotseat() then
			return
		end
		local me = Game.GetLocalPlayer()
		local pc = PlayerConfigurations[me]
		if pc == nil then
			return
		end
		local name = string.gsub(tostring(pc:GetPlayerName()), "|", "/")
		print("MCPB|0|T|" .. table.concat({
			tostring(os.time()), tostring(Game.GetCurrentGameTurn()), tostring(me),
			tostring(pc:GetLeaderTypeName()), name, why,
		}, "|"))
	end
	Events.LocalPlayerTurnBegin.Add(function()
		turnLine("begin")
	end)
	Events.LoadScreenClose.Add(function()
		local player = Players[Game.GetLocalPlayer()]
		if player ~= nil and player:IsTurnActive() then
			turnLine("load")
		end
	end)
end
