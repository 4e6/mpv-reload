-- reload.lua
--
-- When an online video is stuck buffering or got very slow CDN
-- source, restarting often helps. This script provides automatic
-- reloading of videos that doesn't have buffering progress for some
-- time while keeping the current time position. It also adds `Ctrl+r`
-- keybinding to reload video manually.
--
-- SETTINGS
--
-- To override default setting put `lua-settings/reload.conf` file in
-- mpv user folder, on linux it is `~/.config/mpv`.  NOTE: config file
-- name should match the name of the script.
--
-- Default `reload.conf` settings:
--
-- ```
-- # enable automatic reload on timeout
-- # when paused-for-cache event fired, we will wait
-- # paused_for_cache_timer_timeout sedonds and then reload the video
-- paused_for_cache_timer_enabled=yes
--
-- # checking paused_for_cache property interval in seconds,
-- # can not be less than 0.05 (50 ms)
-- paused_for_cache_timer_interval=1
--
-- # time in seconds to wait until reload
-- paused_for_cache_timer_timeout=10
--
-- # enable automatic reload based on demuxer cache
-- # if demuxer-cache-time property didn't change in demuxer_cache_timer_timeout
-- # time interval, the video will be reloaded as soon as demuxer cache depleated
-- demuxer_cache_timer_enabled=yes
--
-- # checking demuxer-cache-time property interval in seconds,
-- # can not be less than 0.05 (50 ms)
-- demuxer_cache_timer_interval=2
--
-- # if demuxer cache didn't receive any data during demuxer_cache_timer_timeout
-- # we decide that it has no progress and will reload the stream when
-- # paused_for_cache event happens
-- demuxer_cache_timer_timeout=20
--
-- # when the end-of-file is reached, reload the stream to check
-- # if there is more content available.
-- reload_eof_enabled=no
--
-- # keybinding to reload stream from current time position
-- # you can disable keybinding by setting it to empty value
-- # reload_key_binding=
-- reload_key_binding=Ctrl+r
-- ```
--
-- DEBUGGING
--
-- Debug messages will be printed to stdout with mpv command line option
-- `--msg-level='reload=debug'`

local msg = require 'mp.msg'
local options = require 'mp.options'
local utils = require 'mp.utils'


local settings = {
  paused_for_cache_timer_enabled = true,
  paused_for_cache_timer_interval = 1,
  paused_for_cache_timer_timeout = 10,
  demuxer_cache_timer_enabled = true,
  demuxer_cache_timer_interval = 2,
  demuxer_cache_timer_timeout = 20,
  reload_eof_enabled = false,
  reload_key_binding = "Ctrl+r",
}

local demuxer_cache = {
  timer = nil,
  time = 0,
  has_progress = true,
  no_progress_time = 0,
}

-- global state stores properties between reloads
local paused_for_cache_seconds = 0
local paused_for_cache_timer = nil
local property_path = nil
local property_time_pos = 0
local property_keep_open = nil

function read_settings()
  options.read_options(settings, mp.get_script_name())
  msg.debug(utils.to_string(settings))
end

function debug_info(event)
  msg.debug("event =", utils.to_string(event))
  msg.debug("path =", mp.get_property("path"))
  msg.debug("time-pos =", mp.get_property("time-pos"))
  msg.debug("paused-for-cache =", mp.get_property("paused-for-cache"))
  msg.debug("stream-path =", mp.get_property("stream-path"))
  msg.debug("stream-pos =", mp.get_property("stream-pos"))
  msg.debug("stream-end =", mp.get_property("stream-end"))
  msg.debug("duration =", mp.get_property("duration"))
  msg.debug("seekable =", mp.get_property("seekable"))
end

function reload(path, time_pos)
  msg.debug("reload", path, time_pos)
  if time_pos == nil then
    mp.commandv("loadfile", path, "replace")
  else
    mp.commandv("loadfile", path, "replace", "start=+" .. time_pos)
  end
end

function reload_resume()
  local path = mp.get_property("path", property_path)
  local time_pos = mp.get_property("time-pos")
  local reload_duration = mp.get_property_native("duration")

  -- Tries to determine live stream vs. pre-recordered VOD. VOD has non-zero
  -- duration property. When reloading VOD, to keep the current time position
  -- we should provide offset from the start. Stream doesn't have fixed start.
  -- Decent choice would be to reload stream from it's current 'live' positon.
  -- That's the reason we don't pass the offset when reloading streams.
  if reload_duration and reload_duration > 0 then
    msg.info("reloading video from", time_pos, "second")
    reload(path, time_pos)
  else
    msg.info("reloading stream")
    reload(path, nil)
  end
end

function reload_eof(property, eof_reached)
  msg.debug("reload_eof", property, eof_reached)
  local time_pos = mp.get_property_number("time-pos")
  local duration = mp.get_property_number("duration")

  if eof_reached and math.floor(time_pos) == math.floor(duration) then
    msg.debug("property_time_pos", property_time_pos, "time_pos", time_pos)

    -- Check that playback time_pos made progress after the last reload. When
    -- eof is reached we try to reload video, in case there is more content
    -- available. If time_pos stayed the same after reload, it means that vidkk
    -- to avoid infinite reload loop when playback ended
    -- math.floor function rounds time_pos to a second, to avoid inane reloads
    if math.floor(property_time_pos) == math.floor(time_pos) then
      msg.info("eof reached, playback ended")
      mp.set_property("keep-open", property_keep_open)
    else
      msg.info("eof reached, checking if more content available")
      reload_resume()
      mp.set_property_bool("pause", false)
      property_time_pos = time_pos
    end
  end
end

function reset_timer()
  msg.debug("reset_timer; paused_for_cache_seconds =", paused_for_cache_seconds)
  if paused_for_cache_timer then
    paused_for_cache_timer:kill()
    paused_for_cache_timer = nil
    paused_for_cache_seconds = 0
  end
end

function start_timer(interval_seconds, timeout_seconds)
  msg.debug("start_timer")
  paused_for_cache_timer = mp.add_periodic_timer(
    interval_seconds,
    function()
      paused_for_cache_seconds = paused_for_cache_seconds + interval_seconds
      if paused_for_cache_seconds >= timeout_seconds then
        reset_timer()
        reload_resume()
      end
  end)
end

function paused_for_cache_handler(property, is_paused)
  if is_paused then
    if not demuxer_cache.has_progress then
      msg.info("demuxer cache has no progress")
      -- reset demuxer_cache timer to avoid immediate reload if
      -- paused_for_cache event triggered right after reload
      demuxer_cache.has_progress = true
      reload_resume()
    end

    if not paused_for_cache_timer then
      start_timer(
        settings.paused_for_cache_timer_interval,
        settings.paused_for_cache_timer_timeout)
    end
  else
    if paused_for_cache_timer then
      reset_timer()
    end
  end
end

-- main

read_settings()

if settings.reload_key_binding ~= "" then
  mp.add_key_binding(settings.reload_key_binding, "reload_resume", reload_resume)
end

if settings.paused_for_cache_timer_enabled then
  mp.observe_property("paused-for-cache", "bool", paused_for_cache_handler)
end

if settings.demuxer_cache_timer_enabled then
  -- if there is no progress of demuxer-cache-time in settings.demuxer_cache_time_duration
  -- set demuxer_cache.has_progress = false and
  -- set back to true as soon as any progress is made
  demuxer_cache.timer = mp.add_periodic_timer(
    settings.demuxer_cache_timer_interval,
    function()
      demuxer_cache_time = mp.get_property_native("demuxer-cache-time")
      demuxer_has_progress = demuxer_cache.time ~= demuxer_cache_time
      demuxer_cache.time = demuxer_cache_time

      if not demuxer_has_progress then
        demuxer_cache.no_progress_time = demuxer_cache.no_progress_time + settings.demuxer_cache_timer_interval
      else
        demuxer_cache.no_progress_time = 0
      end

      demuxer_cache.has_progress = demuxer_cache.no_progress_time < settings.demuxer_cache_timer_timeout

      msg.debug("demuxer_cache", utils.to_string(demuxer_cache))
    end)
end

if settings.reload_eof_enabled then
  -- vo-configured == video output created && its configuration went ok
  mp.observe_property(
    "vo-configured",
    "bool",
    function(name, vo_configured)
      msg.debug(name, vo_configured)
      if vo_configured then
        property_path = mp.get_property("path")
        property_keep_open = mp.get_property("keep-open")
        mp.set_property("keep-open", "yes")
        mp.set_property("keep-open-pause", "no")
      end
    end)

  mp.observe_property("eof-reached", "bool", reload_eof)
end

--mp.register_event("file-loaded", debug_info)
