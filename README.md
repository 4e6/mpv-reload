# reload.lua

When an online video is stuck during buffering or got slow CDN source,
restarting often helps. This script provides automatic reloading of videos that
didn't have buffering progress for some time, keeping the current time
position. It also adds `Ctrl+r` keybinding to reload video manually.

## Install

Mpv reads its configuration from `MPV_HOME` directory. On Unix systems it is
`~/.config/mpv`, see [files](https://mpv.io/manual/stable/#files) section of
the manual for the Windows configuration files.

To install the script, copy `reload.lua` to the `MPV_HOME/scripts` directory.

To override default settings, create `reload.conf` file in the lua-settings
directory `MPV_HOME/lua-settings`.

NOTE: config file name should match the name of the script.

Default `reload.conf` settings:

```
# enable automatic reload on timeout
paused_for_cache_timer_enabled=yes

# checking paused_for_cache property interval in seconds,
# can not be less than 0.05 (50 ms)
paused_for_cache_timer_interval=1

# time in seconds to wait until reload
paused_for_cache_timer_timeout=10

# when the end-of-file is reached, reload the stream to check
# if there is more content available.
reload_eof_enabled=no

# keybinding to reload stream from current time position
# you can disable keybinding by setting it to empty value
# reload_key_binding=
reload_key_binding=Ctrl+r
```

## Debugging

Debug messages will be printed to stdout with mpv command line option
`--msg-level='reload=debug'`
