-- SPDX-License-Identifier: GPL-3.0-only
-- Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
-- pob2_bridge.lua
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

-- Tune LuaJIT GC aggressively BEFORE loading PoB2 TreeData / ModCache so dead startup
-- parse tables are collected incrementally instead of inflating the OS heap watermark.
collectgarbage("setpause", 110)
collectgarbage("setstepmul", 300)

local ffi = require("ffi")

if ffi.os == "Windows" then
	ffi.cdef[[
int _chdir(const char *path);
void *OpenProcess(uint32_t dwDesiredAccess, int bInheritHandle, uint32_t dwProcessId);
uint32_t WaitForSingleObject(void *hHandle, uint32_t dwMilliseconds);
int CloseHandle(void *hObject);
]]
else
	ffi.cdef[[
int chdir(const char *path);
int getppid(void);

struct pob2_pollfd {
	int   fd;
	short events;
	short revents;
};
int poll(struct pob2_pollfd *fds, unsigned int nfds, int timeout);
long read(int fd, void *buf, size_t count);
]]
end

ffi.cdef[[
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

local checkParentAlive
if ffi.os == "Windows" then
	local parentHandle = nil
	local envPpid = tonumber(os.getenv("POE2BRAIN_PARENT_PID") or "")
	if envPpid and envPpid > 0 then
		local okK, k32 = pcall(ffi.load, "kernel32")
		if okK and k32 then
			local h = k32.OpenProcess(0x00100000, 0, envPpid) -- SYNCHRONIZE
			if h ~= nil then
				parentHandle = { k32 = k32, handle = h }
			end
		end
	end
	checkParentAlive = function()
		if parentHandle ~= nil then
			-- WAIT_TIMEOUT = 0x00000102; any other return means parent exited
			if parentHandle.k32.WaitForSingleObject(parentHandle.handle, 0) ~= 0x00000102 then
				os.exit(0)
			end
		end
	end
else
	local INITIAL_PPID = ffi.C.getppid()
	checkParentAlive = function()
		local curPpid = ffi.C.getppid()
		if curPpid ~= INITIAL_PPID or curPpid <= 1 then
			os.exit(0)
		end
	end
end

-- Allow passing PoB2 src directory via POB2_SRC_DIR or arg[1] if CWD is elsewhere.
local pobSrcDir = os.getenv("POB2_SRC_DIR") or (arg and arg[1])
if pobSrcDir and pobSrcDir ~= "" then
	local rc = (ffi.os == "Windows") and ffi.C._chdir(pobSrcDir) or ffi.C.chdir(pobSrcDir)
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
	if ffi.os == "Windows" then
		candidates[#candidates + 1] = "../runtime/zlib1.dll"
		candidates[#candidates + 1] = "zlib1"
		candidates[#candidates + 1] = "../runtime/z.dll"
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

	checkParentAlive()
	dofile("Launch.lua")
	__mainObject__.continuousIntegrationMode = false
	runCallback("OnInit")
	runCallback("OnFrame")
	if __mainObject__.promptMsg then
		error("PoB2 startup promptMsg: " .. tostring(__mainObject__.promptMsg), 0)
	end
	checkParentAlive()
end)

if not bootOk then
	stderr:write("[pob2_bridge] Boot failed: " .. tostring(bootErr) .. "\n")
	os.exit(1)
end

local bootSeconds = os.clock() - bootT0
local mainObj = __mainObject__.main
mainObj.disableDevAutoSave = true

-- Cancel PoB2's background GUI dropdown coroutine (`LoadItems`) and release unused
-- Unique/Rare GUI template catalogs so subsequent OnFrame calls never parse ~1,500
-- Item instances into memory.
if mainObj.onFrameFuncs then
	mainObj.onFrameFuncs["LoadItems"] = nil
	mainObj.onFrameFuncs["FirstFrame"] = nil
end
if mainObj.uniqueDB then
	if mainObj.uniqueDB.list then wipeTable(mainObj.uniqueDB.list) end
	mainObj.uniqueDB.loading = nil
end
if mainObj.rareDB then
	if mainObj.rareDB.list then wipeTable(mainObj.rareDB.list) end
	mainObj.rareDB.loading = nil
end
if data then
	data.uniques = {}
	data.rares = {}
end
collectgarbage("collect")
collectgarbage("collect")

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
configOptionsList = nil

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
-- Helper functions for refreshing & loading builds (strictly 1 active Build!)
-- ============================================================================

local function closeAllPopups()
	while mainObj.popups and mainObj.popups[1] do
		mainObj:ClosePopup()
	end
end

local function packXml(xmlText)
	local raw = tostring(xmlText or "")
	if zlib and #raw > 0 then
		local ok, compressed = pcall(realDeflate, raw, false)
		if ok and compressed then
			return { z = compressed, len = #raw }
		end
	end
	return { raw = raw, len = #raw }
end

local function unpackXml(packed)
	if type(packed) == "string" then
		return packed
	end
	if type(packed) ~= "table" then
		return ""
	end
	if packed.raw ~= nil then
		return packed.raw
	end
	if packed.z ~= nil then
		return realInflate(packed.z)
	end
	return ""
end

local function packedXmlBytes(packed)
	if type(packed) == "table" and type(packed.len) == "number" then
		return packed.len
	end
	if type(packed) == "string" then
		return #packed
	end
	return 0
end

local function teardownActiveBuild()
	closeAllPopups()
	if GlobalCache and GlobalCache.cachedData then
		wipeGlobalCache()
	end
	if mainObj.onFrameFuncs then
		wipeTable(mainObj.onFrameFuncs)
	end
	local b = mainObj.modes["BUILD"]
	if b then
		b.unsaved = false
		b.abortSave = true
		pcall(function()
			if b.Shutdown then b:Shutdown() end
		end)
		mainObj.mode = nil
		if b.controls then wipeTable(b.controls) end
		if b.xmlSectionList then wipeTable(b.xmlSectionList) end
		b.calcsTab = nil
		b.itemsTab = nil
		b.treeTab = nil
		b.skillsTab = nil
		b.configTab = nil
		b.importTab = nil
		b.partyTab = nil
		b.compareTab = nil
		b.notesTab = nil
		b.spec = nil
		b.savers = nil
		b.legacyLoaders = nil
		b.sidebarBreakdownData = nil
		b.breakdownInputs = nil
	end
	build = nil
	collectgarbage("collect")
end

local function refreshBuild()
	build = mainObj.modes["BUILD"]
	-- Keep viewMode off "TREE" so OnFrame skips treeTab:Draw and Calcs:getMiscCalculator
	-- respects useFullDPS == false instead of forcing calcFullDPS on every node pass!
	build.viewMode = "NOTES"
	build.modFlag = true
	build.buildFlag = true
	runCallback("OnFrame")
	closeAllPopups()
end

local function resetEngineToNewBuild(buildName)
	teardownActiveBuild()
	mainObj:SetMode("BUILD", false, buildName or "Unnamed Build")
	runCallback("OnFrame")
	closeAllPopups()
	build = mainObj.modes["BUILD"]
	build.viewMode = "NOTES"
	collectgarbage("collect")
end

local function loadEngineFromXML(xmlText, buildName)
	teardownActiveBuild()
	mainObj:SetMode("BUILD", false, buildName or "Imported Build", xmlText)
	runCallback("OnFrame")
	closeAllPopups()
	build = mainObj.modes["BUILD"]
	build.viewMode = "NOTES"
	collectgarbage("collect")
end

local function saveEngineToXML()
	build = mainObj.modes["BUILD"]
	return build:SaveDB("slot-snapshot")
end

-- ============================================================================
-- Capability 1: Named Build Slots + Clone / Reset / Undo / Redo
-- ============================================================================

local MAX_HISTORY = 5
local slots = {}
local activeSlot = "main"

resetEngineToNewBuild("main")
slots["main"] = {
	name = "main",
	packed = packXml(saveEngineToXML()),
	rev = 1,
	undoStack = {},
	redoStack = {},
}

local function setSlotXml(slotEntry, xmlText)
	slotEntry.packed = packXml(xmlText)
end

local function getSlotXml(slotEntry)
	return unpackXml(slotEntry.packed)
end

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
	-- Save outgoing active slot as compact compressed XML before tearing down active Build
	if activeSlot and slots[activeSlot] then
		setSlotXml(slots[activeSlot], saveEngineToXML())
	end
	if not slots[slotName] then
		if createIfMissing == false then
			error("Unknown slot '" .. tostring(slotName) .. "'", 0)
		end
		resetEngineToNewBuild(slotName)
		slots[slotName] = {
			name = slotName,
			packed = packXml(saveEngineToXML()),
			rev = 1,
			undoStack = {},
			redoStack = {},
		}
		activeSlot = slotName
		return slots[slotName]
	end
	local targetXml = getSlotXml(slots[slotName])
	loadEngineFromXML(targetXml, slotName)
	targetXml = nil
	activeSlot = slotName
	collectgarbage("collect")
	return slots[slotName]
end

local function pushUndoSnapshot(slotEntry)
	local snap = packXml(saveEngineToXML())
	table.insert(slotEntry.undoStack, snap)
	if #slotEntry.undoStack > MAX_HISTORY then
		table.remove(slotEntry.undoStack, 1)
	end
	slotEntry.redoStack = {}
end

local function finalizeSlotMutation(slotEntry)
	refreshBuild()
	slotEntry.rev = (slotEntry.rev or 1) + 1
	setSlotXml(slotEntry, saveEngineToXML())
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
	"ManaCost", "ManaPerSecondCost", "ManaPercentCost", "ManaPercentPerSecondCost", "ManaCostWarning",
	"LifeCost", "LifePerSecondCost", "ESCost", "ESPerSecondCost", "RageCost",
	-- Pools & Sustain
	"TotalEHP", "Life", "LifeUnreserved", "LifeUnreservedPercent", "LifeReserved", "LifeReservedPercent",
	"LifeRegenRecovery", "LifeLeechGainRate",
	"EnergyShield", "EnergyShieldRecharge", "EnergyShieldRechargeDelay", "EnergyShieldRegenRecovery",
	"Mana", "ManaUnreserved", "ManaUnreservedPercent", "ManaReserved", "ManaReservedPercent",
	"ManaRegenRecovery", "Ward",
	"ChaosInoculation", "EnergyShieldProtectsMana",
	-- Spirit
	"Spirit", "SpiritReserved", "SpiritUnreserved", "SpiritReservedPercent", "SpiritUnreservedPercent",
	-- Resistances
	"FireResist", "FireResistOverCap", "ColdResist", "ColdResistOverCap",
	"LightningResist", "LightningResistOverCap", "ChaosResist", "ChaosResistOverCap",
	-- Mitigation & Avoidance (PoE2 Armour / Evasion / Deflection / Block)
	"Armour", "PhysicalDamageReduction",
	"Evasion", "EvadeChance", "MeleeEvadeChance", "ProjectileEvadeChance", "SpellEvadeChance",
	"DeflectionRating", "DeflectChance", "DeflectEffect",
	"BlockChance", "BlockChanceMax", "BlockChanceOverCap", "EffectiveBlockChance",
	"SpellBlockChance", "SpellBlockChanceMax", "EffectiveSpellBlockChance",
	"ProjectileBlockChance", "EffectiveProjectileBlockChance",
	-- Max Hits & Thresholds
	"PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken",
	"LightningMaximumHitTaken", "ChaosMaximumHitTaken", "SecondMinimalMaximumHitTaken",
	"StunThreshold", "AilmentThreshold",
	-- Attributes & Mobility
	"Str", "Dex", "Int", "TotalAttr", "ReqStr", "ReqDex", "ReqInt",
	"ReqStrString", "ReqDexString", "ReqIntString",
	"MovementSpeedMod", "EffectiveMovementSpeedMod", "MovementSpeedWhileUsingSkill",
	"EffectiveLootRarityMod",
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
	-- Reconcile SpiritReserved when over-reserved (PoB2 caps SpiritReserved at Spirit while SpiritUnreserved goes negative)
	if res.SpiritUnreserved < 0 and res.Spirit >= 0 then
		res.SpiritReserved = math.max(res.SpiritReserved, res.Spirit - res.SpiritUnreserved)
	end
	-- Effective primary DPS convenience field (FullDPS if > 0 else CombinedDPS/TotalDPS, with Minion fallback)
	local fullDps = safeNum(out.FullDPS, 0)
	local combinedDps = safeNum(out.CombinedDPS, 0)
	local totalDps = safeNum(out.TotalDPS, 0)
	local playerEff = (fullDps > 0 and fullDps) or (combinedDps > 0 and combinedDps) or totalDps

	local minionEff = 0
	if type(out.Minion) == "table" then
		local mFull = safeNum(out.Minion.FullDPS, 0)
		local mComb = safeNum(out.Minion.CombinedDPS, 0)
		local mTot = safeNum(out.Minion.TotalDPS, 0)
		minionEff = (mFull > 0 and mFull) or (mComb > 0 and mComb) or mTot
		res.Minion = {
			TotalDPS = mTot,
			FullDPS = mFull,
			CombinedDPS = mComb,
			Life = safeNum(out.Minion.Life, 0),
			EnergyShield = safeNum(out.Minion.EnergyShield, 0),
			TotalEHP = safeNum(out.Minion.TotalEHP, 0),
		}
	end
	res.EffectiveDPS = (fullDps > 0 and fullDps) or math.max(playerEff, minionEff)

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
	local lvl = math.max(1, math.min(100, math.floor(tonumber(build.characterLevel) or 1)))
	local respecTbl = (data and data.goldRespecPrices) or (build.data and build.data.goldRespecPrices) or {}
	local goldPerPt = tonumber(respecTbl[lvl]) or 0
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
		goldRespecPerPoint = goldPerPt,
		ascendancyRespecPerPoint = goldPerPt * 5,
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
			local runesList = {}
			if type(item.runes) == "table" then
				for _, rName in ipairs(item.runes) do
					if type(rName) == "string" and rName ~= "" and rName ~= "None" then
						runesList[#runesList + 1] = rName
					end
				end
			end
			local entry = {
				slot = slotName,
				itemId = itemId,
				name = item.name or "",
				title = item.title or "",
				baseName = item.baseName or "",
				type = item.type or "",
				rarity = item.rarity or "NORMAL",
				active = slotCtrl.active == true,
				corrupted = item.corrupted == true,
				runeSockets = item.itemSocketCount or 0,
				runes = runesList,
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
	local ct = build and build.configTab
	if ct and ct.defaultState and ct.defaultState[varName] ~= nil and val == ct.defaultState[varName] then
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
		-- Use PoB2's own ConfigTab visibility predicate (`ifNode`/`ifCond`/`ifMult`/`ifSkill`/`ifFlag`/`ifMod`)
		-- to skip stale/exported config keys that have zero active modifiers on this build!
		local varCtrl = ct and ct.varControls and ct.varControls[varName]
		if varCtrl and type(varCtrl.IsShown) == "function" then
			local showAllCtrl = ct.controls and ct.controls.showAllConfigurations
			local savedShowAll = showAllCtrl and showAllCtrl.state
			if showAllCtrl then showAllCtrl.state = false end
			local okShown, isShown = pcall(varCtrl.IsShown, varCtrl)
			if showAllCtrl then showAllCtrl.state = savedShowAll end
			if okShown and not isShown then
				return false
			end
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
		setSlotXml(slots[activeSlot], saveEngineToXML())
	end
	local out = {}
	for name, entry in pairs(slots) do
		out[#out + 1] = {
			slot = name,
			rev = entry.rev or 1,
			isActive = (name == activeSlot),
			undoDepth = #(entry.undoStack or {}),
			redoDepth = #(entry.redoStack or {}),
			xmlBytes = packedXmlBytes(entry.packed),
		}
	end
	table.sort(out, function(a, b) return a.slot < b.slot end)
	return { activeSlot = activeSlot, slots = out }
end

methods.snapshot_all_slots = function()
	if activeSlot and slots[activeSlot] then
		setSlotXml(slots[activeSlot], saveEngineToXML())
	end
	local snapSlots = {}
	for name, entry in pairs(slots) do
		local undoXmls = {}
		for i, u in ipairs(entry.undoStack or {}) do
			undoXmls[i] = unpackXml(u)
		end
		local redoXmls = {}
		for i, r in ipairs(entry.redoStack or {}) do
			redoXmls[i] = unpackXml(r)
		end
		snapSlots[name] = {
			xml = getSlotXml(entry),
			rev = entry.rev or 1,
			undoStack = undoXmls,
			redoStack = redoXmls,
		}
	end
	return {
		activeSlot = activeSlot,
		slots = jsonObj(snapSlots),
	}
end

methods.restore_all_slots = function(params)
	if not params or type(params.slots) ~= "table" then
		error("params.slots (map of slot -> {xml, rev}) is required", 0)
	end
	local newSlots = {}
	for name, rawEntry in pairs(params.slots) do
		local sName = tostring(name)
		if type(rawEntry) == "table" and type(rawEntry.xml) == "string" and rawEntry.xml ~= "" then
			local undoPacked = {}
			if type(rawEntry.undoStack) == "table" then
				local startIdx = math.max(1, #rawEntry.undoStack - MAX_HISTORY + 1)
				for i = startIdx, #rawEntry.undoStack do
					undoPacked[#undoPacked + 1] = packXml(rawEntry.undoStack[i])
				end
			end
			local redoPacked = {}
			if type(rawEntry.redoStack) == "table" then
				local startIdx = math.max(1, #rawEntry.redoStack - MAX_HISTORY + 1)
				for i = startIdx, #rawEntry.redoStack do
					redoPacked[#redoPacked + 1] = packXml(rawEntry.redoStack[i])
				end
			end
			newSlots[sName] = {
				name = sName,
				packed = packXml(rawEntry.xml),
				rev = tonumber(rawEntry.rev) or 1,
				undoStack = undoPacked,
				redoStack = redoPacked,
			}
		end
	end
	if not newSlots["main"] then
		resetEngineToNewBuild("main")
		newSlots["main"] = {
			name = "main",
			packed = packXml(saveEngineToXML()),
			rev = 1,
			undoStack = {},
			redoStack = {},
		}
	end
	slots = newSlots
	local targetActive = (params.activeSlot and slots[tostring(params.activeSlot)]) and tostring(params.activeSlot) or "main"
	activeSlot = targetActive
	local activeXml = getSlotXml(slots[activeSlot])
	loadEngineFromXML(activeXml, activeSlot)
	activeXml = nil
	collectgarbage("collect")
	return { ok = true, activeSlot = activeSlot }
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
		setSlotXml(slots[srcName], saveEngineToXML())
	end
	slots[dstName] = {
		name = dstName,
		packed = slots[srcName].packed,
		rev = 1,
		undoStack = {},
		redoStack = {},
	}
	if dstName == activeSlot and srcName ~= dstName then
		local dstXml = getSlotXml(slots[dstName])
		loadEngineFromXML(dstXml, dstName)
		dstXml = nil
	elseif params.activate then
		if srcName == activeSlot then
			activeSlot = dstName
			if build then build.buildName = dstName end
		else
			ensureSlot(dstName, false)
		end
	end
	return {
		ok = true,
		source = srcName,
		target = dstName,
		activeSlot = activeSlot,
		xmlBytes = packedXmlBytes(slots[dstName].packed),
	}
end

methods.delete_slot = function(params)
	local targetName = normalizeSlotName(params, "slot")
	local existed = slots[targetName] ~= nil
	slots[targetName] = nil
	if activeSlot == targetName then
		if slots["main"] then
			ensureSlot("main", false)
		else
			local fallback = next(slots)
			if fallback then
				ensureSlot(fallback, false)
			else
				activeSlot = nil
			end
		end
	end
	return { ok = true, deleted = existed, slot = targetName, activeSlot = activeSlot }
end

methods.copy_main_skill_from_slot = function(params)
	if not params or not params.source then
		error("params.source is required", 0)
	end
	local srcName = normalizeSlotName(params, "source")
	local dstName = normalizeSlotName(params, "target")
	ensureSlot(srcName, false)
	local srcIdx = tonumber(build.mainSocketGroup) or 1
	local srcGroup = build.skillsTab and build.skillsTab.socketGroupList[srcIdx]
	if not srcGroup or type(srcGroup.gemList) ~= "table" or #srcGroup.gemList == 0 then
		error("Source slot '" .. srcName .. "' has no main socket group gems", 0)
	end
	local copiedGems = {}
	local srcLabel = srcGroup.label or ""
	for _, g in ipairs(srcGroup.gemList) do
		copiedGems[#copiedGems + 1] = {
			nameSpec = g.nameSpec or (g.gemData and g.gemData.name) or "",
			gemId = g.gemId,
			skillId = g.skillId,
			level = tonumber(g.level) or 20,
			quality = tonumber(g.quality) or 20,
			qualityId = g.qualityId or "Default",
			enabled = g.enabled ~= false,
			count = tonumber(g.count) or 1,
		}
	end

	local dstEntry = ensureSlot(dstName, true)
	pushUndoSnapshot(dstEntry)
	local dstIdx = tonumber(build.mainSocketGroup) or 1
	local dstGroup = build.skillsTab.socketGroupList[dstIdx]
	if not dstGroup then
		dstIdx = 1
		dstGroup = {
			label = srcLabel,
			enabled = true,
			includeInFullDPS = true,
			gemList = {},
			mainActiveSkill = 1,
			mainActiveSkillCalcs = 1,
		}
		build.skillsTab.socketGroupList[1] = dstGroup
		build.mainSocketGroup = 1
	end
	dstGroup.gemList = {}
	for _, cg in ipairs(copiedGems) do
		local resolved = findGemDataByIdentifier(cg.gemId) or findGemDataByIdentifier(cg.nameSpec)
		dstGroup.gemList[#dstGroup.gemList + 1] = {
			nameSpec = (resolved and resolved.name) or cg.nameSpec,
			gemId = (resolved and resolved.id) or cg.gemId,
			gemData = resolved,
			skillId = cg.skillId or (resolved and resolved.grantedEffectId),
			level = cg.level,
			quality = cg.quality,
			qualityId = cg.qualityId,
			enabled = cg.enabled,
			enableGlobal1 = true,
			enableGlobal2 = true,
			count = cg.count,
		}
	end
	dstGroup.enabled = true
	dstGroup.includeInFullDPS = true
	build.skillsTab:ProcessSocketGroup(dstGroup)
	finalizeSlotMutation(dstEntry)
	return {
		ok = true,
		source = srcName,
		target = dstName,
		groupIndex = dstIdx,
		stats = extractStatsTable(),
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
		setSlotXml(slots[activeSlot], saveEngineToXML())
	end
	local prevEntry = slots[slotName]
	local undoStack = prevEntry and prevEntry.undoStack or {}
	if prevEntry and prevEntry.packed then
		local curPacked = (activeSlot == slotName) and packXml(saveEngineToXML()) or prevEntry.packed
		table.insert(undoStack, curPacked)
		if #undoStack > MAX_HISTORY then table.remove(undoStack, 1) end
	end
	resetEngineToNewBuild((params and params.name) or slotName)
	activeSlot = slotName
	applyCharacterOptions(params)
	refreshBuild()
	slots[slotName] = {
		name = slotName,
		packed = packXml(saveEngineToXML()),
		rev = (prevEntry and (prevEntry.rev + 1)) or 1,
		undoStack = undoStack,
		redoStack = {},
	}
	collectgarbage("collect")
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
	slots[slotName] = nil
	if activeSlot == slotName then
		local mainXml = getSlotXml(slots["main"])
		loadEngineFromXML(mainXml, "main")
		mainXml = nil
		activeSlot = "main"
	end
	collectgarbage("collect")
	return { ok = true, deleted = slotName, activeSlot = activeSlot }
end

methods.undo = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local entry = ensureSlot(slotName, false)
	if #entry.undoStack == 0 then
		error("Nothing to undo in slot '" .. slotName .. "'", 0)
	end
	table.insert(entry.redoStack, packXml(saveEngineToXML()))
	if #entry.redoStack > MAX_HISTORY then
		table.remove(entry.redoStack, 1)
	end
	local prevPacked = table.remove(entry.undoStack)
	local prevXml = unpackXml(prevPacked)
	loadEngineFromXML(prevXml, slotName)
	prevXml = nil
	entry.packed = prevPacked
	entry.rev = entry.rev + 1
	collectgarbage("collect")
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
	table.insert(entry.undoStack, packXml(saveEngineToXML()))
	if #entry.undoStack > MAX_HISTORY then
		table.remove(entry.undoStack, 1)
	end
	local nextPacked = table.remove(entry.redoStack)
	local nextXml = unpackXml(nextPacked)
	loadEngineFromXML(nextXml, slotName)
	nextXml = nil
	entry.packed = nextPacked
	entry.rev = entry.rev + 1
	collectgarbage("collect")
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

local function autoResolveMainSocketGroupIfZeroDps()
	if not build or not build.skillsTab or not build.calcsTab then
		return
	end
	local curStats = extractStatsTable()
	if (curStats.EffectiveDPS or 0) > 0 then
		return
	end
	local groups = build.skillsTab.socketGroupList or {}
	if #groups <= 1 then
		return
	end
	local okCalc, calcFunc = pcall(function()
		return build.calcsTab:GetMiscCalculator()
	end)
	if not okCalc or type(calcFunc) ~= "function" then
		return
	end
	local bestIdx = nil
	local bestDps = 0
	for idx, grp in ipairs(groups) do
		if grp and grp.enabled ~= false and grp.source ~= "Explode" and type(grp.gemList) == "table" and #grp.gemList > 0 then
			local okRun, candOut = pcall(calcFunc, { mainSocketGroup = idx })
			if okRun and type(candOut) == "table" then
				local st = extractStatsTable(candOut)
				local d = tonumber(st.EffectiveDPS) or 0
				if d > bestDps then
					bestDps = d
					bestIdx = idx
				end
			end
		end
	end
	if bestIdx and bestIdx ~= build.mainSocketGroup then
		build.mainSocketGroup = bestIdx
		refreshBuild()
	end
end

methods.load_build_xml = function(params)
	if not params or type(params.xml) ~= "string" or params.xml == "" then
		error("params.xml (non-empty XML string) is required", 0)
	end
	if not params.xml:find("<PathOfBuilding", 1, true) then
		error("Invalid PoB2 XML: missing <PathOfBuilding> root element", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	if activeSlot ~= slotName and slots[activeSlot] then
		setSlotXml(slots[activeSlot], saveEngineToXML())
	end
	local entry = slots[slotName]
	if entry then
		local prevPacked = (activeSlot == slotName) and packXml(saveEngineToXML()) or entry.packed
		if prevPacked then
			table.insert(entry.undoStack, prevPacked)
			if #entry.undoStack > MAX_HISTORY then
				table.remove(entry.undoStack, 1)
			end
		end
		entry.redoStack = {}
		entry.rev = (entry.rev or 1) + 1
	else
		entry = {
			name = slotName,
			packed = nil,
			rev = 1,
			undoStack = {},
			redoStack = {},
		}
		slots[slotName] = entry
	end
	loadEngineFromXML(params.xml, params.name or slotName)
	autoResolveMainSocketGroupIfZeroDps()
	activeSlot = slotName
	setSlotXml(entry, saveEngineToXML())
	collectgarbage("collect")
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
			if k == "customMods" then
				local txt = (v == dkjson.null) and "" or tostring(v)
				setActiveCustomModsText(txt)
			elseif v == dkjson.null then
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
	local activeSkillName = (activeSkill and activeSkill.activeEffect and activeSkill.activeEffect.grantedEffect and activeSkill.activeEffect.grantedEffect.name)
		or group.label
		or ("Group " .. tostring(gIdx))
	if not activeSkill then
		return { slot = slotName, groupIndex = gIdx, activeSkillName = activeSkillName, socketedGems = {}, supports = {} }
	end

	local tierFilter = params and params.tier ~= nil and tonumber(params.tier) or nil
	local maxTierFilter = params and params.maxTier ~= nil and tonumber(params.maxTier) or nil
	local includeLineage = (params == nil or params.includeLineage ~= false)
	local excludeLineageConflicts = (params and params.excludeLineageConflicts == true)
	local rankByDps = (params and params.rankByDps == true)
	local topN = params and params.topN ~= nil and tonumber(params.topN) or nil
	local explicitGemSlot = params and params.gemSlot ~= nil and tonumber(params.gemSlot) or nil
	local replaceGemQuery = (params and type(params.replaceGem) == "string" and params.replaceGem ~= "") and params.replaceGem:lower() or nil
	local bestSwap = (params and params.bestSwap == true)
	local warnings = {}

	-- Scan ALL socket groups on character to enforce PoE2 Lineage Support max_copies_per_char = 1
	local lineageElsewhereById = {}
	for otherIdx, otherGrp in ipairs(build.skillsTab.socketGroupList or {}) do
		if otherIdx ~= gIdx and otherGrp.enabled ~= false then
			local otherAct = otherGrp.displaySkillList and otherGrp.displaySkillList[otherGrp.mainActiveSkill or 1]
			local otherLabel = (otherAct and otherAct.activeEffect and otherAct.activeEffect.grantedEffect and otherAct.activeEffect.grantedEffect.name)
				or (otherGrp.label and otherGrp.label ~= "" and otherGrp.label)
				or ("Group " .. tostring(otherIdx))
			for _, g in ipairs(otherGrp.gemList or {}) do
				local gd = g.gemData
				if g.enabled ~= false and gd and gd.id and gd.tags and gd.tags.lineage == true then
					lineageElsewhereById[gd.id] = otherLabel
				end
			end
		end
	end

	local equippedById = {}
	local equippedIdxByFamily = {}
	local socketedGems = {}
	local resolvedReplaceIdx = nil
	local supportOrdinal = 0
	for idx, g in ipairs(group.gemList or {}) do
		local gd = g.gemData
		local isSup = (gd and gd.grantedEffect and gd.grantedEffect.support) and true or false
		local gName = (gd and gd.name) or g.nameSpec or ("Gem " .. tostring(idx))
		if isSup then
			supportOrdinal = supportOrdinal + 1
			if explicitGemSlot and supportOrdinal == explicitGemSlot then
				resolvedReplaceIdx = idx
			end
			if replaceGemQuery and not resolvedReplaceIdx and gName:lower():find(replaceGemQuery, 1, true) then
				resolvedReplaceIdx = idx
			end
		end
		socketedGems[#socketedGems + 1] = {
			gemIndex = idx,
			supportSlot = isSup and supportOrdinal or dkjson.null,
			name = gName,
			isSupport = isSup,
			supportTier = (gd and tonumber(gd.Tier)) or 0,
			isLineage = (gd and gd.tags and gd.tags.lineage == true) or false,
			enabled = g.enabled ~= false,
		}
		if gd then
			if gd.id then equippedById[gd.id] = idx end
			if gd.gemFamily and isSup then
				equippedIdxByFamily[gd.gemFamily] = idx
			end
		end
	end

	if explicitGemSlot and not resolvedReplaceIdx then
		error("Support slot #" .. tostring(explicitGemSlot) .. " does not exist in socket group " .. tostring(gIdx) .. " (found " .. tostring(supportOrdinal) .. " supports)", 0)
	end

	local supports = {}
	for gemId, gemData in pairs(build.data.gems or {}) do
		if gemData.grantedEffect and gemData.grantedEffect.support then
			local isLineage = (gemData.tags and gemData.tags.lineage == true) or false
			local sTier = tonumber(gemData.Tier) or 0
			local conflictSkill = isLineage and lineageElsewhereById[gemId] or nil
			local keep = true
			if not includeLineage and isLineage then
				keep = false
			elseif excludeLineageConflicts and conflictSkill ~= nil then
				keep = false
			elseif tierFilter ~= nil then
				keep = (sTier == tierFilter) or (includeLineage and isLineage)
			elseif maxTierFilter ~= nil then
				keep = (sTier >= 1 and sTier <= maxTierFilter) or (includeLineage and isLineage)
			end
			if keep then
				local okS, canSup = pcall(calcLib.canGrantedEffectSupportActiveSkill, gemData.grantedEffect, activeSkill)
				if okS and canSup then
					supports[#supports + 1] = {
						_gemData = gemData,
						gemId = gemId,
						gameId = gemData.gameId or dkjson.null,
						name = gemData.name or gemId,
						supportTier = sTier,
						isLineage = isLineage,
						maxCopiesPerChar = isLineage and 1 or dkjson.null,
						lineageConflictSkill = conflictSkill or dkjson.null,
						gemFamily = gemData.gemFamily or dkjson.null,
						reqStr = tonumber(gemData.reqStr) or 0,
						reqDex = tonumber(gemData.reqDex) or 0,
						reqInt = tonumber(gemData.reqInt) or 0,
						alreadySocketed = equippedById[gemId] ~= nil,
					}
				end
			end
		end
	end

	local baseDpsOut = 0
	local baseManaCost = 0
	local baseSpiritUnres = 0
	if rankByDps and #supports > 0 then
		local calcFunc, calcBase = build.calcsTab:GetMiscCalculator()
		local override = (gIdx ~= (build.mainSocketGroup or 1)) and { mainSocketGroup = gIdx } or nil
		local baseOut = override and calcFunc(override, false) or calcBase
		local baseStats = extractStatsTable(baseOut)
		local useFull = (override == nil) and (safeNum(baseStats.FullDPS, 0) > 0)
		local baseDps = useFull and safeNum(baseStats.FullDPS, 0) or safeNum(baseStats.EffectiveDPS, 0)
		baseDpsOut = baseDps
		baseManaCost = safeNum(baseStats.ManaCost, 0)
		baseSpiritUnres = safeNum(baseStats.SpiritUnreserved, 0)
		local fastOpts = { nodeAlloc = true, requirementsItems = true, skipEHP = true }

		local gemList = group.gemList
		local savedDisplayGemList = group.displayGemList
		local appendIdx = #gemList + 1

		-- If skill already has 5/5 supports and bestSwap is requested (with no explicit target),
		-- find the weakest currently-socketed support (highest remaining DPS when removed) so
		-- every candidate is evaluated as a legal 6-link swap.
		if bestSwap and not resolvedReplaceIdx and supportOrdinal >= 5 then
			local weakestIdx = nil
			local weakestOrdinal = nil
			local weakestName = nil
			local highestRemDps = -1
			local supOrd = 0
			for idxG, g in ipairs(gemList) do
				local ggd = g.gemData
				local isSup = (ggd and ggd.grantedEffect and ggd.grantedEffect.support) and true or false
				if isSup then
					supOrd = supOrd + 1
					local wasEnabled = g.enabled
					local okRem, remOut = pcall(function()
						g.enabled = false
						return calcFunc(override, useFull, fastOpts)
					end)
					g.enabled = wasEnabled
					if okRem and remOut then
						local remStats = extractStatsTable(remOut)
						local remDps = useFull and safeNum(remStats.FullDPS, 0) or safeNum(remStats.EffectiveDPS, 0)
						if remDps > highestRemDps then
							highestRemDps = remDps
							weakestIdx = idxG
							weakestOrdinal = supOrd
							weakestName = (ggd and ggd.name) or g.nameSpec or ("Support #" .. tostring(supOrd))
						end
					end
				end
			end
			if weakestIdx then
				resolvedReplaceIdx = weakestIdx
				explicitGemSlot = weakestOrdinal
				warnings[#warnings + 1] = "Auto-selected weakest socketed support (#"
					.. tostring(weakestOrdinal) .. " " .. tostring(weakestName)
					.. ") for legal 6-link --best-swap replacement."
			end
		elseif supportOrdinal >= 5 and not resolvedReplaceIdx then
			warnings[#warnings + 1] = "Active skill already has " .. tostring(supportOrdinal)
				.. "/5 support sockets filled (PoE2 6-link max); unreplaced candidates simulate "
				.. "a hypothetical extra socket. Pass --replace-slot / --replace-gem or --best-swap "
				.. "for legal 6-link replacements."
		end

		for idxSup, entry in ipairs(supports) do
			if idxSup % 16 == 0 then checkParentAlive() end
			local gd = entry._gemData
			entry._gemData = nil
			if entry.alreadySocketed then
				entry.dpsBefore = baseDps
				entry.dpsAfter = baseDps
				entry.dpsDelta = 0
				entry.dpsPct = 0
				entry.replacesGem = dkjson.null
				entry.manaCostBefore = baseManaCost
				entry.manaCostAfter = baseManaCost
				entry.manaSustainableAfter = (safeNum(baseStats.ManaRegenRecovery, 0) >= safeNum(baseStats.ManaPerSecondCost, 0))
					and (safeNum(baseStats.ManaUnreserved, 0) >= baseManaCost)
				entry.spiritUnreservedBefore = baseSpiritUnres
				entry.spiritUnreservedAfter = baseSpiritUnres
				entry.spiritAlreadyOverflowing = (baseSpiritUnres < 0)
				entry.spiritOverflow = false
				entry.attributesMetAfter = (safeNum(baseStats.Str, 0) >= safeNum(baseStats.ReqStr, 0))
					and (safeNum(baseStats.Dex, 0) >= safeNum(baseStats.ReqDex, 0))
					and (safeNum(baseStats.Int, 0) >= safeNum(baseStats.ReqInt, 0))
			else
				local famIdx = (gd.gemFamily and equippedIdxByFamily[gd.gemFamily]) or nil
				local targetIdx = resolvedReplaceIdx or famIdx or appendIdx
				local oldSlot = gemList[targetIdx]
				local replacedName = (oldSlot and oldSlot.gemData and oldSlot.gemData.name) or dkjson.null
				local lvl = (build.skillsTab.ProcessGemLevel and build.skillsTab:ProcessGemLevel(gd)) or gd.naturalMaxLevel or 1
				local okCalc, out = pcall(function()
					gemList[targetIdx] = {
						level = lvl,
						quality = 0,
						count = 1,
						enabled = true,
						enableGlobal1 = true,
						enableGlobal2 = true,
						gemId = gd.id,
						nameSpec = gd.name,
						skillId = gd.grantedEffectId,
						gemData = gd,
					}
					return calcFunc(override, useFull, fastOpts)
				end)
				gemList[targetIdx] = oldSlot
				group.displayGemList = savedDisplayGemList

				if okCalc and out then
					local afterStats = extractStatsTable(out)
					local newDps = useFull and safeNum(afterStats.FullDPS, 0) or safeNum(afterStats.EffectiveDPS, 0)
					local delta = newDps - baseDps
					local mCost = safeNum(afterStats.ManaCost, 0)
					local mPerSec = safeNum(afterStats.ManaPerSecondCost, 0)
					local mRegen = safeNum(afterStats.ManaRegenRecovery, 0)
					local mUnres = safeNum(afterStats.ManaUnreserved, 0)
					local spUnres = safeNum(afterStats.SpiritUnreserved, 0)
					entry.dpsBefore = baseDps
					entry.dpsAfter = newDps
					entry.dpsDelta = delta
					entry.dpsPct = baseDps > 0 and (delta / baseDps * 100.0) or 0.0
					entry.replacesGem = replacedName
					entry.manaCostBefore = baseManaCost
					entry.manaCostAfter = mCost
					entry.manaPerSecondAfter = mPerSec
					entry.manaRegenAfter = mRegen
					entry.manaSustainableAfter = (mUnres >= mCost) and (mPerSec <= 0 or mRegen >= mPerSec * 0.8)
					entry.spiritUnreservedBefore = baseSpiritUnres
					entry.spiritUnreservedAfter = spUnres
					entry.spiritAlreadyOverflowing = (baseSpiritUnres < 0)
					entry.spiritOverflow = (spUnres < 0) and (spUnres < baseSpiritUnres)
					entry.attributesMetAfter = (safeNum(afterStats.Str, 0) >= safeNum(afterStats.ReqStr, 0))
						and (safeNum(afterStats.Dex, 0) >= safeNum(afterStats.ReqDex, 0))
						and (safeNum(afterStats.Int, 0) >= safeNum(afterStats.ReqInt, 0))
				else
					entry.dpsBefore = baseDps
					entry.dpsAfter = baseDps
					entry.dpsDelta = 0
					entry.dpsPct = 0
					entry.replacesGem = dkjson.null
					entry.manaCostBefore = baseManaCost
					entry.manaCostAfter = baseManaCost
					entry.manaSustainableAfter = true
					entry.spiritUnreservedBefore = baseSpiritUnres
					entry.spiritUnreservedAfter = baseSpiritUnres
					entry.spiritAlreadyOverflowing = (baseSpiritUnres < 0)
					entry.spiritOverflow = false
					entry.attributesMetAfter = true
				end
			end
		end
		calcFunc, calcBase = nil, nil
		collectgarbage("collect")
		table.sort(supports, function(a, b)
			local aBlocked = a.spiritOverflow == true
			local bBlocked = b.spiritOverflow == true
			if aBlocked ~= bBlocked then
				return not aBlocked
			end
			if math.abs((a.dpsDelta or 0) - (b.dpsDelta or 0)) > 1e-4 then
				return (a.dpsDelta or 0) > (b.dpsDelta or 0)
			end
			return a.name < b.name
		end)
	else
		for _, entry in ipairs(supports) do entry._gemData = nil end
		table.sort(supports, function(a, b) return a.name < b.name end)
	end

	local totalValidSupports = #supports
	if topN and topN > 0 and #supports > topN then
		local trimmed = {}
		for i = 1, topN do trimmed[i] = supports[i] end
		supports = trimmed
	end

	return {
		slot = slotName,
		groupIndex = gIdx,
		activeSkillName = activeSkillName,
		supportCountSocketed = supportOrdinal,
		supportsFull = (supportOrdinal >= 5),
		hypotheticalExtraSocket = (supportOrdinal >= 5 and resolvedReplaceIdx == nil),
		replacedSupportSlot = explicitGemSlot or dkjson.null,
		baseDps = baseDpsOut,
		baseManaCost = baseManaCost,
		baseSpiritUnreserved = baseSpiritUnres,
		socketedGems = socketedGems,
		totalValid = totalValidSupports,
		count = #supports,
		supports = supports,
		warnings = warnings,
	}
end

-- ---- Capability 4: Dewatering / PoB-Warrior Audit ----

local function evalCalculatorBuildStats()
	checkParentAlive()
	local env = build.calcsTab.calcs.buildOutput(build, "CALCULATOR")
	local out = env.player.output
	out.EnergyShieldProtectsMana = env.modDB:Flag(nil, "EnergyShieldProtectsMana")
	local manaCost = safeNum(out.ManaCost, 0)
	if manaCost > 0 then
		local pool = safeNum(out.ManaUnreserved, 0) + (out.EnergyShieldProtectsMana and safeNum(out.EnergyShield, 0) or 0)
		if pool < manaCost then
			out.ManaCostWarning = true
		end
	end
	return extractStatsTable(out)
end

methods.audit_build = function(params)
	local slotName = normalizeSlotName(params, "slot")
	local slotEntry = ensureSlot(slotName, false)
	local applyDewater = (params and params.apply) == true

	local keepSet = {}
	for _, k in ipairs((params and params.keep) or {}) do
		keepSet[tostring(k)] = true
	end
	local enforcePinnacle = (params == nil or params.pinnacleBoss ~= false)
	local origPacked = applyDewater and packXml(saveEngineToXML()) or nil

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

	local function resetStateTablesOnly()
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
	end

	local function restoreOriginalState(fullRefresh)
		resetStateTablesOnly()
		build.configTab:BuildModList()
		if fullRefresh then
			refreshBuild()
		end
	end

	local rawStats = extractStatsTable()
	local rawEffDps = rawStats.EffectiveDPS
	local rawEhp = rawStats.TotalEHP

	local auditOk, auditResult = pcall(function()
		-- Gather candidate unsustainable levers (filtered to active/non-default options on this build)
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

		-- D. Suspect ConfigOptions flags / overrides (only active/shown on this build!)
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

		-- Evaluate marginal impact of each active culprit via single CALCULATOR pass (no OnFrame!)
		local culprits = {}
		for _, act in ipairs(actions) do
			checkParentAlive()
			resetStateTablesOnly()
			act.apply()
			build.configTab:BuildModList()
			local stepStats = evalCalculatorBuildStats()
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
		checkParentAlive()
		resetStateTablesOnly()
		for _, act in ipairs(actions) do
			act.apply()
		end
		build.configTab:BuildModList()
		local sustainableStats
		if applyDewater then
			refreshBuild()
			sustainableStats = extractStatsTable()
		else
			sustainableStats = evalCalculatorBuildStats()
		end

		-- Evaluate alternate Weapon Set (Weapon Swap safety check) when swap weapons or passives exist
		local weaponSwap = nil
		local activeItemSet = build.itemsTab and build.itemsTab.activeItemSet
		if activeItemSet then
			local swap1 = build.itemsTab.slots["Weapon 1 Swap"]
			local swap2 = build.itemsTab.slots["Weapon 2 Swap"]
			local hasSwapItem = ((swap1 and (swap1.selItemId or 0) ~= 0) or (swap2 and (swap2.selItemId or 0) ~= 0))
			local ws1Used, ws2Used = 0, 0
			if build.spec and build.spec.CountAllocNodes then
				local okCnt, _, _, _, _, c1, c2 = pcall(function() return build.spec:CountAllocNodes() end)
				if okCnt then
					ws1Used = tonumber(c1) or 0
					ws2Used = tonumber(c2) or 0
				end
			end
			if hasSwapItem or ws1Used > 0 or ws2Used > 0 then
				local origUseSecond = activeItemSet.useSecondWeaponSet == true
				local okSwap, altStats = pcall(function()
					activeItemSet.useSecondWeaponSet = not origUseSecond
					if build.itemsTab.BuildModList then
						build.itemsTab:BuildModList()
					end
					return evalCalculatorBuildStats()
				end)
				activeItemSet.useSecondWeaponSet = origUseSecond
				if build.itemsTab.BuildModList then
					pcall(function() build.itemsTab:BuildModList() end)
				end
				if okSwap and type(altStats) == "table" then
					weaponSwap = {
						hasWeaponSwap = true,
						activeSet = origUseSecond and 2 or 1,
						alternateSet = origUseSecond and 1 or 2,
						weaponSet1Passives = ws1Used,
						weaponSet2Passives = ws2Used,
						alternateStats = altStats,
					}
				end
			end
		end

		return {
			sustainableStats = sustainableStats,
			weaponSwap = weaponSwap,
			culprits = culprits,
			checkedLevers = #actions,
		}
	end)

	if not auditOk then
		restoreOriginalState(false)
		collectgarbage("collect")
		error("audit_build failed: " .. tostring(auditResult), 0)
	end

	local restoredVerified = true
	if applyDewater then
		if origPacked then
			table.insert(slotEntry.undoStack, origPacked)
			if #slotEntry.undoStack > MAX_HISTORY then
				table.remove(slotEntry.undoStack, 1)
			end
			slotEntry.redoStack = {}
		end
		slotEntry.rev = (slotEntry.rev or 1) + 1
		setSlotXml(slotEntry, saveEngineToXML())
	else
		restoreOriginalState(false)
		local restoredStats = extractStatsTable()
		restoredVerified = (math.abs(restoredStats.EffectiveDPS - rawStats.EffectiveDPS) < 1e-3)
			and (math.abs(restoredStats.TotalEHP - rawStats.TotalEHP) < 1e-3)
	end
	collectgarbage("collect")

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
		items = extractItemsInfo(false),
		rawStats = rawStats,
		sustainableStats = auditResult.sustainableStats,
		weaponSwap = auditResult.weaponSwap,
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
	checkParentAlive()
	local calcFunc, baseOut = build.calcsTab:GetMiscCalculator()
	local res = evalSingleCandidateItem(calcFunc, baseOut, params, 1)
	calcFunc, baseOut = nil, nil
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
		checkParentAlive()
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
	calcFunc, baseOut = nil, nil
	collectgarbage("collect")

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
	if (statName == "FullDPS" or statName == "EffectiveDPS") and safeNum(baseStats.FullDPS, 0) <= 0 and safeNum(baseStats.EffectiveDPS, 0) > 0 then
		statName = "EffectiveDPS"
	end
	local baseVal = safeNum(baseStats[statName], baseStats.EffectiveDPS)
	local useFullDPS = (statName == "FullDPS") and (safeNum(baseStats.FullDPS, 0) > 0)

	local t0 = os.clock()
	local singleCache = {}
	local candidates = {}
	local fastAnointPrepass = (sortBy == "anoint" and useFullDPS)
	local prepassStat = fastAnointPrepass and "CombinedDPS" or statName
	local basePrepassVal = safeNum(baseStats[prepassStat], baseVal)
	local isOffenceOnly = (
		statName == "FullDPS"
		or statName == "EffectiveDPS"
		or statName == "CombinedDPS"
		or statName == "TotalDPS"
	)
	local fastNodeFastSkillsOpts = {
		requirementsItems = true,
		requirementsGems = true,
		skills = true,
		skipEHP = isOffenceOnly,
	}
	local fastNodeRebuildSkillsOpts = {
		requirementsItems = true,
		requirementsGems = true,
		skipEHP = isOffenceOnly,
	}
	local fastFullDpsOnlyOpts = {
		requirementsItems = true,
		requirementsGems = true,
		skills = true,
		skipEHP = true,
		fullDPSOnly = true,
	}
	local grantedPassives = (build.calcsTab and build.calcsTab.mainEnv and build.calcsTab.mainEnv.grantedPassives) or {}
	local loopCounter = 0

	for nodeId, node in pairs(build.spec.nodes or {}) do
		loopCounter = loopCounter + 1
		if loopCounter % 32 == 0 then checkParentAlive() end
		local isTargetType = (node.type == "Notable") or (includeKeystones and node.type == "Keystone")
		local canAnoint = (sortBy ~= "anoint") or (type(node.recipe) == "table" and #node.recipe > 0 and not grantedPassives[nodeId])
		if isTargetType and canAnoint and not node.ascendancyName and not node.alloc and node.modKey and node.modKey ~= "" then
			local dist = tonumber(node.pathDist) or 1000
			if sortBy == "anoint" or dist <= maxPathDist then
				if not singleCache[node.modKey] then
					checkParentAlive()
					local nOpts = node.grantedSkills and fastNodeRebuildSkillsOpts or fastNodeFastSkillsOpts
					singleCache[node.modKey] = extractStatsTable(calcFunc({ addNodes = { [node] = true } }, not fastAnointPrepass and useFullDPS, nOpts))
				end
				local singleStats = singleCache[node.modKey]
				local singleVal = safeNum(singleStats[prepassStat], singleStats.EffectiveDPS)
				local singleDelta = singleVal - basePrepassVal

				local fullPathDelta = singleDelta
				local pathIds = {}
				if singleDelta > 0.001 and sortBy ~= "anoint" and dist <= maxPathDist and node.path and #node.path > 0 then
					local pathSet = {}
					local pathHasGrantedSkill = false
					for _, pNode in ipairs(node.path) do
						if not pNode.alloc then
							pathSet[pNode] = true
							pathIds[#pathIds + 1] = pNode.id
							if pNode.grantedSkills then pathHasGrantedSkill = true end
						end
					end
					if dist > 1 and next(pathSet) ~= nil then
						checkParentAlive()
						local pOpts = pathHasGrantedSkill and fastNodeRebuildSkillsOpts or fastNodeFastSkillsOpts
						local pathStats = extractStatsTable(calcFunc({ addNodes = pathSet }, useFullDPS, pOpts))
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
						recipe = node.recipe or {},
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
		local refineLimit = math.min(#candidates, math.max(topN * 2, 16))
		local refined = {}
		local exactCache = {}
		for i = 1, refineLimit do
			checkParentAlive()
			local c = candidates[i]
			local nd = c._nodeRef
			if not exactCache[nd.modKey] then
				local rOpts = nd.grantedSkills and fastNodeRebuildSkillsOpts or fastFullDpsOnlyOpts
				exactCache[nd.modKey] = extractStatsTable(calcFunc({ addNodes = { [nd] = true } }, true, rOpts))
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
	singleCache, calcFunc, baseOut = nil, nil, nil
	collectgarbage("collect")

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
						recipe = node.recipe or {},
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
	local mainOut = build.calcsTab and build.calcsTab.mainOutput
	local hasFullDps = safeNum(mainOut and mainOut.FullDPS, 0) > 0
	local fallbackDpsStat = (safeNum(mainOut and mainOut.CombinedDPS, 0) > 0) and "CombinedDPS" or "TotalDPS"
	local statWeights = {}
	if type(params.statWeights) == "table" and #params.statWeights > 0 then
		for _, sw in ipairs(params.statWeights) do
			local st = tostring(sw.stat or "FullDPS")
			if st == "EffectiveDPS" then st = "FullDPS" end
			if st == "FullDPS" and not hasFullDps then st = fallbackDpsStat end
			statWeights[#statWeights + 1] = {
				stat = st,
				weightMult = tonumber(sw.weightMult or sw.weight) or 1.0,
			}
		end
	else
		statWeights[1] = { stat = hasFullDps and "FullDPS" or fallbackDpsStat, weightMult = 1.0 }
	end

	-- If targetSlot is currently empty, temporarily insert a zero-mod white item so
	-- TradeQueryGenerator:StartQuery resolves itemCategory cleanly, then remove it after!
	local tempItemObj = nil
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
		tempItemObj = tempItem
		slotCtrl.selItemId = tempItem.id
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
			maxPriceType = (type(params.maxPriceType) == "string" and params.maxPriceType ~= "") and params.maxPriceType or nil,
		}
		qg:StartQuery(slotCtrl, options)
		if qg.calcContext and type(qg.calcContext.calcFunc) == "function" then
			local rawCalcFunc = qg.calcContext.calcFunc
			local fastAmuletNodeOpts = { requirementsItems = true, requirementsGems = true, skills = true }
			qg.calcContext.calcFunc = function(override, useFullDPS, fastOpts)
				if fastOpts == nil and type(override) == "table" and override.addNodes and not override.repSlotName then
					local anyGrantedSkill = false
					for nd, _ in pairs(override.addNodes) do
						if type(nd) == "table" and nd.grantedSkills then
							anyGrantedSkill = true
							break
						end
					end
					if not anyGrantedSkill then
						return rawCalcFunc(override, useFullDPS, fastAmuletNodeOpts)
					end
				end
				return rawCalcFunc(override, useFullDPS, fastOpts)
			end
		end
		while qg.calcContext and qg.calcContext.co do
			checkParentAlive()
			qg:OnFrame()
		end
		closeAllPopups()
	end)

	-- Clean up temporary probe item if we inserted one
	if tempItemObj then
		slotCtrl.selItemId = origSelItemId
		build.itemsTab:DeleteItem(tempItemObj, true)
		closeAllPopups()
	end

	if not okRun then
		qg = nil
		collectgarbage("collect")
		error("TradeQueryGenerator failed: " .. tostring(runErr), 0)
	end

	local statTextMap = getTradeStatTextMap()
	local topN = tonumber(params.topN) or 35
	local weightsOut = {}
	local rawWeights = (qg and qg.modWeights) or {}
	local totalWeightedMods = #rawWeights
	local itemCategoryStr = qg and qg.calcContext and qg.calcContext.itemCategoryQueryStr or dkjson.null
	for i = 1, math.min(topN, totalWeightedMods) do
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
	qg, rawWeights = nil, nil
	collectgarbage("collect")

	local tradePayload = capturedQuery and {
		query = capturedQuery,
		sort = { ["statgroup.0"] = "desc" },
	} or dkjson.null

	return {
		slot = slotName,
		targetSlot = targetSlot,
		itemCategory = itemCategoryStr,
		totalWeightedMods = totalWeightedMods,
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
		checkParentAlive()
		local mName = call and call.method
		local fn = mName and methods[mName]
		if not fn or mName == "call_batch" then
			out[i] = { ok = false, method = mName or dkjson.null, error = "Unknown or disallowed batch method: " .. tostring(mName) }
		else
			local okC, resOrErr = pcall(fn, call.params or {})
			checkParentAlive()
			if okC then
				out[i] = { ok = true, method = mName, result = resOrErr }
			else
				out[i] = { ok = false, method = mName, error = tostring(resOrErr) }
			end
		end
	end
	return { results = out }
end

-- ============================================================================
-- Capability 10: Calcs Breakdown & Radius Jewel Socket Optimizer
-- ============================================================================

local function stripPobColorCodes(val)
	if val == nil then return "" end
	local s = tostring(val)
	s = s:gsub("%^x%x%x%x%x%x%x", "")
	s = s:gsub("%^%d", "")
	return s
end

local STAT_QUERY_ALIASES = {
	manacost = {
		outputs = { "ManaCost", "ManaPerSecondCost", "ManaHasCost" },
		breakdowns = { "ManaCost", "ManaPerSecondCost" },
		mods = { "ManaCost", "Cost", "ManaCostNoMult" },
	},
	cost = {
		outputs = { "ManaCost", "ManaPerSecondCost", "LifeCost" },
		breakdowns = { "ManaCost", "ManaPerSecondCost" },
		mods = { "ManaCost", "Cost" },
	},
	castspeed = {
		outputs = { "Speed", "CastRate", "Time" },
		breakdowns = { "Speed" },
		mods = { "Speed" },
		cfg = "skill",
	},
	attackspeed = {
		outputs = { "Speed", "HitSpeed", "Time" },
		breakdowns = { "Speed" },
		mods = { "Speed" },
		cfg = "skill",
	},
	speed = {
		outputs = { "Speed", "MovementSpeedMod", "EffectiveMovementSpeedMod" },
		breakdowns = { "Speed", "MovementSpeedMod" },
		mods = { "Speed", "MovementSpeed" },
		cfg = "skill",
	},
	crit = {
		outputs = { "CritChance", "PreEffectiveCritChance", "CritMultiplier", "CritEffect" },
		breakdowns = { "CritChance", "CritMultiplier", "CritEffect" },
		mods = { "CritChance", "CritMultiplier" },
		cfg = "skill",
	},
	critchance = {
		outputs = { "CritChance", "PreEffectiveCritChance" },
		breakdowns = { "CritChance" },
		mods = { "CritChance" },
		cfg = "skill",
	},
	critmulti = {
		outputs = { "CritMultiplier", "CritEffect" },
		breakdowns = { "CritMultiplier", "CritEffect" },
		mods = { "CritMultiplier" },
		cfg = "skill",
	},
	dps = {
		outputs = { "FullDPS", "CombinedDPS", "TotalDPS", "TotalDot", "AverageDamage", "Speed", "HitChance" },
		breakdowns = { "FullDPS", "CombinedDPS", "TotalDPS", "AverageDamage", "Speed" },
		mods = { "Damage", "SpellDamage", "PhysicalDamage", "LightningDamage", "ColdDamage", "FireDamage", "ChaosDamage" },
		cfg = "skill",
	},
	fulldps = {
		outputs = { "FullDPS", "CombinedDPS", "TotalDPS", "AverageDamage", "Speed" },
		breakdowns = { "FullDPS", "CombinedDPS", "TotalDPS", "AverageDamage" },
		mods = { "Damage", "SpellDamage", "PhysicalDamage", "LightningDamage", "ColdDamage", "FireDamage", "ChaosDamage" },
		cfg = "skill",
	},
	spirit = {
		outputs = { "Spirit", "SpiritReserved", "SpiritUnreserved", "SpiritReservedPercent" },
		breakdowns = { "Spirit", "SpiritReserved" },
		mods = { "Spirit", "SpiritReservationEfficiency", "ReservationEfficiency" },
	},
	es = {
		outputs = { "EnergyShield", "EnergyShieldRecharge", "EnergyShieldRecoveryCap" },
		breakdowns = { "EnergyShield", "EnergyShieldRecharge" },
		mods = { "EnergyShield", "EnergyShieldRecharge", "EnergyShieldRechargeFaster", "Defences" },
	},
	energyshield = {
		outputs = { "EnergyShield", "EnergyShieldRecharge", "EnergyShieldRecoveryCap" },
		breakdowns = { "EnergyShield", "EnergyShieldRecharge" },
		mods = { "EnergyShield", "EnergyShieldRecharge", "Defences" },
	},
	life = {
		outputs = { "Life", "LifeUnreserved", "LifeRegenRecovery" },
		breakdowns = { "Life", "LifeRegenRecovery" },
		mods = { "Life", "LifeRegen", "LifeRegenPercent" },
	},
	mana = {
		outputs = { "Mana", "ManaUnreserved", "ManaRegenRecovery", "ManaCost", "ManaPerSecondCost" },
		breakdowns = { "Mana", "ManaRegenRecovery", "ManaCost" },
		mods = { "Mana", "ManaRegen", "ManaCost" },
	},
	chaosresist = {
		outputs = { "ChaosResist", "ChaosResistOverCap", "ChaosMaximumHitTaken" },
		breakdowns = { "ChaosResist", "ChaosMaximumHitTaken" },
		mods = { "ChaosResist", "ChaosResistMax", "ChaosDamageTaken", "DamageTaken" },
	},
	fireresist = {
		outputs = { "FireResist", "FireResistOverCap", "FireMaximumHitTaken" },
		breakdowns = { "FireResist", "FireMaximumHitTaken" },
		mods = { "FireResist", "ElementalResist", "FireResistMax" },
	},
	coldresist = {
		outputs = { "ColdResist", "ColdResistOverCap", "ColdMaximumHitTaken" },
		breakdowns = { "ColdResist", "ColdMaximumHitTaken" },
		mods = { "ColdResist", "ElementalResist", "ColdResistMax" },
	},
	lightningresist = {
		outputs = { "LightningResist", "LightningResistOverCap", "LightningMaximumHitTaken" },
		breakdowns = { "LightningResist", "LightningMaximumHitTaken" },
		mods = { "LightningResist", "ElementalResist", "LightningResistMax" },
	},
	armour = {
		outputs = { "Armour", "PhysicalDamageReduction", "PhysicalMaximumHitTaken" },
		breakdowns = { "Armour", "PhysicalDamageReduction", "PhysicalMaximumHitTaken" },
		mods = { "Armour", "Defences", "PhysicalDamageReduction", "PhysicalDamageTaken" },
	},
	evasion = {
		outputs = { "Evasion", "EvadeChance", "DeflectChance", "DeflectionRating" },
		breakdowns = { "Evasion", "EvadeChance", "DeflectChance" },
		mods = { "Evasion", "Defences", "DeflectionRating" },
	},
	ehp = {
		outputs = { "TotalEHP", "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken", "ChaosMaximumHitTaken" },
		breakdowns = { "TotalEHP", "PhysicalMaximumHitTaken", "ChaosMaximumHitTaken" },
		mods = { "Life", "EnergyShield", "Armour", "Evasion", "DamageTaken" },
	},
}

local function resolveModSourceLabel(mod, targetBuild)
	local rawSrc = tostring((mod and mod.source) or "")
	local srcType = rawSrc:match("^([^:]+)") or "Unknown"
	local label = rawSrc
	if srcType == "Item" then
		local itemId = tonumber(rawSrc:match("^Item:(%d+)"))
		local item = itemId and targetBuild.itemsTab and targetBuild.itemsTab.items[itemId]
		local slotStr = (mod and mod.sourceSlot) and (" (" .. tostring(mod.sourceSlot) .. ")") or ""
		if item then
			label = stripPobColorCodes(item.name or item.baseName or ("Item #" .. tostring(itemId))) .. slotStr
		else
			label = stripPobColorCodes(rawSrc:match("^Item:%d+:(.+)") or rawSrc) .. slotStr
		end
	elseif srcType == "Tree" then
		local nodeId = tonumber(rawSrc:match("^Tree:(%d+)"))
		local node = nodeId and targetBuild.spec and (
			targetBuild.spec.nodes[nodeId]
			or (targetBuild.spec.switchableNodes and targetBuild.spec.switchableNodes[nodeId])
			or (targetBuild.spec.tree and targetBuild.spec.tree.nodes[nodeId])
		)
		if node and node.dn then
			label = stripPobColorCodes(node.dn) .. " (#" .. tostring(nodeId) .. ")"
		end
	elseif srcType == "Skill" then
		local skillId = rawSrc:match("^Skill:(.+)")
		local sk = skillId and targetBuild.data and targetBuild.data.skills and targetBuild.data.skills[skillId]
		label = (sk and sk.name) or skillId or rawSrc
	elseif srcType == "Rune" or srcType == "Custom" or srcType == "Config" or srcType == "Many Sources" then
		label = stripPobColorCodes(rawSrc:match("^[^:]+:(.+)") or rawSrc)
	end
	return srcType, label
end

local function formatModFlagsText(mod)
	if not mod then return "" end
	local names = {}
	local mf = tonumber(mod.flags) or 0
	local kf = tonumber(mod.keywordFlags) or 0
	if mf ~= 0 and type(ModFlag) == "table" then
		for name, bitVal in pairs(ModFlag) do
			if type(bitVal) == "number" and bitVal > 0 and bit.band(mf, bitVal) == bitVal then
				names[#names + 1] = name
			end
		end
	end
	if kf ~= 0 and type(KeywordFlag) == "table" then
		for name, bitVal in pairs(KeywordFlag) do
			if type(bitVal) == "number" and bitVal > 0 and bit.band(kf, bitVal) == bitVal then
				names[#names + 1] = "Keyword:" .. name
			end
		end
	end
	table.sort(names)
	return table.concat(names, ", ")
end

methods.calc_breakdown = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	checkParentAlive()

	local rawQuery = tostring((params and params.query) or "FullDPS")
	local normQuery = rawQuery:lower():gsub("[^%w]", "")
	local rowLimit = math.max(1, math.min(100, tonumber(params and params.limit) or 25))
	local requestedSkillGroup = params and params.skillGroup ~= nil and tonumber(params.skillGroup) or nil

	local calcsTab = build.calcsTab
	local savedSkillNum = calcsTab.input and calcsTab.input.skill_number or 1
	local mainGroupIdx = build.mainSocketGroup or 1
	local warnings = {}

	if requestedSkillGroup ~= nil then
		if not build.skillsTab.socketGroupList[requestedSkillGroup] then
			error("Unknown socket group index " .. tostring(requestedSkillGroup), 0)
		end
		calcsTab.input.skill_number = requestedSkillGroup
	elseif savedSkillNum ~= mainGroupIdx and build.skillsTab.socketGroupList[savedSkillNum] and build.skillsTab.socketGroupList[mainGroupIdx] then
		local calcsGrp = build.skillsTab.socketGroupList[savedSkillNum]
		local mainGrp = build.skillsTab.socketGroupList[mainGroupIdx]
		local cAct = calcsGrp.displaySkillList and calcsGrp.displaySkillList[calcsGrp.mainActiveSkill or 1]
		local mAct = mainGrp.displaySkillList and mainGrp.displaySkillList[mainGrp.mainActiveSkill or 1]
		local cName = (cAct and cAct.activeEffect and cAct.activeEffect.grantedEffect and cAct.activeEffect.grantedEffect.name) or calcsGrp.label or ("Group #" .. tostring(savedSkillNum))
		local mName = (mAct and mAct.activeEffect and mAct.activeEffect.grantedEffect and mAct.activeEffect.grantedEffect.name) or mainGrp.label or ("Group #" .. tostring(mainGroupIdx))
		if cName ~= mName then
			warnings[#warnings + 1] = string.format(
				"PoB2 Calcs tab was pointing at socket group #%d (%s) while mainSkill is socket group #%d (%s); aligned inspection to mainSkill #%d (pass skill_group=%d to inspect %s).",
				savedSkillNum, cName, mainGroupIdx, mName, mainGroupIdx, savedSkillNum, cName
			)
		end
		calcsTab.input.skill_number = mainGroupIdx
	end

	local inspectedGroupIdx = calcsTab.input.skill_number or mainGroupIdx
	local okBuild, buildErr = pcall(function()
		calcsTab:BuildOutput()
	end)
	calcsTab.input.skill_number = savedSkillNum
	if not okBuild then
		error("calcsTab:BuildOutput() failed: " .. tostring(buildErr), 0)
	end

	local calcsEnv = calcsTab.calcsEnv
	local actor = calcsEnv and calcsEnv.player
	local output = (actor and actor.output) or calcsTab.mainOutput or {}
	local breakdown = (actor and actor.breakdown) or {}
	local mainSkill = actor and actor.mainSkill
	local activeSkillName = (mainSkill and mainSkill.activeEffect and mainSkill.activeEffect.grantedEffect and mainSkill.activeEffect.grantedEffect.name)
		or "Default"

	local aliasSpec = STAT_QUERY_ALIASES[normQuery]
	local targetOutputs = {}
	local targetBreakdowns = {}
	local targetMods = {}
	local seenOut, seenBd, seenMod = {}, {}, {}

	local function pushUnique(tbl, seen, key)
		if key and not seen[key] then
			seen[key] = true
			tbl[#tbl + 1] = key
		end
	end

	if aliasSpec then
		for _, k in ipairs(aliasSpec.outputs or {}) do pushUnique(targetOutputs, seenOut, k) end
		for _, k in ipairs(aliasSpec.breakdowns or {}) do pushUnique(targetBreakdowns, seenBd, k) end
		for _, k in ipairs(aliasSpec.mods or {}) do pushUnique(targetMods, seenMod, k) end
	end

	for k, v in pairs(output) do
		if type(k) == "string" and (type(v) == "number" or type(v) == "boolean" or type(v) == "string") then
			local kl = k:lower():gsub("[^%w]", "")
			if kl == normQuery or (normQuery ~= "" and kl:find(normQuery, 1, true)) then
				pushUnique(targetOutputs, seenOut, k)
				if breakdown[k] then pushUnique(targetBreakdowns, seenBd, k) end
				pushUnique(targetMods, seenMod, k)
			end
		end
	end
	for k, _ in pairs(breakdown) do
		if type(k) == "string" then
			local kl = k:lower():gsub("[^%w]", "")
			if kl == normQuery or (normQuery ~= "" and kl:find(normQuery, 1, true)) then
				pushUnique(targetBreakdowns, seenBd, k)
				if output[k] ~= nil then pushUnique(targetOutputs, seenOut, k) end
				pushUnique(targetMods, seenMod, k)
			end
		end
	end

	-- Also scan active modDB + skillModList keys so direct mod names ("SpellDamage", "ProjectileCount", etc.) resolve cleanly
	for _, store in ipairs({ mainSkill and mainSkill.skillModList, actor and actor.modDB }) do
		if store and type(store.mods) == "table" then
			for modKey, _ in pairs(store.mods) do
				if type(modKey) == "string" then
					local ml = modKey:lower():gsub("[^%w]", "")
					if ml == normQuery or (normQuery ~= "" and ml:find(normQuery, 1, true)) then
						pushUnique(targetMods, seenMod, modKey)
					end
				end
			end
		end
	end

	table.sort(targetOutputs, function(a, b)
		local al = (a:lower():gsub("[^%w]", "") == normQuery) and 0 or 1
		local bl = (b:lower():gsub("[^%w]", "") == normQuery) and 0 or 1
		if al ~= bl then return al < bl end
		return a < b
	end)

	local matchedOutputs = {}
	for i = 1, math.min(#targetOutputs, 15) do
		local k = targetOutputs[i]
		local v = output[k]
		if type(v) == "number" then
			matchedOutputs[#matchedOutputs + 1] = { key = k, value = safeNum(v, 0) }
		elseif type(v) == "boolean" or type(v) == "string" then
			matchedOutputs[#matchedOutputs + 1] = { key = k, value = stripPobColorCodes(v) }
		end
	end

	local formulaBreakdowns = {}
	local localDefenceRows = {}
	for i = 1, math.min(#targetBreakdowns, 8) do
		local bk = targetBreakdowns[i]
		local bEntry = breakdown[bk]
		if type(bEntry) == "table" then
			local lines = {}
			for _, ln in ipairs(bEntry) do
				if type(ln) == "string" and ln ~= "" then
					lines[#lines + 1] = stripPobColorCodes(ln)
				end
			end
			local slotRows = {}
			if type(bEntry.slots) == "table" then
				for _, sr in ipairs(bEntry.slots) do
					local itemLbl = (sr.item and sr.item.name and stripPobColorCodes(sr.item.name)) or sr.sourceName or sr.source or ""
					local slotSrc = tostring(sr.source or "")
					local slotTotal = safeNum(tonumber(sr.total), 0)
					slotRows[#slotRows + 1] = {
						source = slotSrc,
						sourceLabel = tostring(itemLbl),
						base = safeNum(tonumber(sr.base), 0),
						inc = safeNum(tonumber(sr.inc), 0),
						more = safeNum(tonumber(sr.more), 0),
						total = slotTotal,
					}
					if slotTotal > 0 and (sr.item ~= nil or (slotSrc ~= "" and slotSrc ~= "Base" and slotSrc ~= "Global")) then
						local lbl = slotSrc ~= "" and (slotSrc .. (itemLbl ~= "" and itemLbl ~= slotSrc and (": " .. tostring(itemLbl)) or "")) or tostring(itemLbl)
						localDefenceRows[#localDefenceRows + 1] = {
							statName = bk .. " (Local Defence)",
							modType = "BASE",
							value = slotTotal,
							numericValue = slotTotal,
							sourceType = "Item",
							sourceLabel = lbl,
							rawSource = "Slot:" .. slotSrc,
							flags = "",
						}
					end
				end
			end
			local reservationRows = {}
			if type(bEntry.reservations) == "table" then
				for _, rr in ipairs(bEntry.reservations) do
					reservationRows[#reservationRows + 1] = {
						skillName = stripPobColorCodes(rr.skillName or ""),
						base = tostring(rr.base or ""),
						mult = tostring(rr.mult or ""),
						efficiency = tostring(rr.efficiency or ""),
						total = tostring(rr.total or ""),
					}
				end
			end
			formulaBreakdowns[#formulaBreakdowns + 1] = {
				key = bk,
				outputValue = (type(output[bk]) == "number" and safeNum(output[bk], 0))
					or (output[bk] ~= nil and stripPobColorCodes(output[bk]))
					or dkjson.null,
				lines = lines,
				slots = slotRows,
				reservations = reservationRows,
			}
		end
	end

	local cfg = {}
	if mainSkill and mainSkill.skillCfg then
		for k, v in pairs(mainSkill.skillCfg) do cfg[k] = v end
	end
	cfg.ignoreSourceInCheckConditions = true

	local rawRows = {}
	local seenRowSig = {}
	for _, ldr in ipairs(localDefenceRows) do
		local sig = ldr.statName .. "|" .. ldr.sourceLabel .. "|" .. tostring(ldr.value)
		if not seenRowSig[sig] then
			seenRowSig[sig] = true
			rawRows[#rawRows + 1] = ldr
		end
	end
	local modStores = {}
	if mainSkill and mainSkill.skillModList then
		modStores[#modStores + 1] = mainSkill.skillModList
	end
	if actor and actor.modDB then
		modStores[#modStores + 1] = actor.modDB
	end

	for _, mName in ipairs(targetMods) do
		for _, store in ipairs(modStores) do
			if store and store.Tabulate then
				local okTab, rows = pcall(function()
					return store:Tabulate(nil, cfg, mName)
				end)
				if okTab and type(rows) == "table" then
					for _, r in ipairs(rows) do
						local mod = r.mod
						if mod then
							local srcType, srcLabel = resolveModSourceLabel(mod, build)
							local val = r.value
							local numVal = (type(val) == "number") and safeNum(val, 0) or nil
							local strVal = (type(val) ~= "number") and stripPobColorCodes(tostring(val)) or nil
							local sig = table.concat({
								tostring(mod.name or mName),
								tostring(mod.type or ""),
								tostring(numVal or strVal or ""),
								tostring(mod.source or ""),
							}, "|")
							if not seenRowSig[sig] then
								seenRowSig[sig] = true
								rawRows[#rawRows + 1] = {
									statName = tostring(mod.name or mName),
									modType = tostring(mod.type or "BASE"),
									value = numVal ~= nil and numVal or strVal,
									numericValue = numVal or 0,
									sourceType = srcType,
									sourceLabel = srcLabel,
									rawSource = tostring(mod.source or ""),
									flags = formatModFlagsText(mod),
								}
							end
						end
					end
				end
			end
		end
	end

	local typeOrder = { BASE = 1, INC = 2, MORE = 3, OVERRIDE = 4, FLAG = 5 }
	table.sort(rawRows, function(a, b)
		local ta = typeOrder[a.modType] or 9
		local tb = typeOrder[b.modType] or 9
		if ta ~= tb then return ta < tb end
		local aa = math.abs(a.numericValue or 0)
		local ab = math.abs(b.numericValue or 0)
		if math.abs(aa - ab) > 1e-6 then return aa > ab end
		return a.sourceLabel < b.sourceLabel
	end)

	local sourceTotals = {}
	for _, r in ipairs(rawRows) do
		local bucket = sourceTotals[r.sourceType]
		if not bucket then
			bucket = { BASE = 0, INC = 0, MORE_PCT = 0, count = 0 }
			sourceTotals[r.sourceType] = bucket
		end
		bucket.count = bucket.count + 1
		if r.modType == "BASE" then
			bucket.BASE = bucket.BASE + (r.numericValue or 0)
		elseif r.modType == "INC" then
			bucket.INC = bucket.INC + (r.numericValue or 0)
		elseif r.modType == "MORE" then
			local curMult = 1.0 + (bucket.MORE_PCT / 100.0)
			curMult = curMult * (1.0 + (r.numericValue or 0) / 100.0)
			bucket.MORE_PCT = (curMult - 1.0) * 100.0
		end
	end

	local trimmedRows = {}
	for i = 1, math.min(#rawRows, rowLimit) do
		local r = rawRows[i]
		trimmedRows[i] = {
			statName = r.statName,
			modType = r.modType,
			value = r.value,
			sourceType = r.sourceType,
			sourceLabel = r.sourceLabel,
			flags = r.flags,
		}
	end

	return {
		slot = slotName,
		query = rawQuery,
		skillGroup = inspectedGroupIdx,
		activeSkillName = activeSkillName,
		matchedOutputs = matchedOutputs,
		formulaBreakdowns = formulaBreakdowns,
		modifierCountTotal = #rawRows,
		modifiers = trimmedRows,
		sourceTotals = sourceTotals,
		warnings = warnings,
	}
end

local CONTROLLED_METAMORPHOSIS_RINGS = {
	{ radiusIndex = 5, ringIndex = 1, label = "Very Small Ring (650-950)" },
	{ radiusIndex = 6, ringIndex = 2, label = "Small Ring (800-1100)" },
	{ radiusIndex = 7, ringIndex = 3, label = "Medium-Small Ring (950-1250)" },
	{ radiusIndex = 8, ringIndex = 4, label = "Medium Ring (1100-1400)" },
	{ radiusIndex = 9, ringIndex = 5, label = "Medium-Large Ring (1250-1550)" },
	{ radiusIndex = 10, ringIndex = 6, label = "Large Ring (1400-1700)" },
	{ radiusIndex = 11, ringIndex = 7, label = "Very Large Ring (1650-1950)" },
	{ radiusIndex = 12, ringIndex = 8, label = "Massive Ring (1800-2100)" },
}

methods.rank_radius_jewel_sockets = function(params)
	local slotName = normalizeSlotName(params, "slot")
	ensureSlot(slotName, false)
	build.spec:BuildAllDependsAndPaths()

	local statName = (params and params.stat) or "FullDPS"
	local topN = math.max(1, math.min(30, tonumber(params and params.topN) or 10))
	local maxSocketDist = tonumber(params and params.maxPathDist) or 10
	local jewelQuery = tostring((params and params.jewelName) or "Controlled Metamorphosis")
	local jLow = jewelQuery:lower()
	local isAgainstDarkness = jLow:find("against", 1, true) ~= nil or jLow:find("darkness", 1, true) ~= nil

	local calcFunc, baseOut = build.calcsTab:GetMiscCalculator()
	local baseStats = extractStatsTable(baseOut)
	if (statName == "FullDPS" or statName == "EffectiveDPS") and safeNum(baseStats.FullDPS, 0) <= 0 and safeNum(baseStats.EffectiveDPS, 0) > 0 then
		statName = "EffectiveDPS"
	end
	local baseVal = safeNum(baseStats[statName], baseStats.EffectiveDPS)
	local useFullDPS = (statName == "FullDPS") and (safeNum(baseStats.FullDPS, 0) > 0)
	local prepassStat = (useFullDPS and safeNum(baseStats.CombinedDPS, 0) > 0) and "CombinedDPS" or statName
	local basePrepassVal = safeNum(baseStats[prepassStat], baseVal)
	local isOffenceOnly = (statName == "FullDPS" or statName == "EffectiveDPS" or statName == "CombinedDPS" or statName == "TotalDPS")
	local fastOpts = {
		requirementsItems = true,
		requirementsGems = true,
		skills = true,
		skipEHP = isOffenceOnly,
	}

	local singleCache = {}
	local candidates = {}

	for nodeId, sockNode in pairs(build.spec.nodes or {}) do
		local isSocket = (sockNode.type == "Socket" or sockNode.isJewelSocket == true) and not sockNode.ascendancyName
		if isSocket and type(sockNode.nodesInRadius) == "table" then
			local sockAlloc = sockNode.alloc == true
			local sockDist = sockAlloc and 0 or (tonumber(sockNode.pathDist) or 1000)
			if sockDist <= maxSocketDist then
				checkParentAlive()
				if isAgainstDarkness then
					local inLarge = sockNode.nodesInRadius[3] or {}
					local pathNodeIds = {}
					if not sockAlloc and sockNode.path then
						for _, pNode in ipairs(sockNode.path) do
							if pNode.id then pathNodeIds[pNode.id] = true end
						end
					end
					local allocNotables = {}
					local pathNotables = {}
					local allocSmalls = 0
					local nearUnallocNotables = {}
					local bestUnallocNode = nil
					local bestUnallocDist = 9999
					for rId, rawNode in pairs(inLarge) do
						local rNode = (build.spec.nodes and build.spec.nodes[rId]) or rawNode
						if not rNode.ascendancyName then
							if rNode.alloc then
								if rNode.type == "Notable" then
									allocNotables[#allocNotables + 1] = stripPobColorCodes(rNode.dn or tostring(rNode.id))
								elseif rNode.type == "Normal" then
									allocSmalls = allocSmalls + 1
								end
							elseif pathNodeIds[rNode.id] then
								if rNode.type == "Notable" then
									pathNotables[#pathNotables + 1] = stripPobColorCodes(rNode.dn or tostring(rNode.id))
								elseif rNode.type == "Normal" then
									allocSmalls = allocSmalls + 1
								end
							elseif rNode.type == "Notable" then
								nearUnallocNotables[#nearUnallocNotables + 1] = stripPobColorCodes(rNode.dn or tostring(rNode.id))
								local rDist = tonumber(rNode.pathDist) or 9999
								if rDist < bestUnallocDist then
									bestUnallocDist = rDist
									bestUnallocNode = rNode
								end
							end
						end
					end
					table.sort(allocNotables)
					table.sort(pathNotables)
					table.sort(nearUnallocNotables)
					local effectiveNotables = #allocNotables + #pathNotables
					if effectiveNotables > 0 or #nearUnallocNotables > 0 then
						local addSet = {}
						local extraPts = 0
						if not sockAlloc and sockNode.path then
							for _, pNode in ipairs(sockNode.path) do
								if not pNode.alloc then addSet[pNode] = true end
							end
						end
						if effectiveNotables == 0 and bestUnallocNode ~= nil then
							effectiveNotables = 1
							addSet[bestUnallocNode] = true
							extraPts = 1
						end
						-- Evaluate representative Against the Darkness bonus per allocated notable in Large radius
						local bonusLine = isOffenceOnly
							and string.format("Gain %d%% of Damage as Extra Lightning Damage", effectiveNotables * 4)
							or string.format("%d%% increased maximum Life\n%d%% increased maximum Energy Shield", effectiveNotables * 4, effectiveNotables * 4)
						local simItem = nil
						pcall(function()
							simItem = new("Item"):Item("Rarity: UNIQUE\nAgainst the Darkness\nTime-Lost Diamond\nImplicits: 0\n" .. bonusLine)
						end)
						local calcArgs = {}
						if next(addSet) ~= nil then calcArgs.addNodes = addSet end
						if simItem then
							calcArgs.repSlotName = "Jewel " .. tostring(nodeId)
							calcArgs.repItem = simItem
						end
						local simStats = extractStatsTable(calcFunc(calcArgs, useFullDPS))
						local newVal = safeNum(simStats[statName], simStats.EffectiveDPS)
						local delta = newVal - baseVal
						local pct = baseVal > 0 and (delta / baseVal * 100.0) or 0.0
						local totalSpent = sockDist + extraPts
						local pts = math.max(1, totalSpent)
						local activeRingNotables = {}
						for _, nm in ipairs(allocNotables) do activeRingNotables[#activeRingNotables + 1] = nm end
						for _, nm in ipairs(pathNotables) do activeRingNotables[#activeRingNotables + 1] = nm end
						if #activeRingNotables == 0 and bestUnallocNode ~= nil then
							activeRingNotables[1] = stripPobColorCodes(bestUnallocNode.dn or tostring(bestUnallocNode.id))
						end
						candidates[#candidates + 1] = {
							jewelName = "Against the Darkness",
							socketNodeId = nodeId,
							socketName = stripPobColorCodes(sockNode.dn or ("Jewel Socket #" .. tostring(nodeId))),
							socketAllocated = sockAlloc,
							socketPathDist = sockDist,
							ringIndex = 3,
							ringLabel = "Large Radius (0-1300)",
							allocatedNotablesInRadius = #allocNotables + #pathNotables,
							allocatedSmallsInRadius = allocSmalls,
							ringNotables = activeRingNotables,
							nearbyUnallocatedNotables = nearUnallocNotables,
							totalPointsSpent = totalSpent,
							normalPointsSaved = 0,
							stat = statName,
							before = baseVal,
							after = newVal,
							delta = delta,
							pct = pct,
							perPointDelta = delta / pts,
							perPointPct = pct / pts,
						}
					end
				else
					for _, ringInfo in ipairs(CONTROLLED_METAMORPHOSIS_RINGS) do
						local inRing = sockNode.nodesInRadius[ringInfo.radiusIndex] or {}
						local posNodes = {}
						local notableNames = {}
						local sumSingleDelta = 0
						local savedTravelPts = 0
						for rId, rawNode in pairs(inRing) do
							local rNode = (build.spec.nodes and build.spec.nodes[rId]) or rawNode
							if rNode.type == "Notable" and not rNode.ascendancyName and not rNode.alloc and rNode.modKey and rNode.modKey ~= "" then
								if not singleCache[rNode.modKey] then
									singleCache[rNode.modKey] = extractStatsTable(calcFunc({ addNodes = { [rNode] = true } }, false, fastOpts))
								end
								local sStats = singleCache[rNode.modKey]
								local sDelta = safeNum(sStats[prepassStat], sStats.EffectiveDPS) - basePrepassVal
								if sDelta > 0 then
									posNodes[#posNodes + 1] = { node = rNode, delta = sDelta }
									sumSingleDelta = sumSingleDelta + sDelta
									local normalDist = tonumber(rNode.pathDist) or 4
									if normalDist < 500 and normalDist > 1 then
										savedTravelPts = savedTravelPts + (normalDist - 1)
									end
								end
							end
						end
						if #posNodes > 0 then
							table.sort(posNodes, function(a, b) return a.delta > b.delta end)
							for _, pn in ipairs(posNodes) do
								notableNames[#notableNames + 1] = stripPobColorCodes(pn.node.dn or tostring(pn.node.id))
							end
							local totalPts = sockDist + #posNodes
							candidates[#candidates + 1] = {
								_posNodes = posNodes,
								_sockNode = sockNode,
								jewelName = "Controlled Metamorphosis",
								socketNodeId = nodeId,
								socketName = stripPobColorCodes(sockNode.dn or ("Jewel Socket #" .. tostring(nodeId))),
								socketAllocated = sockAlloc,
								socketPathDist = sockDist,
								ringIndex = ringInfo.ringIndex,
								ringLabel = ringInfo.label,
								allocatedNotablesInRadius = 0,
								allocatedSmallsInRadius = 0,
								ringNotables = notableNames,
								nearbyUnallocatedNotables = notableNames,
								totalPointsSpent = totalPts,
								normalPointsSaved = math.max(0, savedTravelPts - sockDist),
								stat = statName,
								before = baseVal,
								after = baseVal + sumSingleDelta,
								delta = sumSingleDelta,
								pct = baseVal > 0 and (sumSingleDelta / baseVal * 100.0) or 0.0,
								perPointDelta = sumSingleDelta / math.max(1, totalPts),
								perPointPct = (baseVal > 0 and (sumSingleDelta / baseVal * 100.0) or 0.0) / math.max(1, totalPts),
							}
						end
					end
				end
			end
		end
	end

	table.sort(candidates, function(a, b)
		if math.abs(a.perPointDelta - b.perPointDelta) > 1e-4 then
			return a.perPointDelta > b.perPointDelta
		end
		return a.delta > b.delta
	end)

	local trimmed = {}
	for i = 1, math.min(#candidates, topN) do
		local c = candidates[i]
		if c._posNodes then
			local addSet = {}
			if not c.socketAllocated and c._sockNode.path then
				for _, pNode in ipairs(c._sockNode.path) do
					if not pNode.alloc then addSet[pNode] = true end
				end
			end
			for _, pn in ipairs(c._posNodes) do
				addSet[pn.node] = true
			end
			local exactStats = extractStatsTable(calcFunc({ addNodes = addSet }, useFullDPS))
			local exactAfter = safeNum(exactStats[statName], exactStats.EffectiveDPS)
			local exactDelta = exactAfter - baseVal
			local exactPct = baseVal > 0 and (exactDelta / baseVal * 100.0) or 0.0
			local pts = math.max(1, c.totalPointsSpent)
			c.after = exactAfter
			c.delta = exactDelta
			c.pct = exactPct
			c.perPointDelta = exactDelta / pts
			c.perPointPct = exactPct / pts
			c._posNodes = nil
			c._sockNode = nil
		end
		trimmed[i] = c
	end

	table.sort(trimmed, function(a, b)
		if math.abs(a.perPointDelta - b.perPointDelta) > 1e-4 then
			return a.perPointDelta > b.perPointDelta
		end
		return a.delta > b.delta
	end)

	calcFunc, baseOut = nil, nil
	collectgarbage("collect")

	return {
		slot = slotName,
		jewelName = isAgainstDarkness and "Against the Darkness" or "Controlled Metamorphosis",
		stat = statName,
		baseValue = baseVal,
		count = #trimmed,
		candidates = trimmed,
	}
end

methods.import_ggg_character_json = function(params)
	if type(params) ~= "table" or type(params.characterJson) ~= "string" or params.characterJson == "" then
		error("params.characterJson is required", 0)
	end
	local slotName = normalizeSlotName(params, "slot")
	local decoded, _, decErr = dkjson.decode(params.characterJson)
	if type(decoded) ~= "table" then
		error("Invalid GGG character JSON: " .. tostring(decErr or "decode failed"), 0)
	end
	local charData = type(decoded.character) == "table" and decoded.character or decoded
	if type(charData.passives) ~= "table" and type(charData.equipment) ~= "table" then
		error("GGG character JSON is missing passives/equipment fields", 0)
	end

	local entry = ensureSlot(slotName, true)
	local charName = tostring((params and params.name) or charData.name or slotName)
	local blankXml = '<PathOfBuilding2><Build level="' .. tostring(tonumber(charData.level) or 1) .. '" className="Witch" ascendClassName="None" mainSocketGroup="1"/><Tree activeSpec="1"><Spec treeVersion="0_5" classId="0" ascendClassId="0" nodes=""/></Tree><Skills activeSkillSet="1"><SkillSet id="1"/></Skills><Items activeItemSet="1"><ItemSet id="1"/></Items><Config><ConfigSet id="1"/></Config></PathOfBuilding2>'
	loadXMLIntoBuild(blankXml, charName)

	if build.importTab then
		if type(charData.passives) == "table" and build.importTab.ImportPassiveTreeAndJewels then
			build.importTab:ImportPassiveTreeAndJewels(charData)
		end
		if build.calcsTab and build.calcsTab.BuildOutput then
			build.calcsTab:BuildOutput()
		end
		if type(charData.equipment) == "table" and build.importTab.ImportItemsAndSkills then
			build.importTab:ImportItemsAndSkills(charData)
		end
	else
		error("PoB2 build.importTab is unavailable", 0)
	end

	local lv = tonumber(charData.level)
	if lv and lv >= 1 and lv <= 100 then
		build.characterLevel = lv
		build.characterLevelAutoMode = false
		if build.controls and build.controls.characterLevelInput then
			build.controls.characterLevelInput:SetText(tostring(lv))
		end
	end
	rebuildAndSnapshot(entry)
	return extractStatsTable()
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
-- Main JSON-RPC 2.0 stdin/stdout Loop (with POSIX poll + getppid orphan guard)
-- ============================================================================

local stdinPendingBuf = ""
local stdinEof = false
local stdinPollFd = (ffi.os ~= "Windows") and ffi.new("struct pob2_pollfd[1]") or nil
local stdinReadBuf = (ffi.os ~= "Windows") and ffi.new("uint8_t[65536]") or nil

local function readStdinLine()
	if ffi.os == "Windows" then
		return io.read("*l")
	end
	while true do
		local nlPos = stdinPendingBuf:find("\n", 1, true)
		if nlPos then
			local line = stdinPendingBuf:sub(1, nlPos - 1)
			if line:sub(-1) == "\r" then
				line = line:sub(1, -2)
			end
			stdinPendingBuf = stdinPendingBuf:sub(nlPos + 1)
			return line
		end
		if stdinEof then
			if stdinPendingBuf ~= "" then
				local tail = stdinPendingBuf
				stdinPendingBuf = ""
				return tail
			end
			return nil
		end

		checkParentAlive()
		stdinPollFd[0].fd = 0
		stdinPollFd[0].events = 1 -- POLLIN
		stdinPollFd[0].revents = 0
		local rc = ffi.C.poll(stdinPollFd, 1, 250)
		checkParentAlive()

		if rc > 0 then
			local n = tonumber(ffi.C.read(0, stdinReadBuf, 65536)) or -1
			if n == 0 then
				stdinEof = true
			elseif n > 0 then
				stdinPendingBuf = stdinPendingBuf .. ffi.string(stdinReadBuf, n)
			else
				local errNo = ffi.errno()
				if errNo ~= 4 and errNo ~= 11 and errNo ~= 35 then
					stdinEof = true
				end
			end
		elseif rc < 0 then
			if ffi.errno() ~= 4 then
				stdinEof = true
			end
		end
	end
end

while not shouldExit do
	checkParentAlive()
	local line = readStdinLine()
	if line == nil then
		stdout:flush()
		stderr:flush()
		os.exit(0)
	end
	checkParentAlive()
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
				checkParentAlive()
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

stdout:flush()
stderr:flush()
os.exit(0)
