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

-- global state stores properties between reloads
local property_path = nil
local property_time_pos = 0
local property_keep_open = nil

-- FSM managing the demuxer cache.
--
-- States:
--
-- * fetch - fetching new data
-- * stale - unable to fetch new data for time <  'demuxer_cache_timer_timeout'
-- * stuck - unable to fetch new data for time >= 'demuxer_cache_timer_timeout'
--
-- State transitions:
--
--    +---------------------------+
--    v                           |
-- +-------+     +-------+     +-------+
-- + fetch +<--->+ stale +---->+ stuck |
-- +-------+     +-------+     +-------+
--   |   ^         |   ^         |   ^
--   +---+         +---+         +---+
local demuxer_cache = {
  timer = nil,

  state = {
    name = 'uninitialized',
    demuxer_cache_time = 0,
    in_state_time = 0,
  },

  events = {
    continue_fetch = { name = 'continue_fetch', from = 'fetch', to = 'fetch' },
    continue_stale = { name = 'continue_stale', from = 'stale', to = 'stale' },
    continue_stuck = { name = 'continue_stuck', from = 'stuck', to = 'stuck' },
    fetch_to_stale = { name = 'fetch_to_stale', from = 'fetch', to = 'stale' },
    stale_to_fetch = { name = 'stale_to_fetch', from = 'stale', to = 'fetch' },
    stale_to_stuck = { name = 'stale_to_stuck', from = 'stale', to = 'stuck' },
    stuck_to_fetch = { name = 'stuck_to_fetch', from = 'stuck', to = 'fetch' },
  },

}

-- Always start with 'fetch' state
function demuxer_cache.reset_state()
  demuxer_cache.state = {
    name = demuxer_cache.events.continue_fetch.to,
    demuxer_cache_time = 0,
    in_state_time = 0,
  }
end

-- Has 'demuxer_cache_time' changed
function demuxer_cache.has_progress_since(t)
  return demuxer_cache.state.demuxer_cache_time ~= t
end

function demuxer_cache.is_state_fetch()
  return demuxer_cache.state.name == demuxer_cache.events.continue_fetch.to
end

function demuxer_cache.is_state_stale()
  return demuxer_cache.state.name == demuxer_cache.events.continue_stale.to
end

function demuxer_cache.is_state_stuck()
  return demuxer_cache.state.name == demuxer_cache.events.continue_stuck.to
end

function demuxer_cache.transition(event)
  if demuxer_cache.state.name == event.from then

    -- state setup
    demuxer_cache.state.demuxer_cache_time = event.demuxer_cache_time

    if event.name == 'continue_fetch' then
      demuxer_cache.state.in_state_time = demuxer_cache.state.in_state_time + event.interval
    elseif event.name == 'continue_stale' then
      demuxer_cache.state.in_state_time = demuxer_cache.state.in_state_time + event.interval
    elseif event.name == 'continue_stuck' then
      demuxer_cache.state.in_state_time = demuxer_cache.state.in_state_time + event.interval
    elseif event.name == 'fetch_to_stale' then
      demuxer_cache.state.in_state_time = 0
    elseif event.name == 'stale_to_fetch' then
      demuxer_cache.state.in_state_time = 0
    elseif event.name == 'stale_to_stuck' then
      demuxer_cache.state.in_state_time = 0
    elseif event.name == 'stuck_to_fetch' then
      demuxer_cache.state.in_state_time = 0
    end

    -- state transition
    demuxer_cache.state.name = event.to

    msg.debug('demuxer_cache.transition', event.name, utils.to_string(demuxer_cache.state))
  else
    msg.error(
      'demuxer_cache.transition',
      'illegal transition', event.name,
      'from state', demuxer_cache.state.name)
  end
end

function demuxer_cache.initialize(demuxer_cache_timer_interval)
  demuxer_cache.reset_state()
  demuxer_cache.timer = mp.add_periodic_timer(
    demuxer_cache_timer_interval,
    function()
      demuxer_cache.demuxer_cache_timer_tick(
        mp.get_property_native('demuxer-cache-time'),
        demuxer_cache_timer_interval)
    end
  )
end

-- If there is no progress of demuxer_cache_time in
-- settings.demuxer_cache_timer_timeout time interval switch state to
-- 'stuck' and switch back to 'fetch' as soon as any progress is made
function demuxer_cache.demuxer_cache_timer_tick(demuxer_cache_time, demuxer_cache_timer_interval)
  local event = nil
  local cache_has_progress = demuxer_cache.has_progress_since(demuxer_cache_time)

  -- I miss pattern matching so much
  if demuxer_cache.is_state_fetch() then
    if cache_has_progress then
      event = demuxer_cache.events.continue_fetch
    else
      event = demuxer_cache.events.fetch_to_stale
    end
  elseif demuxer_cache.is_state_stale() then
    if cache_has_progress then
      event = demuxer_cache.events.stale_to_fetch
    elseif demuxer_cache.state.in_state_time < settings.demuxer_cache_timer_timeout then
      event = demuxer_cache.events.continue_stale
    else
      event = demuxer_cache.events.stale_to_stuck
    end
  elseif demuxer_cache.is_state_stuck() then
    if cache_has_progress then
      event = demuxer_cache.events.stuck_to_fetch
    else
      event = demuxer_cache.events.continue_stuck
    end
  end

  event.demuxer_cache_time = demuxer_cache_time
  event.interval = demuxer_cache_timer_interval
  demuxer_cache.transition(event)
end


local paused_for_cache = {
  timer = nil,
  time = 0,
}

function paused_for_cache.reset_timer()
  msg.debug('paused_for_cache.reset_timer', paused_for_cache.time)
  if paused_for_cache.timer then
    paused_for_cache.timer:kill()
    paused_for_cache.timer = nil
    paused_for_cache.time = 0
  end
end

function paused_for_cache.start_timer(interval_seconds, timeout_seconds)
  msg.debug('paused_for_cache.start_timer', paused_for_cache.time)
  if not paused_for_cache.timer then
    paused_for_cache.timer = mp.add_periodic_timer(
      interval_seconds,
      function()
        paused_for_cache.time = paused_for_cache.time + interval_seconds
        if paused_for_cache.time >= timeout_seconds then
          paused_for_cache.reset_timer()
          reload_resume()
        end
        msg.debug('paused_for_cache', 'tick', paused_for_cache.time)
      end
    )
  end
end

function paused_for_cache.handler(property, is_paused)
  if is_paused then

    if demuxer_cache.is_state_stuck() then
      msg.info("demuxer cache has no progress")
      -- reset demuxer state to avoid immediate reload if
      -- paused_for_cache event triggered right after reload
      demuxer_cache.reset_state()
      reload_resume()
    end

    paused_for_cache.start_timer(
      settings.paused_for_cache_timer_interval,
      settings.paused_for_cache_timer_timeout)
  else
    paused_for_cache.reset_timer()
  end
end

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

  local playlist_count = mp.get_property_number("playlist/count")
  local playlist_pos = mp.get_property_number("playlist-pos")
  local playlist = {}
  for i = 0, playlist_count-1 do
      playlist[i] = mp.get_property("playlist/" .. i .. "/filename")
  end
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
  msg.info("file ", playlist_pos+1, " of ", playlist_count, "in playlist")
  for i = 0, playlist_pos-1 do
    mp.commandv("loadfile", playlist[i], "append")
  end
  mp.commandv("playlist-move", 0, playlist_pos+1)
  for i = playlist_pos+1, playlist_count-1 do
    mp.commandv("loadfile", playlist[i], "append")
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

-- main

read_settings()

if settings.reload_key_binding ~= "" then
  mp.add_key_binding(settings.reload_key_binding, "reload_resume", reload_resume)
end

if settings.paused_for_cache_timer_enabled then
  mp.observe_property("paused-for-cache", "bool", paused_for_cache.handler)
end

if settings.demuxer_cache_timer_enabled then
  demuxer_cache.initialize(settings.demuxer_cache_timer_interval)
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
    end
  )

  mp.observe_property("eof-reached", "bool", reload_eof)
end

--mp.register_event("file-loaded", debug_info)
