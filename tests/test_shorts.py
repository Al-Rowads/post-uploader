import unittest
from dataclasses import replace
from fractions import Fraction

from post_uploader.media import MediaError, Video, video_from_probe


class ShortsEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.video = Video(100, 30, 1080, 1920, 30, "h264", "mov,mp4")

    def test_square_and_portrait_including_three_minutes_are_eligible(self):
        for width, height in ((1080, 1920), (1080, 1080)):
            for duration in (0.1, 60, 180):
                with self.subTest(width=width, height=height, duration=duration):
                    video = replace(self.video, width=width, height=height, duration=duration)
                    self.assertIsNone(video.youtube_shorts_error())

    def test_invalid_durations_are_rejected_with_resend_instructions(self):
        for duration in (0, -1, 180.001, 600, float("nan"), float("inf")):
            with self.subTest(duration=duration):
                error = replace(self.video, duration=duration).youtube_shorts_error()
                self.assertIn("180 seconds", error)
                self.assertIn("resend", error)

    def test_landscape_rejection_reports_display_ratio(self):
        video = replace(self.video, width=1920, height=1080)
        self.assertIn("16:9", video.youtube_shorts_error())
        self.assertIn("resend", video.youtube_shorts_error())
        self.assertIsNone(video.tiktok_error(600))

    def test_rotation_changes_display_orientation_without_changing_encoded_dimensions(self):
        for rotation in (-270, -90, 90, 270, 450):
            with self.subTest(rotation=rotation):
                landscape = replace(self.video, width=1920, height=1080, rotation=rotation)
                self.assertIsNone(landscape.youtube_shorts_error())
                self.assertEqual((landscape.width, landscape.height), (1920, 1080))
                self.assertIsNotNone(replace(self.video, rotation=rotation).youtube_shorts_error())
        for rotation in (0, 180, 360):
            self.assertIsNone(replace(self.video, rotation=rotation).youtube_shorts_error())

    def test_pixel_aspect_ratio_and_rotation_are_combined(self):
        video = replace(self.video, width=720, height=1080, sample_aspect_ratio=Fraction(2))
        self.assertIn("4:3", video.youtube_shorts_error())
        self.assertIsNone(replace(video, rotation=90).youtube_shorts_error())
        square = replace(video, sample_aspect_ratio=Fraction(3, 2))
        self.assertIsNone(square.youtube_shorts_error())
        self.assertIsNone(replace(square, rotation=90).youtube_shorts_error())

    def test_unverifiable_geometry_is_rejected(self):
        for changes in (
            {"width": 0},
            {"height": -1},
            {"sample_aspect_ratio": Fraction(0)},
            {"rotation": 45},
            {"rotation": float("nan")},
            {"rotation": float("inf")},
        ):
            with self.subTest(changes=changes):
                self.assertIn("resend", replace(self.video, **changes).youtube_shorts_error())


class ProbeMetadataTests(unittest.TestCase):
    def parse(self, **stream_fields):
        return video_from_probe(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "avg_frame_rate": "30/1",
                        **stream_fields,
                    }
                ],
                "format": {"duration": "180.000", "format_name": "mov,mp4"},
            },
            100,
        )

    def test_display_matrix_takes_precedence_over_legacy_rotation_tag(self):
        video = self.parse(tags={"rotate": "0"}, side_data_list=[{"rotation": -90}])
        self.assertEqual(video.rotation, -90)
        self.assertIsNone(video.youtube_shorts_error())
        video = self.parse(tags={"rotate": "90"}, side_data_list=[{"rotation": 0}])
        self.assertIsNotNone(video.youtube_shorts_error())

    def test_legacy_rotation_tag_and_unrelated_side_data(self):
        video = self.parse(tags={"rotate": "270"}, side_data_list=[{"side_data_type": "other"}])
        self.assertIsNone(video.youtube_shorts_error())

    def test_missing_and_unspecified_sar_use_encoded_dimensions(self):
        self.assertEqual(self.parse().sample_aspect_ratio, 1)
        for sar in ("N/A", "0:1", "1:1"):
            with self.subTest(sar=sar):
                self.assertEqual(self.parse(sample_aspect_ratio=sar).sample_aspect_ratio, 1)

    def test_explicit_pixel_aspect_ratio_is_preserved(self):
        video = self.parse(width=720, height=1080, sample_aspect_ratio="2:1")
        self.assertEqual(video.sample_aspect_ratio, 2)
        self.assertIn("4:3", video.youtube_shorts_error())

    def test_malformed_metadata_is_rejected(self):
        for fields in (
            {"sample_aspect_ratio": "broken"},
            {"sample_aspect_ratio": "1:0"},
            {"sample_aspect_ratio": "-1:1"},
            {"sample_aspect_ratio": None},
            {"tags": {"rotate": "broken"}},
            {"width": 0},
            {"height": -1},
        ):
            with self.subTest(fields=fields), self.assertRaises(MediaError):
                self.parse(**fields)
