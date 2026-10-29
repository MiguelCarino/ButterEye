-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 The ButterEye contributors
--
-- ButterEye helper for mpv (SCOPE F8, minimal M1 subset). Written from scratch.
--
--   Alt+b   turn interpolation on/off
--   Alt+B   show ButterEye status, frame counters and the licence notice
--   Alt+c   A/B view: source on the left half, smoothed on the right
--
-- Rebind in input.conf with:  KEY script-binding buttereye/toggle
--                             KEY script-binding buttereye/status
--                             KEY script-binding buttereye/compare
--
-- The toggle asks ButterEye first (script-message "buttereye-request toggle").
-- If ButterEye does not acknowledge within 0.6 s (for example because its
-- window was closed), the helper flips the @buttereye filter itself, so the
-- key keeps working without ButterEye. It never adds a filter of its own.
-- The A/B view is ButterEye's ("buttereye-request compare"): it builds the
-- split into the filter, so without ButterEye the key only says so.
--
-- Start videos smooth (ButterEye's "preload" setting, buttereye-preload=N in
-- script-opts): mpv starts paused, and this script holds the first picture of
-- each file until ButterEye says smoothing is ready ("preload-release"), the
-- user unpauses (ButterEye is told: "buttereye-request preload-cancel"), or
-- N + 2 seconds pass, so mpv is never left paused without ButterEye. A file
-- the user had paused is not held ("preload-cancel paused"). Once ButterEye
-- disconnects ("preload-off") or misses the limit, later files are not held.
-- The pause is this script's; ButterEye never sets it.
--
-- ButterEye talks to this script with:
--   script-message-to buttereye status <text> [quiet]
--   script-message-to buttereye ack <what>
--   script-message-to buttereye preload-release
--   script-message-to buttereye preload-off

local mp = require("mp")
local options = require("mp.options")

local LICENCE = "ButterEye is free software (GNU AGPL v3 or later) and comes with "
    .. "NO WARRANTY. Open ButterEye → About, or run 'buttereye-gui --version', for "
    .. "details and the source."
local ACK_TIMEOUT = 0.6
-- the hold outlasts ButterEye's own limit by this much (launcher.PRELOAD_GRACE_S)
local PRELOAD_GRACE = 2

local opts = { preload = 0, readahead = 0 }
options.read_options(opts, "buttereye")

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

local function compare()
    if own_filter() == nil then
        osd("ButterEye: the A/B view needs smoothing on.")
        return
    end
    mp.commandv("script-message", "buttereye-request", "compare")
end

mp.add_key_binding("Alt+b", "toggle", toggle)
mp.add_key_binding("Alt+B", "status", show_status)
mp.add_key_binding("Alt+c", "compare", compare)

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

-- ---- start videos smooth ----
local holding = false
local hold_timer = nil
local hold_files = opts.preload > 0 -- false once nobody is left to release them

local function stop_timer()
    if hold_timer ~= nil then
        hold_timer:kill()
        hold_timer = nil
    end
end

local function release(why)
    if not holding then
        return
    end
    holding = false
    stop_timer()
    mp.msg.verbose("starting playback: " .. why)
    mp.set_property_bool("pause", false)
end

local function hold()
    holding = true
    stop_timer()
    hold_timer = mp.add_timeout(opts.preload + PRELOAD_GRACE, function()
        hold_timer = nil
        hold_files = false
        release("ButterEye did not answer in time")
    end)
end

if opts.preload > 0 then
    -- read ahead at least as far as ButterEye asks; a longer setting of the
    -- user's own stays
    if opts.readahead > (mp.get_property_number("demuxer-readahead-secs") or 0) then
        mp.set_property_number("demuxer-readahead-secs", opts.readahead)
    end

    -- ButterEye started mpv with --pause: the first file is held already
    if mp.get_property_bool("pause") then
        hold()
    end

    -- every later file: hold again unless the user had paused
    mp.add_hook("on_preloaded", 50, function()
        if not hold_files then
            mp.commandv("script-message", "buttereye-request", "preload-cancel", "off")
        elseif holding then
            hold() -- the limit counts from this file
        elseif not mp.get_property_bool("pause") then
            hold()
            mp.set_property_bool("pause", true)
        else
            -- the user had paused: nothing is held, ButterEye stops waiting
            mp.commandv("script-message", "buttereye-request", "preload-cancel", "paused")
        end
    end)

    mp.observe_property("pause", "bool", function(_, paused)
        if holding and paused == false then
            -- the user started playback: stop holding, and tell ButterEye
            holding = false
            stop_timer()
            mp.commandv("script-message", "buttereye-request", "preload-cancel")
        end
    end)
end

mp.register_script_message("preload-release", function()
    release("smoothing is ready")
end)

mp.register_script_message("preload-off", function()
    hold_files = false
    release("ButterEye disconnected")
end)
