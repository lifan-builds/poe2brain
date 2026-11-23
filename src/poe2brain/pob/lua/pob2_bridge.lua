-- pob2_bridge.lua
-- MIT License
-- Persistent headless PathOfBuilding-PoE2 JSON-RPC 2.0 bridge over stdin/stdout.
--
-- Capabilities:
--   1. Named build slots ("main", "ref", "candidate", ...) + switch/clone/reset/delete/undo/redo
--   2. Import/export of PoB2 share codes (eN... zlib/gzip wbits=15/47 via FFI libz), raw XML,
--      and official GGG PoE2 BuildPlanner (.build JSON via Modules.BuildExportPoE2)
--   3. Full PoE2 stats extraction (FullDPS, TotalDPS, TotalEHP, Life/ES/Mana, Spirit reserved/total,
--      Resistances, Armour/Evasion/Deflection/Block, MaxHits, active skills/supports, passives, gear)
--   4. Dewatering / PoB-warrior audit (snapshot config + flasks/charms, attribute each unsustainable
--      buff/charge/shock/customMod/non-Pinnacle boss setting, compute sustainable Pinnacle stats,
--      restore original state losslessly)
--   5. Single & batch non-destructive candidate item upgrade diffs via CalcsTab:GetMiscCalculator()
--   6. Passive node add/remove diff & top reachable unallocated notables ranking
--   7. TradeQueryGenerator marginal stat weights & ready-to-POST trade2 weighted search JSON per slot

local stdout = io.stdout
local stderr = io.stderr

local function logStderr(...)
	local n = select("#", ...)
	local parts = {}
	for i = 1, n do
		parts[i] = tostring((select(i, ...)))
	end
	stderr:write(table.concat(parts, "\t"), "\n")
	stderr:flush()
end

-- Reserve stdout strictly for newline-delimited JSON-RPC frames.
_G.print = logStderr

local ffi = require("ffi")

ffi.cdef[[
int chdir(const char *path);

typedef struct pob2_z_stream_s {
	const uint8_t *next_in;
	unsigned int   avail_in;
	unsigned long  total_in;
	uint8_t       *next_out;
	unsigned int   avail_out;
	unsigned long  total_out;
	const char    *msg;
	void          *state;
	void          *zalloc;
	void          *zfree;
	void          *opaque;
	int            data_type;
	unsigned long  adler;
	unsigned long  reserved;
} pob2_z_stream;

int deflateInit2_(pob2_z_stream *strm, int level, int method, int windowBits, int memLevel, int strategy, const char *version, int stream_size);
int deflate(pob2_z_stream *strm, int flush);
int deflateEnd(pob2_z_stream *strm);
int inflateInit2_(pob2_z_stream *strm, int windowBits, const char *version, int stream_size);
int inflate(pob2_z_stream *strm, int flush);
int inflateEnd(pob2_z_stream *strm);
const char *zlibVersion(void);
]]

-- Allow passing PoB2 src directory via POB2_SRC_DIR or arg[1] if CWD is elsewhere.
local pobSrcDir = os.getenv("POB2_SRC_DIR") or (arg and arg[1])
if pobSrcDir and pobSrcDir ~= "" then
	local rc = ffi.C.chdir(pobSrcDir)
	if rc ~= 0 then
		stderr:write("[pob2_bridge] Failed to chdir to POB2_SRC_DIR: " .. tostring(pobSrcDir) .. "\n")
		os.exit(1)
	end
end

package.path = "../runtime/lua/?.lua;../runtime/lua/?/init.lua;" .. package.path

-- Ensure utf8 global is populated (luautf8 C module).
if type(_G.utf8) ~= "table" then
	local okU, utf8Mod = pcall(require, "lua-utf8")
	if not okU then
		okU, utf8Mod = pcall(require, "utf8")
	end
	if okU and type(utf8Mod) == "table" then
		_G.utf8 = utf8Mod
	else
		stderr:write("[pob2_bridge] Missing lua-utf8 C module in LUA_CPATH\n")
		os.exit(1)
	end
end

local dkjson = require("dkjson")

local function jsonObj(tbl)
	tbl = tbl or {}
	if next(tbl) == nil then
		return setmetatable(tbl, { __jsontype = "object" })
	end
	return tbl
end

local function writeFrame(payload)
	local encoded, err = dkjson.encode(payload)
	if not encoded then
		encoded = dkjson.encode({
			jsonrpc = "2.0",
			id = payload and payload.id or dkjson.null,
			error = { code = -32603, message = "JSON encode error: " .. tostring(err) },
		})
	end
	stdout:write(encoded, "\n")
	stdout:flush()
end

-- Load zlib via FFI for PoB2 share codes and Timeless Jewel LUTs.
local function loadZlib()
	local candidates = {}
	local explicit = os.getenv("POB2_ZLIB_PATH")
	if explicit and explicit ~= "" then
		candidates[#candidates + 1] = explicit
	end
	candidates[#candidates + 1] = "z"
	candidates[#candidates + 1] = "libz.dylib"
	candidates[#candidates + 1] = "libz.so.1"
	candidates[#candidates + 1] = "../runtime/zlib1.dll"
	candidates[#candidates + 1] = "zlib1"
	for _, name in ipairs(candidates) do
		local ok, lib = pcall(ffi.load, name)
		if ok and lib then
			return lib
		end
	end
	return nil
end

local zlib = loadZlib()
local zlibVer = zlib and ffi.string(zlib.zlibVersion()) or "1.2.11"

local function realDeflate(dataStr, isGzip)
	if not zlib then
		error("zlib FFI unavailable", 0)
	end
	local n = #dataStr
	local strm = ffi.new("pob2_z_stream")
	-- _SimpleGraphic.def.lua contract: ZLib header (wbits=15) by default, Gzip (wbits=31) when isGzip=true.
	local wbits = isGzip and (15 + 16) or 15
	local ret = zlib.deflateInit2_(strm, 9, 8, wbits, 8, 0, zlibVer, ffi.sizeof(strm))
	if ret ~= 0 then
		error("deflateInit2_ failed with code " .. tostring(ret), 0)
	end
	local inBuf = ffi.new("uint8_t[?]", n + 1)
	if n > 0 then
		ffi.copy(inBuf, dataStr, n)
	end
	strm.next_in = inBuf
	strm.avail_in = n
	local bufSize = 65536
	local outBuf = ffi.new("uint8_t[?]", bufSize)
	local chunks = {}
	repeat
		strm.next_out = outBuf
		strm.avail_out = bufSize
		ret = zlib.deflate(strm, 4) -- Z_FINISH = 4
		if ret < 0 then
			zlib.deflateEnd(strm)
			error("deflate failed with code " .. tostring(ret), 0)
		end
		local have = bufSize - strm.avail_out
		if have > 0 then
			chunks[#chunks + 1] = ffi.string(outBuf, have)
		end
	until ret == 1 -- Z_STREAM_END = 1
	zlib.deflateEnd(strm)
	-- Pin FFI buffers across the entire zlib C stream lifecycle so LuaJIT GC cannot collect them early!
	if inBuf == nil or outBuf == nil then error("unreachable") end
	return table.concat(chunks)
end

local function realInflate(dataStr)
	if not zlib then
		error("zlib FFI unavailable", 0)
	end
	local n = #dataStr
	local strm = ffi.new("pob2_z_stream")
	-- windowBits = 15 + 32 (47) auto-detects ZLib (0x78..) and Gzip (0x1f 0x8b) headers.
	local ret = zlib.inflateInit2_(strm, 15 + 32, zlibVer, ffi.sizeof(strm))
	if ret ~= 0 then
		ret = zlib.inflateInit2_(strm, -15, zlibVer, ffi.sizeof(strm))
		if ret ~= 0 then
			error("inflateInit2_ failed with code " .. tostring(ret), 0)
		end
	end
	local inBuf = ffi.new("uint8_t[?]", n + 1)
	if n > 0 then
		ffi.copy(inBuf, dataStr, n)
	end
	strm.next_in = inBuf
	strm.avail_in = n
	local bufSize = 65536
	local outBuf = ffi.new("uint8_t[?]", bufSize)
	local chunks = {}
	local iterations = 0
	repeat
		strm.next_out = outBuf
		strm.avail_out = bufSize
		ret = zlib.inflate(strm, 0) -- Z_NO_FLUSH = 0
		if ret ~= 0 and ret ~= 1 then
			zlib.inflateEnd(strm)
			error("inflate failed with code " .. tostring(ret), 0)
		end
		local have = bufSize - strm.avail_out
		if have > 0 then
			chunks[#chunks + 1] = ffi.string(outBuf, have)
		end
		iterations = iterations + 1
		if iterations > 100000 then
			zlib.inflateEnd(strm)
			error("inflate exceeded iteration safety cap", 0)
		end
	until ret == 1 or (strm.avail_in == 0 and (bufSize - strm.avail_out) == 0)
	zlib.inflateEnd(strm)
	-- Pin FFI buffers across the entire zlib C stream lifecycle so LuaJIT GC cannot collect them early!
	if inBuf == nil or outBuf == nil then error("unreachable") end
	return table.concat(chunks)
end

-- Boot PathOfBuilding-PoE2 headless environment.
-- Note: HeadlessWrapper.lua executes _SimpleGraphic.def.lua, which defines no-op Deflate/Inflate,
-- so we pre-load _SimpleGraphic.def.lua, override Deflate/Inflate BEFORE Launch.lua runs OnInit,
-- and leave continuousIntegrationMode false so ModCache.lua loads normally!
local bootT0 = os.clock()
local bootOk, bootErr = pcall(function()
	dofile("_SimpleGraphic.def.lua")
	function GetVirtualScreenSize()
		return 1920, 1080
	end
	_G.Deflate = function(d, isGzip)
		local ok, res = pcall(realDeflate, d, isGzip)
		if ok then return res end
		logStderr("[pob2_bridge] Deflate error:", res)
		return nil, tostring(res)
	end
	_G.Inflate = function(d)
		local ok, res = pcall(realInflate, d)
		if ok then return res end
		logStderr("[pob2_bridge] Inflate error:", res)
		return nil, tostring(res)
	end

	__callbackTable__ = {}
	function runCallback(name, ...)
		if __callbackTable__[name] then
			return __callbackTable__[name](...)
		elseif __mainObject__ and __mainObject__[name] then
			return __mainObject__[name](__mainObject__, ...)
		end
	end

	local origRequire = require
	function require(name)
		if name == "lcurl.safe" then
			return nil
		end
		return origRequire(name)
	end

	dofile("Launch.lua")
	__mainObject__.continuousIntegrationMode = false
	runCallback("OnInit")
	runCallback("OnFrame")
	if __mainObject__.promptMsg then
		error("PoB2 startup promptMsg: " .. tostring(__mainObject__.promptMsg), 0)
	end
end)

if not bootOk then
	stderr:write("[pob2_bridge] Boot failed: " .. tostring(bootErr) .. "\n")
	os.exit(1)
end

local bootSeconds = os.clock() - bootT0
local mainObj = __mainObject__.main
local build = mainObj.modes["BUILD"]
local base64 = require("base64")
local BuildExportPoE2 = require("Modules.BuildExportPoE2")
local tradeHelpers = LoadModule("Classes/TradeHelpers")
local configOptionsList = LoadModule("Modules/ConfigOptions")

-- Build lookup of ConfigOptions metadata by var name.
local configMetaByVar = {}
do
	local curSection = "General"
	for _, opt in ipairs(configOptionsList) do
		if opt.section then
			curSection = opt.section
		elseif opt.var then
			local cleanLabel = tostring(opt.label or opt.var):gsub("%^x%x%x%x%x%x%x", ""):gsub("%^%d", "")
			configMetaByVar[opt.var] = {
				var = opt.var,
				section = curSection,
				type = opt.type,
				label = cleanLabel,
				defaultState = opt.defaultState,
				defaultIndex = opt.defaultIndex,
				list = opt.list,
			}
		end
	end
end

-- Build lookup of TradeSiteStats id -> text for human-readable stat weight output.
local tradeStatTextById
local function getTradeStatTextMap()
	if tradeStatTextById then
		return tradeStatTextById
	end
	tradeStatTextById = {}
	local stats = tradeHelpers.getTradeStats() or {}
	for _, cat in ipairs(stats) do
		for _, entry in ipairs(cat.entries or {}) do
			if entry.id and entry.text then
				tradeStatTextById[entry.id] = entry.text
			end
		end
	end
	return tradeStatTextById
end

-- Default blank PoE2 base item per slot so empty slots still support TradeQueryGenerator.
local DEFAULT_SLOT_BASES = {
	["Weapon 1"] = "Attuned Wand",
	["Weapon 2"] = "Hallowed Focus",
	["Weapon 1 Swap"] = "Attuned Wand",
	["Weapon 2 Swap"] = "Hallowed Focus",
	["Helmet"] = "Twig Circlet",
	["Body Armour"] = "Hexer's Robe",
	["Gloves"] = "Torn Gloves",
	["Boots"] = "Straw Sandals",
	["Amulet"] = "Amber Amulet",
	["Ring 1"] = "Gold Ring",
	["Ring 2"] = "Gold Ring",
	["Ring 3"] = "Gold Ring",
	["Belt"] = "Rawhide Belt",
	["Flask 1"] = "Lesser Life Flask",
	["Flask 2"] = "Lesser Mana Flask",
	["Charm 1"] = "Thawing Charm",
	["Charm 2"] = "Staunching Charm",
	["Charm 3"] = "Grounding Charm",
}

-- ============================================================================
-- Helper functions for refreshing & loading builds
-- ============================================================================

local function closeAllPopups()
	while mainObj.popups and mainObj.popups[1] do
		mainObj:ClosePopup()
	end
end

local function refreshBuild()
	build = mainObj.modes["BUILD"]
	build.modFlag = true
	build.buildFlag = true
	runCallback("OnFrame")
	closeAllPopups()
end

local function resetEngineToNewBuild(buildName)
	if GlobalCache and GlobalCache.cachedData then
		wipeGlobalCache()
	end
	mainObj:SetMode("BUILD", false, buildName or "Unnamed Build")
	runCallback("OnFrame")
	closeAllPopups()
	build = mainObj.modes["BUILD"]
end

local function loadEngineFromXML(xmlText, buildName)
	if GlobalCache and GlobalCache.cachedData then
		wipeGlobalCache()
	end
	mainObj:SetMode("BUILD", false, buildName or "Imported Build", xmlText)
	runCallback("OnFrame")
	closeAllPopups()
	build = mainObj.modes["BUILD"]
end

local function saveEngineToXML()
	build = mainObj.modes["BUILD"]
	return build:SaveDB("slot-snapshot")
end

-- ============================================================================
-- Capability 1: Named Build Slots + Clone / Reset / Undo / Redo
-- ============================================================================

local MAX_HISTORY = 20
local slots = {}
local activeSlot = "main"

resetEngineToNewBuild("main")
slots["main"] = {
	name = "main",
	xml = saveEngineToXML(),
	rev = 1,
	undoStack = {},
	redoStack = {},
}

local function normalizeSlotName(params, key)
	local s = params and params[key or "slot"]
	if s == nil or s == "" then
		return activeSlot
	end
	s = tostring(s)
	if not s:match("^[%w_%-%.]+$") then
		error("Invalid slot name '" .. s .. "' (use alphanumeric, _, -, .)", 0)
	end
	return s
end

local function ensureSlot(slotName, createIfMissing)
	if slotName == activeSlot and slots[slotName] then
		return slots[slotName]
	end
	-- Save outgoing active slot first
	if activeSlot and slots[activeSlot] then
		slots[activeSlot].xml = saveEngineToXML()
	end
	if not slots[slotName] then
		if createIfMissing == false then
			error("Unknown slot '" .. tostring(slotName) .. "'", 0)
		end
		resetEngineToNewBuild(slotName)
		slots[slotName] = {
			name = slotName,
			xml = saveEngineToXML(),
			rev = 1,
			undoStack = {},
			redoStack = {},
		}
		activeSlot = slotName
		return slots[slotName]
	end
	loadEngineFromXML(slots[slotName].xml, slotName)
	activeSlot = slotName
	return slots[slotName]
end

local function pushUndoSnapshot(slotEntry)
	local snap = saveEngineToXML()
	table.insert(slotEntry.undoStack, snap)
	if #slotEntry.undoStack > MAX_HISTORY then
		table.remove(slotEntry.undoStack, 1)
	end
	slotEntry.redoStack = {}
end

local function finalizeSlotMutation(slotEntry)
	refreshBuild()
	slotEntry.rev = (slotEntry.rev or 1) + 1
	slotEntry.xml = saveEngineToXML()
end

-- ============================================================================
-- Capability 3: Full PoE2 Stats & Build Summary Extraction
-- ============================================================================

local function safeNum(v, default)
	if type(v) ~= "number" then
		return default or 0
	end
	if v ~= v or v == math.huge or v == -math.huge then
		return default or 0
	end
	return v
end

local function minElementalMaxHit(out)
	local f = safeNum(out.FireMaximumHitTaken, 0)
	local c = safeNum(out.ColdMaximumHitTaken, 0)
	local l = safeNum(out.LightningMaximumHitTaken, 0)
	return math.min(f, c, l)
end

local CORE_STAT_KEYS = {
	-- Offence
	"FullDPS", "FullDotDPS", "TotalDPS", "CombinedDPS", "TotalDot", "TotalDotDPS",
	"WithBleedDPS", "WithIgniteDPS", "WithPoisonDPS", "AverageDamage", "AverageBurstDamage",
	"Speed", "HitChance", "AccuracyHitChance", "CritChance", "CritMultiplier",
	"ManaCost", "LifeCost", "ESCost", "RageCost",
	-- Pools & Sustain
	"TotalEHP", "Life", "LifeUnreserved", "LifeUnreservedPercent", "LifeReserved", "LifeReservedPercent",
	"LifeRegenRecovery", "LifeLeechGainRate",
	"EnergyShield", "EnergyShieldRecharge", "EnergyShieldRechargeDelay", "EnergyShieldRegenRecovery",
	"Mana", "ManaUnreserved", "ManaUnreservedPercent", "ManaReserved", "ManaReservedPercent",
	"ManaRegenRecovery", "Ward",
	-- Spirit
	"Spirit", "SpiritReserved", "SpiritUnreserved", "SpiritReservedPercent", "SpiritUnreservedPercent",
	-- Resistances
	"FireResist", "FireResistOverCap", "ColdResist", "ColdResistOverCap",
	"LightningResist", "LightningResistOverCap", "ChaosResist", "ChaosResistOverCap",
	-- Mitigation & Avoidance (PoE2 Armour / Evasion / Deflection / Block)
	"Armour", "PhysicalDamageReduction",
	"Evasion", "EvadeChance", "MeleeEvadeChance", "ProjectileEvadeChance", "SpellEvadeChance",
	"DeflectionRating", "DeflectChance", "DeflectEffect",
	"BlockChance", "BlockChanceMax", "EffectiveBlockChance", "SpellBlockChance", "ProjectileBlockChance",
	-- Max Hits & Thresholds
	"PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken",
	"LightningMaximumHitTaken", "ChaosMaximumHitTaken", "SecondMinimalMaximumHitTaken",
	"StunThreshold", "AilmentThreshold",
	-- Attributes & Mobility
	"Str", "Dex", "Int", "TotalAttr", "ReqStrString", "ReqDexString", "ReqIntString",
	"MovementSpeedMod", "EffectiveMovementSpeedMod",
}

local function extractStatsTable(out, extraFields)
	out = out or (build and build.calcsTab and build.calcsTab.mainOutput) or {}
	local res = {}
	for _, k in ipairs(CORE_STAT_KEYS) do
		local v = out[k]
		if type(v) == "number" then
			res[k] = safeNum(v, 0)
		elseif type(v) == "boolean" or type(v) == "string" then
			res[k] = v
		else
			res[k] = 0
		end
	end
	res.ElementalMaximumHitTaken = minElementalMaxHit(out)
	-- Effective primary DPS convenience field (FullDPS if > 0 else CombinedDPS/TotalDPS)
	local fullDps = safeNum(out.FullDPS, 0)
	local combinedDps = safeNum(out.CombinedDPS, 0)
	local totalDps = safeNum(out.TotalDPS, 0)
	res.EffectiveDPS = (fullDps > 0 and fullDps) or (combinedDps > 0 and combinedDps) or totalDps

	if type(out.Minion) == "table" then
		res.Minion = {
			TotalDPS = safeNum(out.Minion.TotalDPS, 0),
			FullDPS = safeNum(out.Minion.FullDPS, 0),
			CombinedDPS = safeNum(out.Minion.CombinedDPS, 0),
			Life = safeNum(out.Minion.Life, 0),
			EnergyShield = safeNum(out.Minion.EnergyShield, 0),
			TotalEHP = safeNum(out.Minion.TotalEHP, 0),
		}
	end

	if type(extraFields) == "table" then
		for _, k in ipairs(extraFields) do
			local v = out[k]
			if type(v) == "number" then
				res[k] = safeNum(v, 0)
			elseif type(v) == "boolean" or type(v) == "string" then
				res[k] = v
			end
		end
	end
	return res
end

local function extractCharacterInfo()
	local spec = build.spec
	local usedPoints, ascUsedPoints = 0, 0
	if spec and spec.CountAllocNodes then
		usedPoints, ascUsedPoints = spec:CountAllocNodes()
	end
	return {
		name = build.buildName or "Unnamed Build",
		level = build.characterLevel or 1,
		classId = spec and spec.curClassId or 0,
		className = spec and spec.curClassName or "Scion",
		ascendClassId = spec and spec.curAscendClassId or 0,
		ascendancyName = spec and spec.curAscendClassName or "None",
		treeVersion = spec and spec.treeVersion or latestTreeVersion,
		passivePointsUsed = usedPoints or 0,
		ascendancyPointsUsed = ascUsedPoints or 0,
	}
end

local function extractSkillsInfo()
	local groups = {}
	for idx, group in ipairs(build.skillsTab.socketGroupList or {}) do
		local gems = {}
		for gIdx, gem in ipairs(group.gemList or {}) do
			local gemData = gem.gemData
			local granted = gemData and gemData.grantedEffect or gem.grantedEffect
			gems[#gems + 1] = {
				index = gIdx,
				name = (gemData and gemData.name) or (gem.nameSpec ~= "" and gem.nameSpec) or gem.skillId or "Unknown",
				nameSpec = gem.nameSpec or "",
				gemId = gem.gemId or (gemData and gemData.id) or dkjson.null,
				gameId = (gemData and gemData.gameId) or dkjson.null,
				skillId = gem.skillId or (granted and granted.id) or dkjson.null,
				isSupport = (granted and granted.support) == true,
				level = tonumber(gem.level) or 1,
				quality = tonumber(gem.quality) or 0,
				enabled = gem.enabled ~= false,
			}
		end
		local activeName = dkjson.null
		if group.displaySkillList and group.displaySkillList[group.mainActiveSkill or 1] then
			local ae = group.displaySkillList[group.mainActiveSkill or 1].activeEffect
			if ae and ae.grantedEffect then
				activeName = ae.grantedEffect.name
			end
		end
		groups[#groups + 1] = {
			index = idx,
			label = group.label or "",
			slot = group.slot or dkjson.null,
			enabled = group.enabled ~= false,
			includeInFullDPS = group.includeInFullDPS == true,
			isMainSkill = (build.mainSocketGroup == idx),
			mainActiveSkillIndex = group.mainActiveSkill or 1,
			mainActiveSkillName = activeName,
			gems = gems,
		}
	end
	return {
		mainSocketGroup = build.mainSocketGroup or 1,
		socketGroups = groups,
	}
end

local function extractTreeInfo()
	local spec = build.spec
	local allocIds = {}
	local notables = {}
	local keystones = {}
	local ascendancyNodes = {}
	if spec and spec.allocNodes then
		for nodeId, node in pairs(spec.allocNodes) do
			if type(nodeId) == "number" then
				allocIds[#allocIds + 1] = nodeId
				local entry = {
					id = nodeId,
					stringId = node.stringId or dkjson.null,
					name = node.dn or "",
					type = node.type or "Normal",
					stats = node.sd or {},
					weaponSet = node.allocMode or 0,
				}
				if node.ascendancyName then
					entry.ascendancyName = node.ascendancyName
					ascendancyNodes[#ascendancyNodes + 1] = entry
				elseif node.type == "Keystone" then
					keystones[#keystones + 1] = entry
				elseif node.type == "Notable" then
					notables[#notables + 1] = entry
				end
			end
		end
	end
	table.sort(allocIds)
	table.sort(notables, function(a, b) return a.id < b.id end)
	table.sort(keystones, function(a, b) return a.id < b.id end)
	table.sort(ascendancyNodes, function(a, b) return a.id < b.id end)
	return {
		treeVersion = spec and spec.treeVersion or latestTreeVersion,
		allocatedCount = #allocIds,
		allocatedNodeIds = allocIds,
		notables = notables,
		keystones = keystones,
		ascendancyNodes = ascendancyNodes,
	}
end

local function extractItemsInfo(includeRaw)
	local equipped = {}
	for _, slotCtrl in ipairs(build.itemsTab.orderedSlots or {}) do
		local slotName = slotCtrl.slotName
		local itemId = slotCtrl.selItemId or 0
		if itemId ~= 0 and build.itemsTab.items[itemId] then
			local item = build.itemsTab.items[itemId]
			local entry = {
				slot = slotName,
				itemId = itemId,
				name = item.name or "",
				title = item.title or "",
				baseName = item.baseName or "",
				type = item.type or "",
				rarity = item.rarity or "NORMAL",
				active = slotCtrl.active == true,
				runeSockets = item.itemSocketCount or 0,
			}
			if includeRaw then
				entry.raw = item:BuildRaw()
			end
			equipped[#equipped + 1] = entry
		end
	end
	return equipped
end

local function getActiveCustomModsText()
	local ct = build and build.configTab
	local activeSet = ct and ct.configSets and ct.configSets[ct.activeConfigSetId]
	local parts = {}
	if activeSet and type(activeSet.customModsList) == "table" then
		for _, block in ipairs(activeSet.customModsList) do
			if block.enabled ~= false and type(block.text) == "string" then
				local trimmed = block.text:match("^%s*(.-)%s*$")
				if trimmed ~= "" then
					parts[#parts + 1] = trimmed
				end
			end
		end
	end
	if #parts > 0 then
		return table.concat(parts, "\n")
	end
	local legacy = ct and ct.input and ct.input.customMods
	if type(legacy) == "string" then
		return legacy:match("^%s*(.-)%s*$")
	end
	return ""
end

local function setActiveCustomModsText(txt)
	local clean = tostring(txt or "")
	local ct = build and build.configTab
	if not ct then return end
	local activeSet = ct.configSets and ct.configSets[ct.activeConfigSetId]
	if activeSet then
		activeSet.customModsList = {
			{ title = "Default", enabled = true, text = clean },
		}
	end
	if ct.input then
		ct.input.customMods = nil
	end
end

local function extractConfigInfo()
	local input = {}
	for k, v in pairs(build.configTab.input or {}) do
		if k ~= "customMods" and v ~= nil then
			input[k] = v
		end
	end
	return {
		input = jsonObj(input),
		customMods = getActiveCustomModsText(),
	}
end

-- ============================================================================
-- Capability 2: Share Code, XML, and GGG .build JSON Import / Export
-- ============================================================================

local function padBase64(b64)
	local rem = #b64 % 4
	if rem > 0 then
		b64 = b64 .. string.rep("=", 4 - rem)
	end
	return b64
end

-- Build reverse map for GGG .build passive stringId -> nodeId on a spec
local function buildStringIdMap(spec)
	local map = {}
	for nodeId, node in pairs(spec.nodes or {}) do
		if type(node) == "table" and node.stringId and type(nodeId) == "number" and nodeId < 65536 then
			map[node.stringId] = nodeId
			map[node.stringId:lower()] = nodeId
		end
	end
	return map
end

-- Build reverse map for GGG .build gem gameId/name -> gemData
local gemLookupCache
local function findGemDataByIdentifier(ident)
	if not ident or ident == "" then
		return nil
	end
	if not gemLookupCache then
		gemLookupCache = { byGameId = {}, byName = {}, byId = {} }
		for gemId, gemData in pairs(build.data.gems or {}) do
			gemLookupCache.byId[gemId] = gemData
			gemLookupCache.byId[gemId:lower()] = gemData
			if gemData.gameId then
				gemLookupCache.byGameId[gemData.gameId] = gemData
				gemLookupCache.byGameId[gemData.gameId:lower()] = gemData
			end
			if gemData.name then
				gemLookupCache.byName[gemData.name:lower()] = gemData
			end
		end
	end
	local low = tostring(ident):lower()
	return gemLookupCache.byGameId[ident]
		or gemLookupCache.byGameId[low]
		or gemLookupCache.byId[ident]
		or gemLookupCache.byId[low]
		or gemLookupCache.byName[low]
		or gemLookupCache.byName[low .. " iii"]
		or gemLookupCache.byName[low .. " ii"]
		or gemLookupCache.byName[low .. " i"]
end

local function parseAdditionalTextLevelQuality(text, isSupport)
	local lvl = isSupport and 1 or 20
	local qual = 0
	if type(text) == "string" and text ~= "" then
		local lMatch = text:match("[Ll]evel%s+(%d+)")
		if lMatch then lvl = tonumber(lMatch) or lvl end
		local qMatch = text:match("(%d+)%%%s+[Qq]uality")
		if qMatch then qual = tonumber(qMatch) or qual end
	end
	return lvl, qual
end

local function reconstructItemRawFromBuildNote(noteText)
	if type(noteText) ~= "string" or noteText == "" then
		return nil
	end
	if noteText:match("^%s*Rarity:%s*%a+") or noteText:match("\nRarity:%s*%a+") then
		return noteText
	end
	-- Reconstruct from BuildExportPoE2.ItemAdditionalText PoE2 markup:
	-- e.g. "<rare>{<b>{Storm Spire\nAttuned Wand}}\n<crafted>{+80 to maximum Mana}..."
	local rarity = "RARE"
	local firstTag = noteText:match("^<([^>]+)>")
	if firstTag then
		local low = firstTag:lower()
		if low:find("unique") or low:find("af6025") or low:find("175,96,37") then
			rarity = "UNIQUE"
		elseif low:find("magic") or low:find("8888ff") or low:find("136,136,255") then
			rarity = "MAGIC"
		elseif low:find("normal") or low:find("c8c8c8") then
			rarity = "NORMAL"
		end
	end
	local cleaned = noteText
	-- Repeatedly unwrap <tag>{inner} from innermost braces outward.
	local changed = true
	local guard = 0
	while changed and guard < 20 do
		local nextStr, count = cleaned:gsub("<[^>]+>{([^{}]*)}", "%1")
		changed = (count > 0)
		cleaned = nextStr
		guard = guard + 1
	end
	cleaned = cleaned:gsub("<[^>]+>", ""):gsub("[{}]", "")
	local lines = {}
	for line in cleaned:gmatch("[^\r\n]+") do
		local trimmed = line:match("^%s*(.-)%s*$")
		if trimmed ~= "" then
			lines[#lines + 1] = trimmed
		end
	end
	if #lines == 0 then
		return nil
	end
	-- Check whether line 1 or line 2 is a known PoE2 base item
	local baseIdx = nil
	for i = 1, math.min(3, #lines) do
		if build.data.itemBases[lines[i]] then
			baseIdx = i
			break
		end
	end
	if not baseIdx then
		return nil
	end
	local outLines = { "Rarity: " .. rarity }
	if baseIdx == 1 then
		if rarity == "RARE" or rarity == "UNIQUE" then
			outLines[#outLines + 1] = "Imported Item"
		end
		outLines[#outLines + 1] = lines[1]
	else
		outLines[#outLines + 1] = lines[1]
		outLines[#outLines + 1] = lines[baseIdx]
	end
	outLines[#outLines + 1] = "Implicits: 0"
	for i = baseIdx + 1, #lines do
		outLines[#outLines + 1] = lines[i]
	end
	return table.concat(outLines, "\n")
end

-- ============================================================================
-- Capability 4: Dewatering / PoB-Warrior Audit
-- ============================================================================

-- Config variables that are permanent character state and should NOT be stripped
-- unless overridden in a PoB-warrior direction (handled explicitly below).
local PERMANENT_CONFIG_VARS = {
	resistancePenalty = true,
	enemyIsBoss = true,
	enemyLevel = true,
	detonateDeadCorpseLife = true,
	ailmentMode = true,
	cooldownMode = true,
	physMode = true,
	lifeRegenMode = true,
	resourceGainMode = true,
	EHPUnluckyWorstOf = true,
	DisableEHPGainOnBlock = true,
	armourCalculationMode = true,
	warcryMode = true,
	enemySizePreset = true,
	presetBossSkills = true,
}

local ENEMY_FAVORABLE_OVERRIDE_VARS = {
	enemyPhysicalReduction = true,
	enemyLightningResist = true,
	enemyColdResist = true,
	enemyFireResist = true,
	enemyChaosResist = true,
	enemyArmour = true,
	enemyEvasion = true,
	enemyDamageRollRange = true,
}

local function isQuestVar(varName)
	if type(varName) ~= "string" then return false end
	if varName:sub(1, 5) == "quest" then return true end
	local meta = configMetaByVar[varName]
	return meta and meta.section == "Quest Rewards"
end

local function isSuspectConfigEntry(varName, val, rawOut)
	if val == nil or val == false or val == "" or val == 0 or val == "None" then
		return false
	end
	if isQuestVar(varName) or PERMANENT_CONFIG_VARS[varName] then
		return false
	end
	-- Keep conditionLowLife if character genuinely reserves >= 65% life; keep conditionFullLife if CI!
	if varName == "conditionLowLife" and safeNum(rawOut.LifeUnreservedPercent, 100) <= safeNum(rawOut.LowLifePercentage, 35) then
		return false
	end
	if varName == "conditionFullLife" and safeNum(rawOut.ChaosResist, 0) >= 100 and safeNum(rawOut.Life, 100) == 1 then
		return false
	end
	if ENEMY_FAVORABLE_OVERRIDE_VARS[varName] then
		return true
	end
	local meta = configMetaByVar[varName]
	if meta then
		-- Skip neutral defaults (e.g. multiplierCurrentManaPercentage=100, companionInPresence=true)
		if meta.defaultState ~= nil and val == meta.defaultState then
			return false
		end
		if meta.defaultIndex ~= nil and meta.list and meta.list[meta.defaultIndex] and val == meta.list[meta.defaultIndex].val then
			return false
		end
		if meta.section == "When In Combat" or meta.section == "For Effective DPS" or meta.section == "Skill Options" or meta.section == "General" then
			return true
		end
	end
	return true
end

-- ============================================================================
-- Capability 5: Candidate Item Upgrade Diff Helper
-- ============================================================================

local function buildStatComparison(baseOut, newOut)
	local bFull = safeNum(baseOut.FullDPS, 0)
	local aFull = safeNum(newOut.FullDPS, 0)
	local bTot = safeNum(baseOut.TotalDPS, 0)
	local aTot = safeNum(newOut.TotalDPS, 0)
	local bComb = safeNum(baseOut.CombinedDPS, 0)
	local aComb = safeNum(newOut.CombinedDPS, 0)
	local bEff = (bFull > 0 and bFull) or (bComb > 0 and bComb) or bTot
	local aEff = (aFull > 0 and aFull) or (aComb > 0 and aComb) or aTot

	local bEhp = safeNum(baseOut.TotalEHP, 0)
	local aEhp = safeNum(newOut.TotalEHP, 0)

	local function pct(before, after)
		if before <= 0 then
			return after > 0 and 100.0 or 0.0
		end
		return ((after - before) / before) * 100.0
	end

	return {
		before = extractStatsTable(baseOut),
		after = extractStatsTable(newOut),
		delta = {
			EffectiveDPS = aEff - bEff,
			FullDPS = aFull - bFull,
			TotalDPS = aTot - bTot,
			CombinedDPS = aComb - bComb,
			TotalEHP = aEhp - bEhp,
			Life = safeNum(newOut.Life, 0) - safeNum(baseOut.Life, 0),
			EnergyShield = safeNum(newOut.EnergyShield, 0) - safeNum(baseOut.EnergyShield, 0),
			Mana = safeNum(newOut.Mana, 0) - safeNum(baseOut.Mana, 0),
			Spirit = safeNum(newOut.Spirit, 0) - safeNum(baseOut.Spirit, 0),
			SpiritUnreserved = safeNum(newOut.SpiritUnreserved, 0) - safeNum(baseOut.SpiritUnreserved, 0),
			FireResist = safeNum(newOut.FireResist, 0) - safeNum(baseOut.FireResist, 0),
			ColdResist = safeNum(newOut.ColdResist, 0) - safeNum(baseOut.ColdResist, 0),
			LightningResist = safeNum(newOut.LightningResist, 0) - safeNum(baseOut.LightningResist, 0),
			ChaosResist = safeNum(newOut.ChaosResist, 0) - safeNum(baseOut.ChaosResist, 0),
			PhysicalMaximumHitTaken = safeNum(newOut.PhysicalMaximumHitTaken, 0) - safeNum(baseOut.PhysicalMaximumHitTaken, 0),
			ElementalMaximumHitTaken = minElementalMaxHit(newOut) - minElementalMaxHit(baseOut),
			ChaosMaximumHitTaken = safeNum(newOut.ChaosMaximumHitTaken, 0) - safeNum(baseOut.ChaosMaximumHitTaken, 0),
			Armour = safeNum(newOut.Armour, 0) - safeNum(baseOut.Armour, 0),
			Evasion = safeNum(newOut.Evasion, 0) - safeNum(baseOut.Evasion, 0),
			DeflectChance = safeNum(newOut.DeflectChance, 0) - safeNum(baseOut.DeflectChance, 0),
			EffectiveBlockChance = safeNum(newOut.EffectiveBlockChance, 0) - safeNum(baseOut.EffectiveBlockChance, 0),
		},
		pctChange = {
			EffectiveDPS = pct(bEff, aEff),
			FullDPS = pct(bFull, aFull),
			TotalDPS = pct(bTot, aTot),
			TotalEHP = pct(bEhp, aEhp),
		},
	}
end

local function collectSwapWarnings(newOut, item)
	local warnings = {}
	if safeNum(newOut.SpiritUnreserved, 0) < 0 then
		warnings[#warnings + 1] = string.format("Insufficient Spirit after swap (SpiritUnreserved = %.0f)", safeNum(newOut.SpiritUnreserved, 0))
	end
	if item and item.requirements then
		local reqStr = tonumber(item.requirements.strMod or item.requirements.str) or 0
		local reqDex = tonumber(item.requirements.dexMod or item.requirements.dex) or 0
		local reqInt = tonumber(item.requirements.intMod or item.requirements.int) or 0
		if reqStr > safeNum(newOut.Str, 0) then
			warnings[#warnings + 1] = string.format("Unmet Strength requirement (%d required, %.0f available)", reqStr, safeNum(newOut.Str, 0))
		end
		if reqDex > safeNum(newOut.Dex, 0) then
			warnings[#warnings + 1] = string.format("Unmet Dexterity requirement (%d required, %.0f available)", reqDex, safeNum(newOut.Dex, 0))
		end
		if reqInt > safeNum(newOut.Int, 0) then
			warnings[#warnings + 1] = string.format("Unmet Intelligence requirement (%d required, %.0f available)", reqInt, safeNum(newOut.Int, 0))
		end
	end
	for _, elem in ipairs({ "Fire", "Cold", "Lightning" }) do
		local r = safeNum(newOut[elem .. "Resist"], 0)
		if r < 75 then
			warnings[#warnings + 1] = string.format("%s Resistance uncapped (%.0f%% < 75%%)", elem, r)
		end
	end
	return warnings
end

local function resolveCompatibleSlot(item, explicitSlot)
	if explicitSlot and explicitSlot ~= "" then
		if not build.itemsTab.slots[explicitSlot] then
			error("Unknown equipment slot '" .. tostring(explicitSlot) .. "'", 0)
		end
		return explicitSlot
	end
	local okP, primary = pcall(function() return item:GetPrimarySlot() end)
	if okP and primary and build.itemsTab.slots[primary] then
		return primary
	end
	for _, slotCtrl in ipairs(build.itemsTab.orderedSlots or {}) do
		if build.itemsTab:IsItemValidForSlot(item, slotCtrl.slotName) then
			return slotCtrl.slotName
		end
	end
	error("Could not auto-detect compatible slot for item base '" .. tostring(item.baseName) .. "'", 0)
end

-- ============================================================================
-- JSON-RPC Methods Table
-- ============================================================================

local methods = {}

methods.ping = function()
	local slotNames = {}
	for k, _ in pairs(slots) do slotNames[#slotNames + 1] = k end
	table.sort(slotNames)
	return {
		ok = true,
		bridgeVersion = "0.1.0",
		luajitVersion = jit and jit.version or _VERSION,
		zlibVersion = zlibVer,
		treeVersion = latestTreeVersion,
		bootSeconds = bootSeconds,
		activeSlot = activeSlot,
		slots = slotNames,
	}
end

-- ---- Capability 1: Slot Management ----

methods.list_slots = function()
	if activeSlot and slots[activeSlot] then
		slots[activeSlot].xml = saveEngineToXML()
	end
	local out = {}
	for name, entry in pairs(slots) do
		out[#out + 1] = {
			slot = name,
			rev = entry.rev or 1,
			isActive = (name == activeSlot),
			undoDepth = #(entry.undoStack or {}),
			redoDepth = #(entry.redoStack or {}),
			xmlBytes = #(entry.xml or ""),
		}
	end
	table.sort(out, function(a, b) return a.slot < b.slot end)
	return { activeSlot = activeSlot, slots = out }
end

methods.switch_slot = function(params)
	if not params or not params.slot then
		error("params.slot is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, params.createIfMissing ~= false)
	return {
		slot = slotName,
		rev = entry.rev,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end

methods.clone_slot = function(params)
	if not params or not params.target then
		error("params.target is required", 0)
	end
	local srcName = normalizeSlotName(params, "source")
	local dstName = normalizeSlotName(params, "target")
	if not slots[srcName] then
		error("Source slot '" .. srcName .. "' does not exist", 0)
	end
	if srcName == activeSlot then
		slots[srcName].xml = saveEngineToXML()
	end
	slots[dstName] = {
		name = dstName,
		xml = slots[srcName].xml,
		rev = 1,
		undoStack = {},
		redoStack = {},
	}
	if params.activate then
		ensureSlot(dstName, false)
	end
	return {
		ok = true,
		source = srcName,
		target = dstName,
		activeSlot = activeSlot,
		xmlBytes = #slots[dstName].xml,
	}
end

local function applyCharacterOptions(params)
	if not params then return end
	local tree = build.spec and build.spec.tree
	if params.className and tree then
		local targetClass = tostring(params.className):lower()
		local matchedCid
		for cid, c in pairs(tree.classes or {}) do
			if type(c) == "table" and c.name and c.name:lower() == targetClass then
				matchedCid = cid
				break
			end
		end
		if not matchedCid then
			error("Unknown PoE2 class '" .. tostring(params.className) .. "'", 0)
		end
		build.spec:SelectClass(matchedCid)
	end
	if params.ascendancy and tree then
		local targetAsc = tostring(params.ascendancy):lower()
		if targetAsc == "none" or targetAsc == "" then
			build.spec:SelectAscendClass(0)
		else
			local c = tree.classes[build.spec.curClassId]
			local matchedAid
			for aid, a in ipairs((c and c.classes) or {}) do
				if (a.name and a.name:lower() == targetAsc) or (a.internalId and a.internalId:lower() == targetAsc) then
					matchedAid = aid
					break
				end
			end
			if not matchedAid then
				-- Also allow matching ascendancy directly without passing className first!
				for cid, cls in pairs(tree.classes or {}) do
					if type(cls) == "table" and cls.classes then
						for aid, a in ipairs(cls.classes) do
							if (a.name and a.name:lower() == targetAsc) or (a.internalId and a.internalId:lower() == targetAsc) then
								build.spec:SelectClass(cid)
								matchedAid = aid
								break
							end
						end
					end
					if matchedAid then break end
				end
			end
			if not matchedAid then
				error("Unknown PoE2 ascendancy '" .. tostring(params.ascendancy) .. "'", 0)
			end
			build.spec:SelectAscendClass(matchedAid)
		end
	end
	if params.level ~= nil then
		local lvl = tonumber(params.level)
		if not lvl or lvl < 1 or lvl > 100 then
			error("Invalid character level (must be 1..100)", 0)
		end
		build.characterLevel = math.floor(lvl)
		build.characterLevelAutoMode = false
	end
	if params.name and params.name ~= "" then
		build.buildName = tostring(params.name)
	end
end

methods.reset_slot = function(params)
	local slotName = normalizeSlotName(params, "slot")
	if activeSlot ~= slotName and slots[activeSlot] then
		slots[activeSlot].xml = saveEngineToXML()
	end
	local prevEntry = slots[slotName]
	local undoStack = prevEntry and prevEntry.undoStack or {}
	if prevEntry and prevEntry.xml then
		table.insert(undoStack, prevEntry.xml)
		if #undoStack > MAX_HISTORY then table.remove(undoStack, 1) end
	end
	resetEngineToNewBuild((params and params.name) or slotName)
	activeSlot = slotName
	applyCharacterOptions(params)
	refreshBuild()
	slots[slotName] = {
		name = slotName,
		xml = saveEngineToXML(),
		rev = (prevEntry and (prevEntry.rev + 1)) or 1,
		undoStack = undoStack,
		redoStack = {},
	}
	return {
		slot = slotName,
		rev = slots[slotName].rev,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end
methods.new_build = methods.reset_slot

methods.delete_slot = function(params)
	if not params or not params.slot then
		error("params.slot is required", 0)
	end
	local slotName = tostring(params.slot)
	if slotName == "main" then
		error("Cannot delete primary 'main' slot; use reset_slot instead", 0)
	end
	if not slots[slotName] then
		error("Unknown slot '" .. slotName .. "'", 0)
	end
	if activeSlot == slotName then
		loadEngineFromXML(slots["main"].xml, "main")
		activeSlot = "main"
	end
	slots[slotName] = nil
	return { ok = true, deleted = slotName, activeSlot = activeSlot }
end

methods.undo = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	if #entry.undoStack == 0 then
		error("Nothing to undo in slot '" .. slotName .. "'", 0)
	end
	local currentXml = saveEngineToXML()
	table.insert(entry.redoStack, currentXml)
	local prevXml = table.remove(entry.undoStack)
	loadEngineFromXML(prevXml, slotName)
	entry.xml = prevXml
	entry.rev = entry.rev + 1
	return {
		slot = slotName,
		rev = entry.rev,
		undoDepth = #entry.undoStack,
		redoDepth = #entry.redoStack,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end

methods.redo = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	if #entry.redoStack == 0 then
		error("Nothing to redo in slot '" .. slotName .. "'", 0)
	end
	local currentXml = saveEngineToXML()
	table.insert(entry.undoStack, currentXml)
	local nextXml = table.remove(entry.redoStack)
	loadEngineFromXML(nextXml, slotName)
	entry.xml = nextXml
	entry.rev = entry.rev + 1
	return {
		slot = slotName,
		rev = entry.rev,
		undoDepth = #entry.undoStack,
		redoDepth = #entry.redoStack,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end

-- ---- Capability 2: Import / Export (Share Code, XML, GGG .build JSON) ----

methods.load_build_xml = function(params)
	if not params or type(params.xml) ~= "string" or params.xml == "" then
		error("params.xml (non-empty XML string) is required", 0)
	end
	if not params.xml:find("<PathOfBuilding", 1, true) then
		error("Invalid PoB2 XML: missing <PathOfBuilding> root element", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	pushUndoSnapshot(entry)
	loadEngineFromXML(params.xml, params.name or slotName)
	entry.rev = entry.rev + 1
	entry.xml = saveEngineToXML()
	return {
		slot = slotName,
		rev = entry.rev,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end

methods.export_build_xml = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	local xml = build:SaveDB("xml-export")
	return { slot = slotName, xml = xml, xmlBytes = #xml }
end

methods.load_build_code = function(params)
	if not params or type(params.code) ~= "string" or params.code == "" then
		error("params.code (PoB2 share code) is required", 0)
	end
	local cleaned = params.code:gsub("%s+", "")
	if cleaned:match("^https?://") then
		error("params.code looks like a URL; fetch the raw share code first and pass the base64url string", 0)
	end
	local stdB64 = padBase64(cleaned:gsub("%-", "+"):gsub("_", "/"))
	local compressed = base64.decode(stdB64)
	if not compressed or #compressed == 0 then
		error("Failed to base64-decode PoB2 share code", 0)
	end
	local xmlText, infErr = realInflate(compressed)
	if not xmlText or not xmlText:find("<PathOfBuilding", 1, true) then
		error("Failed to inflate PoB2 share code into valid XML: " .. tostring(infErr or "missing <PathOfBuilding>"), 0)
	end
	return methods.load_build_xml({
		slot = params.slot,
		xml = xmlText,
		name = params.name,
	})
end

methods.export_build_code = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	local xmlText = build:SaveDB("code")
	local compressed = realDeflate(xmlText, false)
	local code = base64.encode(compressed):gsub("%+", "-"):gsub("/", "_")
	return {
		slot = slotName,
		code = code,
		codeLength = #code,
		xmlBytes = #xmlText,
	}
end

methods.export_ggg_build = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	local metadata = {
		name = (params and params.name) or build.buildName or "Unnamed Build",
		author = (params and params.author) or "poe2brain",
		description = (params and params.description) or "",
		useGeneratedItemText = (params == nil or params.useGeneratedItemText ~= false),
	}
	local selection = {
		specIndex = build.treeTab.activeSpec,
		skillSetId = build.skillsTab.activeSkillSetId,
		itemSetId = build.itemsTab.activeItemSetId,
	}
	-- Guard against upstream BuildExportPoE2.lua:155 `gem.gemData?.grantedEffect.support`
	-- nil-deref when a legacy/unresolved gem has gem.gemData == nil.
	local savedGemLists = {}
	local skillSet = build.skillsTab.skillSets[selection.skillSetId]
	if skillSet and skillSet.socketGroupList then
		for gIdx, grp in ipairs(skillSet.socketGroupList) do
			if grp.gemList then
				savedGemLists[gIdx] = grp.gemList
				local filtered = {}
				for _, g in ipairs(grp.gemList) do
					if g.gemData and g.gemData.grantedEffect then
						filtered[#filtered + 1] = g
					end
				end
				grp.gemList = filtered
			end
		end
	end
	local okExp, jsonStr, exportErr = pcall(BuildExportPoE2.Export, build, metadata, selection)
	local tbl = okExp and jsonStr and BuildExportPoE2.BuildTable(build, metadata, selection) or nil
	if skillSet and skillSet.socketGroupList then
		for gIdx, origList in pairs(savedGemLists) do
			skillSet.socketGroupList[gIdx].gemList = origList
		end
	end
	if not okExp then
		error("BuildExportPoE2 threw error: " .. tostring(jsonStr), 0)
	end
	if not jsonStr then
		error("BuildExportPoE2 failed: " .. tostring(exportErr), 0)
	end
	return {
		slot = slotName,
		json = jsonStr,
		build = tbl,
	}
end

methods.load_ggg_build = function(params)
	if not params or (not params.json and not params.build) then
		error("params.json (GGG .build JSON string) or params.build (object) is required", 0)
	end
	local doc = params.build
	if type(doc) ~= "table" then
		local decoded, _, decErr = dkjson.decode(tostring(params.json))
		if not decoded or type(decoded) ~= "table" then
			error("Invalid GGG .build JSON: " .. tostring(decErr or "expected JSON object"), 0)
		end
		doc = decoded
	end

	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	pushUndoSnapshot(entry)
	resetEngineToNewBuild(doc.name or slotName)
	activeSlot = slotName

	if doc.ascendancy and doc.ascendancy ~= "" then
		pcall(applyCharacterOptions, { ascendancy = doc.ascendancy })
	end
	if params.className or params.level then
		applyCharacterOptions({ className = params.className, level = params.level, name = doc.name })
	elseif doc.name and doc.name ~= "" then
		build.buildName = tostring(doc.name)
	end

	-- 1. Allocate passives
	local importedPassives = 0
	if type(doc.passives) == "table" then
		local stringMap = buildStringIdMap(build.spec)
		for _, p in ipairs(doc.passives) do
			local sid = type(p) == "string" and p or (type(p) == "table" and p.id)
			local wset = type(p) == "table" and tonumber(p.weapon_set) or nil
			local nodeId = sid and (stringMap[sid] or stringMap[tostring(sid):lower()])
			if nodeId and build.spec.nodes[nodeId] then
				local node = build.spec.nodes[nodeId]
				node.alloc = true
				if wset and wset > 0 then
					node.allocMode = wset
				end
				build.spec.allocNodes[nodeId] = node
				importedPassives = importedPassives + 1
			end
		end
		build.spec:BuildAllDependsAndPaths()
	end

	-- 2. Populate skills & support_skills
	local importedSkills = 0
	if type(doc.skills) == "table" then
		build.skillsTab.socketGroupList = {}
		for sIdx, sk in ipairs(doc.skills) do
			if type(sk) == "table" and sk.id then
				local activeData = findGemDataByIdentifier(sk.id)
				if activeData then
					local actLvl, actQual = parseAdditionalTextLevelQuality(sk.additional_text, false)
					local gemList = {
						{
							nameSpec = activeData.name or "",
							gemId = activeData.id,
							gemData = activeData,
							level = actLvl,
							quality = actQual,
							enabled = true,
						},
					}
					for _, sup in ipairs(sk.support_skills or {}) do
						local supId = type(sup) == "string" and sup or (type(sup) == "table" and sup.id)
						local supText = type(sup) == "table" and sup.additional_text or nil
						local supData = findGemDataByIdentifier(supId)
						if supData then
							local supLvl, supQual = parseAdditionalTextLevelQuality(supText, true)
							gemList[#gemList + 1] = {
								nameSpec = supData.name or "",
								gemId = supData.id,
								gemData = supData,
								level = supLvl,
								quality = supQual,
								enabled = true,
							}
						end
					end
					local group = {
						label = activeData.name or "",
						enabled = true,
						includeInFullDPS = (sIdx == 1),
						mainActiveSkill = 1,
						gemList = gemList,
					}
					table.insert(build.skillsTab.socketGroupList, group)
					build.skillsTab:ProcessSocketGroup(group)
					importedSkills = importedSkills + 1
				end
			end
		end
		if #build.skillsTab.socketGroupList > 0 then
			build.mainSocketGroup = 1
		end
	end

	-- 3. Reconstruct & equip inventory_slots
	local importedItems = 0
	if type(doc.inventory_slots) == "table" then
		local revSlotMap = {}
		for pobSlotName, mapping in pairs(build.data.buildFileInventorySlotMap or {}) do
			local key = tostring(mapping.id) .. ":" .. tostring(mapping.slot_x or 0)
			revSlotMap[key] = pobSlotName
			if not revSlotMap[tostring(mapping.id)] then
				revSlotMap[tostring(mapping.id)] = pobSlotName
			end
		end
		local activeItemSet = build.itemsTab.activeItemSet
		for _, inv in ipairs(doc.inventory_slots) do
			if type(inv) == "table" and inv.inventory_id then
				local key = tostring(inv.inventory_id) .. ":" .. tostring(inv.slot_x or 0)
				local pobSlotName = revSlotMap[key] or revSlotMap[tostring(inv.inventory_id)]
				if pobSlotName then
					if activeItemSet and activeItemSet[pobSlotName] and inv.additional_text then
						activeItemSet[pobSlotName].note = tostring(inv.additional_text)
					end
					local rawText = reconstructItemRawFromBuildNote(inv.additional_text)
					if rawText then
						local okI, item = pcall(function() return new("Item"):Item(rawText) end)
						if okI and item and item.base and build.itemsTab.slots[pobSlotName] then
							build.itemsTab:AddItem(item, true)
							build.itemsTab.slots[pobSlotName]:SetSelItemId(item.id)
							importedItems = importedItems + 1
						end
					end
				end
			end
		end
	end

	finalizeSlotMutation(entry)
	return {
		slot = slotName,
		rev = entry.rev,
		importedPassives = importedPassives,
		importedSkills = importedSkills,
		importedItems = importedItems,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end

-- ---- Capability 3: Stats, Summary & Build Editing Helpers ----

methods.get_stats = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	return {
		slot = slotName,
		rev = entry.rev,
		character = extractCharacterInfo(),
		stats = extractStatsTable(nil, params and params.fields),
	}
end

methods.get_build_summary = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	return {
		slot = slotName,
		rev = entry.rev,
		character = extractCharacterInfo(),
		stats = extractStatsTable(nil, params and params.fields),
		skills = extractSkillsInfo(),
		tree = extractTreeInfo(),
		items = extractItemsInfo(params and params.includeItemRaw == true),
		config = extractConfigInfo(),
	}
end

methods.list_classes = function()
	local tree = build.spec.tree
	local out = {}
	for cid, c in pairs(tree.classes or {}) do
		if type(cid) == "number" and type(c) == "table" and c.name then
			local ascs = {}
			for aid, a in ipairs(c.classes or {}) do
				ascs[#ascs + 1] = {
					id = aid,
					name = a.name,
					internalId = a.internalId or dkjson.null,
				}
			end
			out[#out + 1] = {
				classId = cid,
				name = c.name,
				ascendancies = ascs,
			}
		end
	end
	table.sort(out, function(a, b) return a.classId < b.classId end)
	return { classes = out }
end

methods.set_character = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	pushUndoSnapshot(entry)
	applyCharacterOptions(params)
	finalizeSlotMutation(entry)
	return {
		slot = slotName,
		rev = entry.rev,
		character = extractCharacterInfo(),
		stats = extractStatsTable(),
	}
end

methods.equip_item = function(params)
	if not params or type(params.itemText) ~= "string" or params.itemText == "" then
		error("params.itemText is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	local item = new("Item"):Item(params.itemText)
	if not item or not item.base then
		error("Unrecognized PoE2 item base or malformed item text", 0)
	end
	local targetSlot = resolveCompatibleSlot(item, params.targetSlot)
	pushUndoSnapshot(entry)
	build.itemsTab:AddItem(item, true)
	build.itemsTab.slots[targetSlot]:SetSelItemId(item.id)
	if params.active ~= nil then
		build.itemsTab.slots[targetSlot].active = (params.active == true)
	end
	finalizeSlotMutation(entry)
	return {
		ok = true,
		slot = slotName,
		rev = entry.rev,
		equippedSlot = targetSlot,
		itemId = item.id,
		itemName = item.name,
		baseName = item.baseName,
		rarity = item.rarity,
		stats = extractStatsTable(),
	}
end

methods.unequip_item = function(params)
	if not params or not params.targetSlot then
		error("params.targetSlot is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	local ctrl = build.itemsTab.slots[params.targetSlot]
	if not ctrl then
		error("Unknown equipment slot '" .. tostring(params.targetSlot) .. "'", 0)
	end
	pushUndoSnapshot(entry)
	ctrl:SetSelItemId(0)
	finalizeSlotMutation(entry)
	return {
		ok = true,
		slot = slotName,
		rev = entry.rev,
		unequippedSlot = params.targetSlot,
		stats = extractStatsTable(),
	}
end

methods.paste_socket_group = function(params)
	if not params or type(params.text) ~= "string" or params.text == "" then
		error("params.text (socket group paste text, e.g. 'Spark 20/20  1\\n') is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	pushUndoSnapshot(entry)
	build.skillsTab:PasteSocketGroup(params.text)
	local groupIdx = #build.skillsTab.socketGroupList
	local group = build.skillsTab.socketGroupList[groupIdx]
	local unresolvedGems = {}
	if group then
		if params.label then group.label = tostring(params.label) end
		if params.slotName then group.slot = tostring(params.slotName) end
		if params.includeInFullDPS ~= nil then
			group.includeInFullDPS = (params.includeInFullDPS == true)
		elseif groupIdx == 1 then
			group.includeInFullDPS = true
		end
		-- Auto-resolve tiered support names (e.g. "Rapid Casting" -> "Rapid Casting III/II/I")
		local reprocess = false
		for _, gem in ipairs(group.gemList or {}) do
			if not gem.gemData and gem.nameSpec and gem.nameSpec ~= "" then
				local resolved = findGemDataByIdentifier(gem.nameSpec)
				if resolved then
					gem.gemId = resolved.id
					gem.gemData = resolved
					gem.nameSpec = resolved.name or gem.nameSpec
					reprocess = true
				else
					unresolvedGems[#unresolvedGems + 1] = gem.nameSpec
				end
			end
		end
		if reprocess then
			build.skillsTab:ProcessSocketGroup(group)
		end
	end
	if params.setMain or not build.mainSocketGroup or build.mainSocketGroup == 0 then
		build.mainSocketGroup = groupIdx
	end
	finalizeSlotMutation(entry)
	return {
		ok = true,
		slot = slotName,
		rev = entry.rev,
		groupIndex = groupIdx,
		unresolvedGems = unresolvedGems,
		skills = extractSkillsInfo(),
		stats = extractStatsTable(),
	}
end

methods.set_config = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	pushUndoSnapshot(entry)
	if params and type(params.values) == "table" then
		for k, v in pairs(params.values) do
			if v == dkjson.null then
				build.configTab.input[k] = nil
			else
				build.configTab.input[k] = v
			end
		end
	end
	if params and type(params.resetKeys) == "table" then
		for _, k in ipairs(params.resetKeys) do
			build.configTab.input[k] = nil
		end
	end
	if params and params.customMods ~= nil then
		local txt = (params.customMods == dkjson.null) and "" or tostring(params.customMods)
		setActiveCustomModsText(txt)
	end
	build.configTab:BuildModList()
	finalizeSlotMutation(entry)
	return {
		ok = true,
		slot = slotName,
		rev = entry.rev,
		config = extractConfigInfo(),
		stats = extractStatsTable(),
	}
end

methods.list_valid_supports = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	local gIdx = tonumber(params and params.groupIndex) or build.mainSocketGroup or 1
	local group = build.skillsTab.socketGroupList[gIdx]
	if not group then
		error("Unknown socket group index " .. tostring(gIdx), 0)
	end
	local activeSkill = group.displaySkillList and group.displaySkillList[group.mainActiveSkill or 1]
	if not activeSkill then
		return { slot = slotName, groupIndex = gIdx, supports = {} }
	end
	local supports = {}
	for gemId, gemData in pairs(build.data.gems or {}) do
		if gemData.grantedEffect and gemData.grantedEffect.support then
			local okS, canSup = pcall(calcLib.canGrantedEffectSupportActiveSkill, gemData.grantedEffect, activeSkill)
			if okS and canSup then
				supports[#supports + 1] = {
					gemId = gemId,
					gameId = gemData.gameId or dkjson.null,
					name = gemData.name or gemId,
				}
			end
		end
	end
	table.sort(supports, function(a, b) return a.name < b.name end)
	return { slot = slotName, groupIndex = gIdx, count = #supports, supports = supports }
end

-- ---- Capability 4: Dewatering / PoB-Warrior Audit ----

methods.audit_build = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local slotEntry = ensureSlot(slotName, false)
	local applyDewater = (params and params.apply) == true

	local keepSet = {}
	for _, k in ipairs((params and params.keep) or {}) do
		keepSet[tostring(k)] = true
	end
	local enforcePinnacle = (params == nil or params.pinnacleBoss ~= false)
	local origXml = applyDewater and saveEngineToXML() or nil

	-- 1. Snapshot original state
	local origInput = copyTable(build.configTab.input)
	local origPlaceholder = copyTable(build.configTab.placeholder)
	local activeConfigSet = build.configTab.configSets and build.configTab.configSets[build.configTab.activeConfigSetId]
	local origCustomModsList = (activeConfigSet and activeConfigSet.customModsList)
		and copyTable(activeConfigSet.customModsList)
		or nil
	local origSlotActive = {}
	for sName, sCtrl in pairs(build.itemsTab.slots or {}) do
		origSlotActive[sName] = sCtrl.active
	end

	local function restoreOriginalState()
		wipeTable(build.configTab.input)
		for k, v in pairs(origInput) do build.configTab.input[k] = v end
		wipeTable(build.configTab.placeholder)
		for k, v in pairs(origPlaceholder) do build.configTab.placeholder[k] = v end
		local curSet = build.configTab.configSets and build.configTab.configSets[build.configTab.activeConfigSetId]
		if curSet then
			curSet.customModsList = origCustomModsList
				and copyTable(origCustomModsList)
				or { { title = "Default", enabled = true, text = "" } }
		end
		for sName, wasActive in pairs(origSlotActive) do
			if build.itemsTab.slots[sName] then
				build.itemsTab.slots[sName].active = wasActive
			end
		end
		build.configTab:BuildModList()
		refreshBuild()
	end

	local rawStats = extractStatsTable()
	local rawEffDps = rawStats.EffectiveDPS
	local rawEhp = rawStats.TotalEHP

	local auditOk, auditResult = pcall(function()
		-- Gather candidate unsustainable levers
		local actions = {}

		-- A. Non-Pinnacle boss setting
		local curBoss = origInput.enemyIsBoss
		if enforcePinnacle and not keepSet["enemyIsBoss"] and curBoss ~= "Pinnacle" and curBoss ~= "Uber" then
			actions[#actions + 1] = {
				id = "config:enemyIsBoss",
				category = "boss_preset",
				key = "enemyIsBoss",
				label = "Enemy Boss Preset -> Pinnacle Boss",
				originalValue = curBoss or "None",
				sustainableValue = "Pinnacle",
				apply = function()
					build.configTab.input.enemyIsBoss = "Pinnacle"
				end,
			}
		end

		-- B. Lenient resistance penalty on endgame characters (level >= 65)
		local curResPen = tonumber(origInput.resistancePenalty)
		if (build.characterLevel or 1) >= 65 and not keepSet["resistancePenalty"] and curResPen ~= nil and curResPen > -60 then
			actions[#actions + 1] = {
				id = "config:resistancePenalty",
				category = "resistance_penalty",
				key = "resistancePenalty",
				label = "Endgame Resistance Penalty (-60%)",
				originalValue = curResPen,
				sustainableValue = -60,
				apply = function()
					build.configTab.input.resistancePenalty = -60
				end,
			}
		end

		-- C. Arbitrary customMods text (both ConfigTab customModsList blocks & legacy input.customMods)
		local customTxt = getActiveCustomModsText()
		if customTxt ~= "" and not keepSet["customMods"] then
			actions[#actions + 1] = {
				id = "config:customMods",
				category = "custom_mods",
				key = "customMods",
				label = "Custom Modifiers (" .. customTxt:gsub("\n", "; ") .. ")",
				originalValue = customTxt,
				sustainableValue = "",
				apply = function()
					setActiveCustomModsText("")
				end,
			}
		end

		-- D. Suspect ConfigOptions flags / overrides
		for k, v in pairs(origInput) do
			if k ~= "customMods" and not keepSet[k] and isSuspectConfigEntry(k, v, rawStats) then
				local meta = configMetaByVar[k]
				actions[#actions + 1] = {
					id = "config:" .. tostring(k),
					category = ENEMY_FAVORABLE_OVERRIDE_VARS[k] and "enemy_override" or ((meta and meta.section) or "config"),
					key = tostring(k),
					label = (meta and meta.label) or tostring(k),
					originalValue = v,
					sustainableValue = dkjson.null,
					apply = function()
						build.configTab.input[k] = nil
					end,
				}
			end
		end

		-- E. Active Flasks & Charms
		if not keepSet["flasks"] and not keepSet["charms"] then
			for _, slotCtrl in ipairs(build.itemsTab.orderedSlots or {}) do
				local sName = slotCtrl.slotName
				if slotCtrl.active and (slotCtrl.selItemId or 0) ~= 0 and not keepSet[sName] then
					local isFlask = sName:find("^Flask") ~= nil
					local isCharm = sName:find("^Charm") ~= nil
					if (isFlask and not keepSet["flasks"]) or (isCharm and not keepSet["charms"]) then
						local it = build.itemsTab.items[slotCtrl.selItemId]
						actions[#actions + 1] = {
							id = "slot:" .. sName,
							category = isFlask and "flask" or "charm",
							key = sName,
							label = sName .. ": " .. tostring((it and it.name) or "Active"),
							originalValue = true,
							sustainableValue = false,
							apply = function()
								build.itemsTab.slots[sName].active = false
							end,
						}
					end
				end
			end
		end

		-- Evaluate marginal impact of each culprit one-by-one
		local culprits = {}
		for _, act in ipairs(actions) do
			restoreOriginalState()
			act.apply()
			build.configTab:BuildModList()
			refreshBuild()
			local stepStats = extractStatsTable()
			local stepDps = stepStats.EffectiveDPS
			local stepEhp = stepStats.TotalEHP
			local dpsDropPct = rawEffDps > 0 and ((rawEffDps - stepDps) / rawEffDps * 100.0) or 0.0
			local dpsInflationPct = stepDps > 0 and ((rawEffDps - stepDps) / stepDps * 100.0) or (rawEffDps > 0 and 100.0 or 0.0)
			local ehpDropPct = rawEhp > 0 and ((rawEhp - stepEhp) / rawEhp * 100.0) or 0.0
			local ehpInflationPct = stepEhp > 0 and ((rawEhp - stepEhp) / stepEhp * 100.0) or (rawEhp > 0 and 100.0 or 0.0)

			if math.abs(dpsInflationPct) >= 0.05 or math.abs(ehpInflationPct) >= 0.05 then
				culprits[#culprits + 1] = {
					id = act.id,
					category = act.category,
					key = act.key,
					label = act.label,
					originalValue = act.originalValue,
					sustainableValue = act.sustainableValue,
					dpsWithout = stepDps,
					ehpWithout = stepEhp,
					dpsDropPct = dpsDropPct,
					dpsInflationPct = dpsInflationPct,
					ehpDropPct = ehpDropPct,
					ehpInflationPct = ehpInflationPct,
				}
			end
		end

		table.sort(culprits, function(a, b)
			local ma = math.max(math.abs(a.dpsInflationPct), math.abs(a.ehpInflationPct))
			local mb = math.max(math.abs(b.dpsInflationPct), math.abs(b.ehpInflationPct))
			return ma > mb
		end)

		-- Apply ALL dewatering actions together for true sustainable Pinnacle Boss stats
		restoreOriginalState()
		for _, act in ipairs(actions) do
			act.apply()
		end
		build.configTab:BuildModList()
		refreshBuild()
		local sustainableStats = extractStatsTable()

		return {
			sustainableStats = sustainableStats,
			culprits = culprits,
			checkedLevers = #actions,
		}
	end)

	if not auditOk then
		restoreOriginalState()
		error("audit_build failed: " .. tostring(auditResult), 0)
	end

	local restoredVerified = true
	if applyDewater then
		if origXml then
			table.insert(slotEntry.undoStack, origXml)
			if #slotEntry.undoStack > MAX_HISTORY then
				table.remove(slotEntry.undoStack, 1)
			end
			slotEntry.redoStack = {}
		end
		slotEntry.rev = (slotEntry.rev or 1) + 1
		slotEntry.xml = saveEngineToXML()
	else
		restoreOriginalState()
		local restoredStats = extractStatsTable()
		restoredVerified = (math.abs(restoredStats.EffectiveDPS - rawStats.EffectiveDPS) < 1e-3)
			and (math.abs(restoredStats.TotalEHP - rawStats.TotalEHP) < 1e-3)
	end

	local sustDps = auditResult.sustainableStats.EffectiveDPS
	local sustEhp = auditResult.sustainableStats.TotalEHP
	local totalDpsInflationPct = sustDps > 0 and ((rawEffDps - sustDps) / sustDps * 100.0) or 0.0
	local totalEhpInflationPct = sustEhp > 0 and ((rawEhp - sustEhp) / sustEhp * 100.0) or 0.0

	return {
		slot = slotName,
		applied = applyDewater,
		character = extractCharacterInfo(),
		skills = extractSkillsInfo(),
		tree = extractTreeInfo(),
		rawStats = rawStats,
		sustainableStats = auditResult.sustainableStats,
		totalDpsInflationPct = totalDpsInflationPct,
		totalEhpInflationPct = totalEhpInflationPct,
		checkedLevers = auditResult.checkedLevers,
		culprits = auditResult.culprits,
		restoredVerified = restoredVerified,
	}
end
methods.dewater_build = methods.audit_build

-- ---- Capability 5: Single & Batch Non-Destructive Candidate Item Upgrade Diffs ----

local function evalSingleCandidateItem(calcFunc, baseOut, entry, index)
	if type(entry) ~= "table" or type(entry.itemText) ~= "string" or entry.itemText == "" then
		return {
			ok = false,
			index = index,
			id = (type(entry) == "table" and entry.id) or tostring(index),
			error = "itemText is required",
		}
	end
	local okParse, item = pcall(function() return new("Item"):Item(entry.itemText) end)
	if not okParse or not item or not item.base then
		return {
			ok = false,
			index = index,
			id = entry.id or tostring(index),
			label = entry.label or dkjson.null,
			error = "Could not parse PoE2 item base or text: " .. tostring(not okParse and item or "unknown base"),
		}
	end
	local okSlot, targetSlot = pcall(resolveCompatibleSlot, item, entry.targetSlot)
	if not okSlot then
		return {
			ok = false,
			index = index,
			id = entry.id or tostring(index),
			label = entry.label or dkjson.null,
			itemName = item.name,
			baseName = item.baseName,
			error = tostring(targetSlot),
		}
	end

	local slotCtrl = build.itemsTab.slots[targetSlot]
	local curItem = (slotCtrl and slotCtrl.selItemId and slotCtrl.selItemId ~= 0) and build.itemsTab.items[slotCtrl.selItemId] or nil

	local newOut = calcFunc({ repSlotName = targetSlot, repItem = item }, true)
	local comp = buildStatComparison(baseOut, newOut)
	local warnings = collectSwapWarnings(newOut, item)

	return {
		ok = true,
		index = index,
		id = entry.id or tostring(index),
		label = entry.label or item.name or tostring(index),
		targetSlot = targetSlot,
		candidate = {
			name = item.name or "",
			baseName = item.baseName or "",
			type = item.type or "",
			rarity = item.rarity or "RARE",
		},
		equipped = curItem and {
			name = curItem.name or "",
			baseName = curItem.baseName or "",
			rarity = curItem.rarity or "NORMAL",
		} or dkjson.null,
		before = comp.before,
		after = comp.after,
		delta = comp.delta,
		pctChange = comp.pctChange,
		warnings = warnings,
	}
end

methods.diff_item = function(params)
	if not params or type(params.itemText) ~= "string" then
		error("params.itemText is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	local calcFunc, baseOut = build.calcsTab:GetMiscCalculator()
	local res = evalSingleCandidateItem(calcFunc, baseOut, params, 1)
	if not res.ok then
		error(res.error, 0)
	end
	res.slot = slotName
	return res
end

methods.diff_items_batch = function(params)
	if not params or type(params.items) ~= "table" then
		error("params.items (array of {id?, label?, targetSlot?, itemText}) is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	local t0 = os.clock()
	local calcFunc, baseOut = build.calcsTab:GetMiscCalculator()
	local results = {}
	local validRanked = {}
	local sortMetric = (params.sortBy and tostring(params.sortBy)) or "EffectiveDPS"

	for i, entry in ipairs(params.items) do
		local r = evalSingleCandidateItem(calcFunc, baseOut, entry, i)
		results[#results + 1] = r
		if r.ok then
			validRanked[#validRanked + 1] = {
				id = r.id,
				index = r.index,
				label = r.label,
				targetSlot = r.targetSlot,
				score = safeNum(r.delta[sortMetric], safeNum(r.delta.EffectiveDPS, 0)),
				effectiveDpsDelta = r.delta.EffectiveDPS,
				effectiveDpsPct = r.pctChange.EffectiveDPS,
				totalEhpDelta = r.delta.TotalEHP,
				totalEhpPct = r.pctChange.TotalEHP,
				warningCount = #r.warnings,
			}
		end
	end

	table.sort(validRanked, function(a, b) return a.score > b.score end)
	local elapsedMs = (os.clock() - t0) * 1000.0
	return {
		slot = slotName,
		count = #results,
		sortBy = sortMetric,
		elapsedMs = elapsedMs,
		results = results,
		ranking = validRanked,
	}
end

-- ---- Capability 6: Passive Node Add/Remove Diff & Top Unallocated Notables ----

methods.diff_passive_nodes = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	build.spec:BuildAllDependsAndPaths()
	local calcFunc, baseOut = build.calcsTab:GetMiscCalculator()

	local addSet = {}
	local remSet = {}
	local addedDetails = {}
	local removedDetails = {}

	for _, rawId in ipairs((params and params.addNodeIds) or {}) do
		local nid = tonumber(rawId)
		local node = nid and build.spec.nodes[nid]
		if not node then
			error("Unknown passive nodeId " .. tostring(rawId), 0)
		end
		if params.includePath and node.path and not node.alloc then
			for _, pNode in ipairs(node.path) do
				if not pNode.alloc and not addSet[pNode] then
					addSet[pNode] = true
					addedDetails[#addedDetails + 1] = { id = pNode.id, name = pNode.dn or "", type = pNode.type or "Normal" }
				end
			end
		elseif not addSet[node] then
			addSet[node] = true
			addedDetails[#addedDetails + 1] = { id = node.id, name = node.dn or "", type = node.type or "Normal" }
		end
	end

	for _, rawId in ipairs((params and params.removeNodeIds) or {}) do
		local nid = tonumber(rawId)
		local node = nid and build.spec.nodes[nid]
		if not node then
			error("Unknown passive nodeId " .. tostring(rawId), 0)
		end
		if params.includeDependents and node.depends and node.alloc then
			for _, dNode in ipairs(node.depends) do
				if dNode.alloc and not remSet[dNode] then
					remSet[dNode] = true
					removedDetails[#removedDetails + 1] = { id = dNode.id, name = dNode.dn or "", type = dNode.type or "Normal" }
				end
			end
		elseif not remSet[node] then
			remSet[node] = true
			removedDetails[#removedDetails + 1] = { id = node.id, name = node.dn or "", type = node.type or "Normal" }
		end
	end

	local calcParams = {}
	if next(addSet) ~= nil then calcParams.addNodes = addSet end
	if next(remSet) ~= nil then calcParams.removeNodes = remSet end

	local newOut = calcFunc(calcParams, true)
	local comp = buildStatComparison(baseOut, newOut)
	return {
		slot = slotName,
		addedNodes = addedDetails,
		removedNodes = removedDetails,
		netPointDelta = #addedDetails - #removedDetails,
		before = comp.before,
		after = comp.after,
		delta = comp.delta,
		pctChange = comp.pctChange,
	}
end

methods.rank_passive_notables = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	build.spec:BuildAllDependsAndPaths()

	local statName = (params and params.stat) or "EffectiveDPS"
	local maxPathDist = tonumber(params and params.maxPathDist) or 8
	local topN = tonumber(params and params.topN) or 15
	local includeKeystones = (params and params.includeKeystones) == true
	local sortBy = (params and params.sortBy) or "perPoint" -- "perPoint" | "total" | "anoint"

	local calcFunc, baseOut = build.calcsTab:GetMiscCalculator()
	local baseStats = extractStatsTable(baseOut)
	local baseVal = safeNum(baseStats[statName], baseStats.EffectiveDPS)
	local useFullDPS = (statName == "FullDPS" or statName == "EffectiveDPS")

	local t0 = os.clock()
	local singleCache = {}
	local candidates = {}
	local fastAnointPrepass = (sortBy == "anoint" and useFullDPS)
	local prepassStat = fastAnointPrepass and "CombinedDPS" or statName
	local basePrepassVal = safeNum(baseStats[prepassStat], baseVal)

	for nodeId, node in pairs(build.spec.nodes or {}) do
		local isTargetType = (node.type == "Notable") or (includeKeystones and node.type == "Keystone")
		if isTargetType and not node.ascendancyName and not node.alloc and node.modKey and node.modKey ~= "" then
			local dist = tonumber(node.pathDist) or 1000
			if sortBy == "anoint" or dist <= maxPathDist then
				if not singleCache[node.modKey] then
					singleCache[node.modKey] = extractStatsTable(calcFunc({ addNodes = { [node] = true } }, not fastAnointPrepass and useFullDPS))
				end
				local singleStats = singleCache[node.modKey]
				local singleVal = safeNum(singleStats[prepassStat], singleStats.EffectiveDPS)
				local singleDelta = singleVal - basePrepassVal

				local fullPathDelta = singleDelta
				local pathIds = {}
				if sortBy ~= "anoint" and dist <= maxPathDist and node.path and #node.path > 0 then
					local pathSet = {}
					for _, pNode in ipairs(node.path) do
						if not pNode.alloc then
							pathSet[pNode] = true
							pathIds[#pathIds + 1] = pNode.id
						end
					end
					if dist > 1 and next(pathSet) ~= nil then
						local pathStats = extractStatsTable(calcFunc({ addNodes = pathSet }, useFullDPS))
						fullPathDelta = safeNum(pathStats[statName], pathStats.EffectiveDPS) - baseVal
					end
				end

				local effectiveDist = (dist > 0 and dist < 1000) and dist or 1
				local perPointDelta = (dist < 1000) and (fullPathDelta / effectiveDist) or 0

				if singleDelta > 0.001 or fullPathDelta > 0.001 then
					candidates[#candidates + 1] = {
						_nodeRef = node,
						nodeId = nodeId,
						stringId = node.stringId or dkjson.null,
						name = node.dn or "",
						type = node.type or "Notable",
						stats = node.sd or {},
						pathDist = dist < 1000 and dist or dkjson.null,
						pathNodeIds = pathIds,
						singleNodeDelta = singleDelta,
						singleNodePct = basePrepassVal > 0 and (singleDelta / basePrepassVal * 100.0) or 0.0,
						fullPathDelta = fullPathDelta,
						fullPathPct = baseVal > 0 and (fullPathDelta / baseVal * 100.0) or 0.0,
						perPointDelta = perPointDelta,
						perPointPct = baseVal > 0 and (perPointDelta / baseVal * 100.0) or 0.0,
					}
				end
			end
		end
	end

	-- Refine top anoint candidates with exact FullDPS multi-skill pass
	if fastAnointPrepass and #candidates > 0 then
		table.sort(candidates, function(a, b) return a.singleNodeDelta > b.singleNodeDelta end)
		local refineLimit = math.min(#candidates, math.max(topN * 3, 25))
		local refined = {}
		local exactCache = {}
		for i = 1, refineLimit do
			local c = candidates[i]
			local nd = c._nodeRef
			if not exactCache[nd.modKey] then
				exactCache[nd.modKey] = extractStatsTable(calcFunc({ addNodes = { [nd] = true } }, true))
			end
			local exVal = safeNum(exactCache[nd.modKey][statName], exactCache[nd.modKey].EffectiveDPS)
			local exDelta = exVal - baseVal
			c.singleNodeDelta = exDelta
			c.singleNodePct = baseVal > 0 and (exDelta / baseVal * 100.0) or 0.0
			c.fullPathDelta = exDelta
			c.fullPathPct = c.singleNodePct
			c._nodeRef = nil
			if exDelta > 0.001 then
				refined[#refined + 1] = c
			end
		end
		candidates = refined
	else
		for _, c in ipairs(candidates) do c._nodeRef = nil end
	end

	table.sort(candidates, function(a, b)
		if sortBy == "anoint" then
			return a.singleNodeDelta > b.singleNodeDelta
		elseif sortBy == "total" then
			return a.fullPathDelta > b.fullPathDelta
		end
		if math.abs(a.perPointDelta - b.perPointDelta) > 1e-6 then
			return a.perPointDelta > b.perPointDelta
		end
		return a.singleNodeDelta > b.singleNodeDelta
	end)

	local top = {}
	for i = 1, math.min(topN, #candidates) do
		top[i] = candidates[i]
	end

	return {
		slot = slotName,
		stat = statName,
		baseValue = baseVal,
		maxPathDist = maxPathDist,
		sortBy = sortBy,
		evaluatedCount = #candidates,
		elapsedMs = (os.clock() - t0) * 1000.0,
		notables = top,
	}
end

methods.alloc_nodes = function(params)
	if not params or type(params.nodeIds) ~= "table" or #params.nodeIds == 0 then
		error("params.nodeIds (non-empty array of passive node IDs) is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, true)
	pushUndoSnapshot(entry)
	build.spec:BuildAllDependsAndPaths()
	local allocated = {}
	for _, rawId in ipairs(params.nodeIds) do
		local nid = tonumber(rawId)
		local node = nid and build.spec.nodes[nid]
		if not node then
			error("Unknown passive nodeId " .. tostring(rawId), 0)
		end
		if params.connectPath ~= false and node.path and #node.path > 0 then
			build.spec:AllocNode(node)
		else
			node.alloc = true
			build.spec.allocNodes[nid] = node
			build.spec:BuildAllDependsAndPaths()
		end
		allocated[#allocated + 1] = nid
	end
	finalizeSlotMutation(entry)
	return {
		ok = true,
		slot = slotName,
		rev = entry.rev,
		allocated = allocated,
		tree = extractTreeInfo(),
		stats = extractStatsTable(),
	}
end

methods.dealloc_nodes = function(params)
	if not params or type(params.nodeIds) ~= "table" or #params.nodeIds == 0 then
		error("params.nodeIds (non-empty array of passive node IDs) is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	pushUndoSnapshot(entry)
	for _, rawId in ipairs(params.nodeIds) do
		local nid = tonumber(rawId)
		local node = nid and build.spec.nodes[nid]
		if node and node.alloc then
			build.spec:DeallocNode(node)
		end
	end
	finalizeSlotMutation(entry)
	return {
		ok = true,
		slot = slotName,
		rev = entry.rev,
		tree = extractTreeInfo(),
		stats = extractStatsTable(),
	}
end

methods.search_nodes = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	build.spec:BuildAllDependsAndPaths()
	local q = tostring((params and params.query) or ""):lower()
	local onlyType = params and params.nodeType -- "Notable" | "Keystone" | "Normal"
	local limit = tonumber(params and params.limit) or 25
	local matches = {}
	for nodeId, node in pairs(build.spec.nodes or {}) do
		if type(nodeId) == "number" and node.dn and node.dn ~= "" then
			if not onlyType or node.type == onlyType then
				local hit = (q == "") or (node.dn:lower():find(q, 1, true) ~= nil)
				if not hit and node.sd then
					for _, line in ipairs(node.sd) do
						if line:lower():find(q, 1, true) then
							hit = true
							break
						end
					end
				end
				if hit then
					matches[#matches + 1] = {
						id = nodeId,
						stringId = node.stringId or dkjson.null,
						name = node.dn,
						type = node.type or "Normal",
						ascendancyName = node.ascendancyName or dkjson.null,
						allocated = node.alloc == true,
						pathDist = node.pathDist or dkjson.null,
						stats = node.sd or {},
					}
				end
			end
		end
	end
	table.sort(matches, function(a, b)
		if a.allocated ~= b.allocated then return a.allocated end
		local da = type(a.pathDist) == "number" and a.pathDist or 9999
		local db = type(b.pathDist) == "number" and b.pathDist or 9999
		if da ~= db then return da < db end
		return a.id < b.id
	end)
	local out = {}
	for i = 1, math.min(limit, #matches) do out[i] = matches[i] end
	return { slot = slotName, total = #matches, nodes = out }
end

-- ---- Capability 7: TradeQueryGenerator Marginal Stat Weights per Slot ----

methods.slot_stat_weights = function(params)
	if not params or not params.targetSlot then
		error("params.targetSlot (e.g. 'Weapon 1', 'Ring 1', 'Body Armour', 'Amulet') is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)

	local targetSlot = tostring(params.targetSlot)
	local slotCtrl = build.itemsTab.slots[targetSlot]
	if not slotCtrl then
		error("Unknown equipment slot '" .. targetSlot .. "'", 0)
	end

	-- Normalize statWeights list
	local statWeights = {}
	if type(params.statWeights) == "table" and #params.statWeights > 0 then
		for _, sw in ipairs(params.statWeights) do
			local st = tostring(sw.stat or "FullDPS")
			if st == "EffectiveDPS" then st = "FullDPS" end
			statWeights[#statWeights + 1] = {
				stat = st,
				weightMult = tonumber(sw.weightMult or sw.weight) or 1.0,
			}
		end
	else
		statWeights[1] = { stat = "FullDPS", weightMult = 1.0 }
	end

	-- If targetSlot is currently empty, temporarily insert a zero-mod white item so
	-- TradeQueryGenerator:StartQuery resolves itemCategory cleanly, then remove it after!
	local tempItemId = nil
	local origSelItemId = slotCtrl.selItemId or 0
	if origSelItemId == 0 and not targetSlot:find("^Jewel") then
		local baseName = params.baseName or DEFAULT_SLOT_BASES[targetSlot]
		if not baseName then
			error("Slot '" .. targetSlot .. "' is empty; pass params.baseName (e.g. 'Gold Ring')", 0)
		end
		local tempItem = new("Item"):Item("Rarity: NORMAL\nSlot Probe\n" .. baseName .. "\nImplicits: 0")
		if not tempItem or not tempItem.base then
			error("Invalid params.baseName '" .. tostring(baseName) .. "'", 0)
		end
		build.itemsTab:AddItem(tempItem, true)
		tempItemId = tempItem.id
		slotCtrl.selItemId = tempItemId
	end

	local t0 = os.clock()
	local capturedQuery, capturedErr
	local qg

	local okRun, runErr = pcall(function()
		qg = new("TradeQueryGenerator"):TradeQueryGenerator({ itemsTab = build.itemsTab })
		local statusMap = { securable = 1, available = 2, onlineleague = 3, online = 4, any = 5 }
		qg.tradeTypeIndex = statusMap[tostring(params.statusOption or "available")] or 2
		qg.requesterCallback = function(_, qTable, errMsg)
			capturedQuery = qTable
			capturedErr = errMsg
		end

		local options = {
			statWeights = statWeights,
			jewelType = params.jewelType or "Base",
			includeCorrupted = params.includeCorrupted == true,
			includeRunes = params.includeRunes == true,
			includeMirrored = params.includeMirrored == true,
			sockets = tonumber(params.sockets) or 0,
			maxLevel = tonumber(params.maxLevel) or nil,
			maxPrice = tonumber(params.maxPrice) or nil,
			maxPriceType = params.maxPriceType or "exalted",
		}
		qg:StartQuery(slotCtrl, options)
		while qg.calcContext and qg.calcContext.co do
			qg:OnFrame()
		end
		closeAllPopups()
	end)

	-- Clean up temporary probe item if we inserted one
	if tempItemId then
		slotCtrl.selItemId = origSelItemId
		build.itemsTab.items[tempItemId] = nil
		closeAllPopups()
	end

	if not okRun then
		error("TradeQueryGenerator failed: " .. tostring(runErr), 0)
	end

	local statTextMap = getTradeStatTextMap()
	local topN = tonumber(params.topN) or 35
	local weightsOut = {}
	local rawWeights = (qg and qg.modWeights) or {}
	for i = 1, math.min(topN, #rawWeights) do
		local w = rawWeights[i]
		local signedWeight = (w.invert == true) and (-1 * w.weight) or w.weight
		weightsOut[#weightsOut + 1] = {
			rank = i,
			tradeModId = w.tradeModId,
			text = statTextMap[w.tradeModId] or w.tradeModId,
			weight = signedWeight,
			meanStatDiff = w.meanStatDiff,
		}
	end

	local tradePayload = capturedQuery and {
		query = capturedQuery,
		sort = { ["statgroup.0"] = "desc" },
	} or dkjson.null

	return {
		slot = slotName,
		targetSlot = targetSlot,
		itemCategory = qg and qg.calcContext and qg.calcContext.itemCategoryQueryStr or dkjson.null,
		totalWeightedMods = #rawWeights,
		elapsedMs = (os.clock() - t0) * 1000.0,
		warning = capturedErr or dkjson.null,
		weights = weightsOut,
		trade2Payload = tradePayload,
	}
end

-- ---- Batch RPC & Graceful Shutdown ----

methods.call_batch = function(params)
	if not params or type(params.calls) ~= "table" then
		error("params.calls (array of {method, params}) is required", 0)
	end
	local out = {}
	for i, call in ipairs(params.calls) do
		local mName = call and call.method
		local fn = mName and methods[mName]
		if not fn or mName == "call_batch" then
			out[i] = { ok = false, method = mName or dkjson.null, error = "Unknown or disallowed batch method: " .. tostring(mName) }
		else
			local okC, resOrErr = pcall(fn, call.params or {})
			if okC then
				out[i] = { ok = true, method = mName, result = resOrErr }
			else
				out[i] = { ok = false, method = mName, error = tostring(resOrErr) }
			end
		end
	end
	return { results = out }
end

local shouldExit = false
methods.shutdown = function()
	shouldExit = true
	return { ok = true }
end

-- Optional startup banner if POB2_READY_BANNER=1 is set
if os.getenv("POB2_READY_BANNER") == "1" then
	writeFrame({
		jsonrpc = "2.0",
		method = "bridge.ready",
		params = methods.ping(),
	})
end

-- ============================================================================
-- Main JSON-RPC 2.0 stdin/stdout Loop
-- ============================================================================

while not shouldExit do
	local line = io.stdin:read("*l")
	if line == nil then
		break
	end
	local trimmed = line:match("^%s*(.-)%s*$")
	if trimmed ~= "" then
		local req, _, decErr = dkjson.decode(trimmed)
		if type(req) ~= "table" then
			writeFrame({
				jsonrpc = "2.0",
				id = dkjson.null,
				error = { code = -32700, message = "Parse error: " .. tostring(decErr or "invalid JSON") },
			})
		else
			local reqId = req.id
			if reqId == nil then reqId = dkjson.null end
			local methodName = req.method
			local fn = type(methodName) == "string" and methods[methodName] or nil
			if not fn then
				writeFrame({
					jsonrpc = "2.0",
					id = reqId,
					error = { code = -32601, message = "Method not found: " .. tostring(methodName) },
				})
			else
				local okCall, resOrErr = pcall(fn, req.params or {})
				if okCall then
					writeFrame({
						jsonrpc = "2.0",
						id = reqId,
						result = resOrErr,
					})
				else
					writeFrame({
						jsonrpc = "2.0",
						id = reqId,
						error = { code = -32000, message = tostring(resOrErr) },
					})
				end
			end
		end
	end
end
