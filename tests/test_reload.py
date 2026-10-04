"""Behaviour of the reload script, checked against a real headless mpv.

Tests that currently fail because of a known bug are listed in
known_failing.py; run them through run.py, not plain unittest.
"""
import threading
import unittest

import mpvtest
from mpvtest import Mpv, MediaServer, media, wait_until, reload_position

# Timeouts short enough for tests; the script's defaults are 10 s and 20 s.
FAST = {
    "paused_for_cache_timer_interval": 0.5,
    "paused_for_cache_timer_timeout": 2,
    "demuxer_cache_timer_interval": 0.5,
    "demuxer_cache_timer_timeout": 2,
}

PLAYLIST = """#EXTM3U
#EXTINF:60,Alpha Channel
{base}/m1.mkv
#EXTINF:60,Beta Channel
{base}/m2.mkv
#EXTINF:60,Gamma Channel
{base}/m3.mkv
"""


class ReloadCase(unittest.TestCase):
    def serve(self, **kw):
        server = MediaServer(media("media.mkv"), **kw)
        self.addCleanup(server.close)
        return server

    def start(self, script_opts=None):
        mpv = Mpv(script_opts)
        self.addCleanup(mpv.close)
        return mpv

    def play(self, mpv, url, seek=None):
        mpv.cmd("loadfile", url)
        wait_until(lambda: mpv.count("file-loaded") >= 1 and mpv.get("duration"),
                   "the first file to load", mpv=mpv)
        if seek is not None:
            mpv.seek(seek)
            wait_until(lambda: (mpv.get("time-pos") or 0) >= seek - 0.5,
                       "the seek to %ss" % seek, mpv=mpv)

    def reload_and_wait(self, mpv):
        """Trigger a manual reload; return once the script has handled it and
        mpv has loaded the file again (not merely once the key was sent)."""
        loads = mpv.count("file-loaded")
        reloads = mpv.log_count("reloading")
        mpv.reload()
        wait_until(lambda: mpv.log_count("reloading") > reloads,
                   "the script to handle the reload", mpv=mpv)
        self.wait_loaded(mpv, loads + 1)

    def wait_loaded(self, mpv, loads):
        wait_until(lambda: mpv.count("file-loaded") >= loads,
                   "file-loaded #%d" % loads, mpv=mpv)

    def assert_resumed_at(self, mpv, expected):
        """Right after a reload, playback must be near `expected` seconds, not
        back at 0. Checked immediately, before playback could catch up from 0
        to the expected position and hide a lost start offset."""
        self.assertGreater(expected, 2, "position too small to tell a resume from a restart")
        position = wait_until(lambda: mpv.get("time-pos"), "a playback position", mpv=mpv)
        self.assertGreaterEqual(
            position, expected - 1,
            "playback restarted at %.1fs instead of resuming at %.1fs" % (position, expected))
        self.assertLessEqual(
            position, expected + 3,
            "playback resumed at %.1fs, too far after %.1fs" % (position, expected))


class ReloadTests(ReloadCase):
    def test_manual_reload_keeps_position(self):
        server = self.serve()
        mpv = self.start()
        self.play(mpv, server.url(), seek=20)
        before = mpv.get("time-pos")
        self.reload_and_wait(mpv)
        self.assert_resumed_at(mpv, before)
        self.assertEqual(mpv.log_count("reloading video from"), 1)
        self.assertAlmostEqual(reload_position(mpv), before, delta=2)

    def test_loadfile_syntax_fallback(self):
        # mpv 0.38 added an `index` argument to loadfile. The script tries the
        # new form first and falls back to the old one on older mpv. Either way
        # the reload must still resume where it was.
        server = self.serve()
        mpv = self.start()
        self.play(mpv, server.url(), seek=10)
        before = mpv.get("time-pos")
        self.reload_and_wait(mpv)
        self.assert_resumed_at(mpv, before)
        used_fallback = "old loadfile syntax detected" in mpv.log()
        if mpvtest.mpv_version() < (0, 38):
            self.assertTrue(used_fallback, "mpv < 0.38 should use the old loadfile syntax")
        else:
            self.assertFalse(used_fallback, "mpv >= 0.38 should accept the new loadfile syntax")

    def stalled_reload(self, script_opts):
        server = self.serve(rate=0.02)
        server.stall_at, server.stalls_left = 60000, 1
        mpv = self.start(script_opts)
        mpv.cmd("loadfile", server.url())
        wait_until(lambda: mpv.count("file-loaded") >= 2, "an automatic reload",
                   timeout=30, mpv=mpv)
        self.assertEqual(server.stalls_hit, 1)
        resume = reload_position(mpv)
        self.assertIsNotNone(resume, "expected a 'reloading video from' log line")
        self.assert_resumed_at(mpv, resume)
        return mpv

    def test_stalled_download_reloads_via_demuxer_cache(self):
        # Only the demuxer-cache path may trigger the reload: the paused-for-cache
        # timer is pushed out of reach so a slow runner cannot fall back to it, and
        # the demuxer timeout is short so it is "stuck" well before the buffer runs
        # dry (the script only checks that state when playback pauses for cache).
        mpv = self.stalled_reload(dict(FAST, demuxer_cache_timer_timeout=1,
                                       paused_for_cache_timer_timeout=30))
        self.assertIn("demuxer cache has no progress", mpv.log())

    def test_stalled_download_reloads_via_paused_for_cache_timer(self):
        mpv = self.stalled_reload(dict(FAST, demuxer_cache_timer_enabled="no"))
        self.assertNotIn("demuxer cache has no progress", mpv.log())

    def test_reload_at_eof_checks_for_more_content(self):
        server = MediaServer(media("short.mkv", seconds=5))
        self.addCleanup(server.close)
        mpv = self.start({"reload_eof_enabled": "yes"})
        mpv.cmd("loadfile", server.url())
        wait_until(lambda: "eof reached, playback ended" in mpv.log(),
                   "playback to end after the eof reload", timeout=30, mpv=mpv)
        self.assertIn("eof reached, checking if more content available", mpv.log())
        self.assertGreaterEqual(mpv.count("file-loaded"), 2)


class PlaylistTests(ReloadCase):
    def start_playlist(self):
        server = self.serve()
        server.texts["/list.m3u"] = PLAYLIST
        mpv = self.start()
        mpv.cmd("loadfile", server.url("list.m3u"))
        wait_until(lambda: mpv.get("playlist/count") == 3, "the playlist to load", mpv=mpv)
        mpv.cmd("playlist-play-index", 1)
        wait_until(lambda: mpv.get("duration"), "entry 2 to start", mpv=mpv)
        mpv.seek(15)
        wait_until(lambda: (mpv.get("time-pos") or 0) >= 14.5, "the seek", mpv=mpv)
        return mpv

    def names(self, mpv):
        return [(mpv.get("playlist/%d/filename" % i) or "").rsplit("/", 1)[-1]
                for i in range(mpv.get("playlist/count") or 0)]

    def titles(self, mpv):
        return [mpv.get("playlist/%d/title" % i)
                for i in range(mpv.get("playlist/count") or 0)]

    def reload_playlist(self, mpv):
        """Reload, then wait until the script has put the whole playlist back."""
        self.reload_and_wait(mpv)
        wait_until(lambda: self.names(mpv) == ["m1.mkv", "m2.mkv", "m3.mkv"],
                   "the playlist to be rebuilt in order", mpv=mpv)

    def test_playlist_order_and_position_survive_reload(self):
        mpv = self.start_playlist()
        before = mpv.get("time-pos")
        self.reload_playlist(mpv)
        self.assertEqual(mpv.get("playlist-pos"), 1)
        self.assert_resumed_at(mpv, before)

    def test_playlist_titles_survive_reload(self):
        # Issue #23: entries come back without their #EXTINF titles.
        mpv = self.start_playlist()
        expected = ["Alpha Channel", "Beta Channel", "Gamma Channel"]
        self.assertEqual(self.titles(mpv), expected)
        self.reload_playlist(mpv)
        self.assertEqual(self.titles(mpv), expected, "playlist titles lost after reload (#23)")


class RaceTests(ReloadCase):
    def test_second_reload_while_first_is_loading_keeps_position(self):
        # Issue #21: while a reload is still opening the file, `duration` is
        # unavailable, so a second reload takes the live-stream branch and drops
        # the start position. The server holds the first reload in flight.
        server = self.serve()
        mpv = self.start()
        self.play(mpv, server.url(), seek=30)
        position = mpv.get("time-pos")
        server.gate = threading.Event()
        mpv.reload()
        wait_until(lambda: server.waiting >= 1, "the first reload to be in flight", mpv=mpv)
        mpv.reload()
        wait_until(lambda: mpv.log_count("reloading") >= 2,
                   "the script to handle the second reload", mpv=mpv)
        server.gate.set()
        wait_until(lambda: mpv.get("duration") and mpv.get("time-pos") is not None,
                   "the file to finish loading", mpv=mpv)
        self.assertGreaterEqual(
            mpv.get("time-pos"), position - 1,
            "position lost after second reload during a reload (#21)")


if __name__ == "__main__":
    unittest.main()
