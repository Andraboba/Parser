# -*- coding: utf-8 -*-
"""Разбор HLS-плейлистов: master, media, диапазоны байт, отказ при шифровании."""

import unittest

from agent.hls import HlsError, MediaPlaylist, Segment, is_master, parse_master, parse_media


MASTER = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio/mp4a",NAME="French",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:PROGRAM-ID=0,BANDWIDTH=1190320,RESOLUTION=640x360,AUDIO="audio/mp4a"
media.m3u8?quality=360&type=video
#EXT-X-STREAM-INF:PROGRAM-ID=0,BANDWIDTH=3648600,RESOLUTION=1280x720,AUDIO="audio/mp4a"
https://cdn.example/media.m3u8?quality=720&type=video
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4",BYTERANGE="794@0"
#EXTINF:4.0,
#EXT-X-BYTERANGE:230444@794
media.mp4
#EXTINF:4.0,
#EXT-X-BYTERANGE:200000
media.mp4
#EXT-X-ENDLIST
"""


class MasterTest(unittest.TestCase):
    def test_variants_and_relative_uri(self) -> None:
        variants = parse_master(MASTER, "https://kinescope.io/abc/master.m3u8")
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0].uri, "https://kinescope.io/abc/media.m3u8?quality=360&type=video")
        self.assertEqual((variants[0].width, variants[0].height), (640, 360))
        self.assertEqual(variants[0].bandwidth, 1190320)
        self.assertEqual(variants[1].uri, "https://cdn.example/media.m3u8?quality=720&type=video")
        self.assertEqual(variants[1].quality, "720p")

    def test_not_master(self) -> None:
        self.assertFalse(is_master(MEDIA))
        self.assertEqual(parse_master(MEDIA, "https://example/"), [])


class MediaTest(unittest.TestCase):
    def test_init_segments_and_byteranges(self) -> None:
        playlist = parse_media(MEDIA, "https://kinescope.io/abc/master.m3u8")
        self.assertIsInstance(playlist, MediaPlaylist)
        self.assertIsNotNone(playlist.init)
        assert playlist.init is not None
        self.assertEqual(playlist.init.uri, "https://kinescope.io/abc/init.mp4")
        self.assertEqual(playlist.init.byte_range, "bytes=0-793")
        self.assertEqual(len(playlist.segments), 2)
        self.assertEqual(playlist.segments[0].byte_range, "bytes=794-231237")
        # Второй диапазон без @ продолжается сразу за первым.
        self.assertEqual(playlist.segments[1].byte_range, "bytes=231238-431237")
        self.assertTrue(playlist.is_fragmented_mp4)
        self.assertAlmostEqual(playlist.duration, 8.0)
        self.assertEqual(playlist.total_bytes, 794 + 230444 + 200000)

    def test_playlist_without_byterange(self) -> None:
        text = "#EXTM3U\n#EXTINF:4,\nseg1.ts\n#EXTINF:4,\nseg2.ts\n"
        playlist = parse_media(text, "https://example.com/hls/index.m3u8")
        self.assertEqual(len(playlist.segments), 2)
        self.assertIsNone(playlist.total_bytes)
        self.assertEqual(playlist.segments[0].uri, "https://example.com/hls/seg1.ts")
        self.assertFalse(playlist.is_fragmented_mp4)

    def test_encrypted_rejected(self) -> None:
        text = (
            "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
            "#EXTINF:4,\nseg.ts\n"
        )
        with self.assertRaises(HlsError):
            parse_media(text, "https://example.com/")

    def test_empty_rejected(self) -> None:
        with self.assertRaises(HlsError):
            parse_media("#EXTM3U\n#EXT-X-ENDLIST\n", "https://example.com/")


class SegmentTest(unittest.TestCase):
    def test_byte_range(self) -> None:
        self.assertEqual(Segment("u", 10, 20).byte_range, "bytes=10-29")
        self.assertIsNone(Segment("u").byte_range)


if __name__ == "__main__":
    unittest.main()
