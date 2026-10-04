-- main.lua runs inside mpv, which provides the `mp` global.
std = "luajit"
read_globals = { "mp" }

-- The script defines its handlers as globals.
globals = {
  "on_file_loaded",
  "read_settings",
  "reload",
  "reload_eof",
  "reload_resume",
  "round",
}

-- Observer and event callbacks receive arguments the script does not use.
ignore = { "212" }

max_line_length = false
exclude_files = { "tests/.cache" }
