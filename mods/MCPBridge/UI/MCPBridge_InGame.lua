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

-- Snapshots: a save of the hotseat game whenever the pause menu opens, kept
-- in Documents/My Games/Sid Meier's Civilization VI/Snapshots, beside Saves,
-- where the Load Game list does not look, so the state a player left can be
-- seen after they reload. Every way out of a game passes through the pause
-- menu (Load Game, Exit to Main Menu, Exit to Desktop, and the window's close
-- button, which opens it to ask), and the menu stays up long enough for the
-- save, which is not immediate, to finish. No sound, no message. One file per
-- player and turn per Lua state, overwritten each time the menu opens: a load
-- starts a new Lua state, so a replay never overwrites the file from before
-- it. The turn logger names the snapshot in each reload log row and prunes
-- old ones. One line in Lua.log per snapshot:
--     MCPB|0|N|<unix time>|<turn>|<slot>|<file name>
if MCPB_SNAPSHOTS == nil then
	MCPB_SNAPSHOTS = 1
	local session = os.date("%Y-%m-%d %H.%M.%S")  -- when this Lua state began
	local pending = nil  -- os.time() of a snapshot whose save has not completed
	local function snapshot()
		if not GameConfiguration.IsHotseat() then
			return
		end
		if pending ~= nil and os.time() - pending < 30 then
			return
		end
		local me = Game.GetLocalPlayer()
		local pc = PlayerConfigurations[me]
		if pc == nil then
			return
		end
		local saveType = Network.GetGameConfigurationSaveType()
		local saves = tostring(UI.GetSaveLocationPath(SaveLocations.LOCAL_STORAGE, saveType, SaveLocationOptions.NO_OPTIONS, false))
		local root, found = string.gsub(saves, "/Saves/[^/]*/?$", "")
		if found ~= 1 then  -- never into a folder the Load Game list shows
			print("MCPB|0|N|error|unexpected save folder " .. saves)
			return
		end
		local turn = Game.GetCurrentGameTurn()
		local player = string.gsub(tostring(pc:GetPlayerName()), "[^%w%- ]", "_")
		local name = string.format("%s turn %d %s", player, turn, session)
		pending = os.time()
		Network.SaveGame({
			Name = name,
			Path = root .. "/Snapshots/" .. name,
			Location = SaveLocations.LOCAL_STORAGE,
			Type = saveType,
			IsAutosave = false,
			IsQuicksave = false,
		})
		print("MCPB|0|N|" .. table.concat({ tostring(pending), tostring(turn), tostring(me), name }, "|"))
	end
	Events.SaveComplete.Add(function()
		pending = nil
	end)
	LuaEvents.InGameTopOptionsMenu_Show.Add(function()
		local ok, err = pcall(snapshot)
		if not ok then
			print("MCPB|0|N|error|" .. tostring(err))
		end
	end)
end
