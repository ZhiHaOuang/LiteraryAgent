import hashlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli import slime_animation as animation
from lg_cli import slime_animation_legacy as legacy


class SlimeVersionTests(unittest.TestCase):
    def test_original_source_is_preserved(self):
        digest = hashlib.sha256(Path(legacy.__file__).read_bytes()).hexdigest()
        self.assertEqual(
            digest, "ab8430e04cd62274a1a1af664b21d376d6cc6de1f1074cc16c6903740caf291a"
        )

    def test_rollback_is_pixel_exact_and_cache_is_separate(self):
        with patch.dict(os.environ, {"LG_ANIMATION": "smooth"}):
            smooth = [animation.frame_pixels(i) for i in range(animation.FRAME_COUNT)]
        with patch.dict(os.environ, {"LG_ANIMATION": "legacy"}):
            for i in range(animation.FRAME_COUNT):
                self.assertEqual(animation.frame_pixels(i), legacy.frame_pixels(i))
                self.assertEqual(
                    animation.slime_mark(i * animation.FRAME_INTERVAL + 0.001).plain,
                    legacy.slime_mark(i * animation.FRAME_INTERVAL + 0.001).plain,
                )
        with patch.dict(os.environ, {"LG_ANIMATION": "smooth"}):
            self.assertEqual(
                smooth,
                [animation.frame_pixels(i) for i in range(animation.FRAME_COUNT)],
            )
        self.assertTrue(
            any(frame != legacy.frame_pixels(i) for i, frame in enumerate(smooth))
        )

    def test_velocity_is_continuous_without_keyframe_stops(self):
        epsilon = 0.00001
        for t, _ in animation._KEYS[1:-1]:
            before = animation.pose_at(t - epsilon, mode="smooth")
            at = animation.pose_at(t, mode="smooth")
            after = animation.pose_at(t + epsilon, mode="smooth")
            for field in animation.Pose.__dataclass_fields__:
                left = (getattr(at, field) - getattr(before, field)) / epsilon
                right = (getattr(after, field) - getattr(at, field)) / epsilon
                self.assertAlmostEqual(left, right, delta=0.2, msg=f"{field} at {t}")
        for t in (0.65, 0.8, 0.95, 1.25, 1.45):
            before = animation.pose_at(t - epsilon, mode="smooth")
            after = animation.pose_at(t + epsilon, mode="smooth")
            self.assertLess((after.x - before.x) / (2 * epsilon), -1)

    def test_smooth_interpolation_preserves_keys_and_bounds(self):
        for i, (t, expected) in enumerate(animation._KEYS[:-1]):
            self.assertEqual(animation.pose_at(t, mode="smooth"), expected)
            end, next_pose = animation._KEYS[i + 1]
            for step in range(1, 20):
                pose = animation.pose_at(t + (end - t) * step / 20, mode="smooth")
                for field in animation.Pose.__dataclass_fields__:
                    a, b, value = (
                        getattr(expected, field),
                        getattr(next_pose, field),
                        getattr(pose, field),
                    )
                    self.assertLessEqual(min(a, b), value)
                    self.assertLessEqual(value, max(a, b))

    def test_invalid_mode_is_explicit(self):
        with (
            patch.dict(os.environ, {"LG_ANIMATION": "typo"}),
            self.assertRaisesRegex(ValueError, "LG_ANIMATION"),
        ):
            animation.slime_mark(0)
