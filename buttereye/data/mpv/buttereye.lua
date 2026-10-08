-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 The ButterEye contributors
--
-- ButterEye helper for mpv (SCOPE F8, minimal M1 subset). Written from scratch.
--
--   Alt+b   turn interpolation on/off
--   Alt+B   show ButterEye status, frame counters and the licence notice
--
-- Rebind in input.conf with:  KEY script-binding buttereye/toggle
--                             KEY script-binding buttereye/status
--
-- The toggle asks ButterEye first (script-message "buttereye-request toggle").
-- If ButterEye does not acknowledge within 0.6 s (for example because its
-- window was closed), the helper flips the @buttereye filter itself, so the
-- key keeps working without ButterEye. It never adds a filter of its own.
--
-- ButterEye talks to this script with:
--   script-message-to buttereye status <text> [quiet]
--   script-message-to buttereye ack <what>

local mp = require("mp")

local LICENCE = "ButterEye is free software (GNU AGPL v3 or later) and comes with "
    .. "NO WARRANTY. Open ButterEye → About, or run 'buttereye-gui --version', for "
    .. "details and the source."
local ACK_TIMEOUT = 0.6

local status_text = "ButterEye: not connected"
local waiting = nil

local function osd(text, seconds)
    mp.osd_message(text, seconds or 3)
end

local function own_filter()
    local chain = mp.get_property_native("vf") or {}
    for _, entry in ipairs(chain) do
        if entry.label == "buttereye" then
            return entry
        end
    end
    return nil
end

local function toggle_locally()
    local entry = own_filter()
    if entry == nil then
        osd("ButterEye is not connected and no smoothing filter is active.")
        return
    end
    mp.command("vf toggle @buttereye")
    entry = own_filter()
    if entry ~= nil and entry.enabled then
        osd("ButterEye: interpolation on")
    else
        osd("ButterEye: interpolation off")
    end
end

local function toggle()
    if waiting ~= nil then
        waiting:kill()
    end
    mp.commandv("script-message", "buttereye-request", "toggle")
    waiting = mp.add_timeout(ACK_TIMEOUT, function()
        waiting = nil
        toggle_locally()
    end)
end

local function count(name)
    local value = mp.get_property_number(name)
    if value == nil then
        return "n/a"
    end
    return string.format("%d", value)
end

local function show_status()
    local lines = {
        status_text,
        "Dropped " .. count("frame-drop-count")
            .. " · decoder " .. count("decoder-frame-drop-count")
            .. " · delayed " .. count("vo-delayed-frame-count")
            .. " · mistimed " .. count("mistimed-frame-count"),
        LICENCE,
    }
    osd(table.concat(lines, "\n"), 6)
end

mp.add_key_binding("Alt+b", "toggle", toggle)
mp.add_key_binding("Alt+B", "status", show_status)

mp.register_script_message("ack", function()
    if waiting ~= nil then
        waiting:kill()
        waiting = nil
    end
end)

mp.register_script_message("status", function(text, mode)
    if text == nil or text == "" then
        return
    end
    status_text = text
    if mode ~= "quiet" then
        osd(text)
    end
end)
