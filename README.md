# reload.lua

When an online video is stuck during buffering or got slow CDN source,
restarting often helps. This script provides automatic reloading of videos that
didn't have buffering progress for some time, keeping the current time position
while preserving entries in the playlist. It also adds `Ctrl+r` keybinding to
reload video manually.

## Install

Mpv reads its configuration from `MPV_HOME` directory. On Unix it is
`~/.config/mpv`, see [files](https://mpv.io/manual/stable/#files) section of
the manual for the Windows configuration files.

To install the script, copy `reload.lua` to the `MPV_HOME/scripts` directory.
To override default settings, create `reload.conf` file in the script-opts
directory `MPV_HOME/script-opts`.

NOTE: config file name should match the name of the script.

For configuration example you can also check
[4e6/dotfiles](https://github.com/4e6/dotfiles/tree/master/.config/mpv) repo.

## Settings

Default `reload.conf` settings:

```
# enable automatic reload on timeout
# when paused-for-cache event fired, we will wait
# paused_for_cache_timer_timeout sedonds and then reload the video
paused_for_cache_timer_enabled=yes

# checking paused_for_cache property interval in seconds,
# can not be less than 0.05 (50 ms)
paused_for_cache_timer_interval=1

# time in seconds to wait until reload
paused_for_cache_timer_timeout=10

# enable automatic reload based on demuxer cache
# if demuxer-cache-time property didn't change in demuxer_cache_timer_timeout
# time interval, the video will be reloaded as soon as demuxer cache depleated
demuxer_cache_timer_enabled=yes

# checking demuxer-cache-time property interval in seconds,
# can not be less than 0.05 (50 ms)
demuxer_cache_timer_interval=2

# if demuxer cache didn't receive any data during demuxer_cache_timer_timeout
# we decide that it has no progress and will reload the stream when
# paused_for_cache event happens
demuxer_cache_timer_timeout=20

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
`--msg-level='reload=debug'`. You may also need to add the `--no-msg-color`
option to make the debug logs visible if you are using a dark colorscheme in
terminal.

