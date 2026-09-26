import unittest
from itertools import pairwise

from lg_cli import slime_animation as animation
from lg_cli.slime_animation import (
    BODY,
    CROWN,
    DURATION,
    FRAME_COUNT,
    FRAME_INTERVAL,
    HEIGHT,
    WIDTH,
    face_pixels,
    frame_pixels,
    pose_at,
    slime_mark,
)


class SlimeAnimationTests(unittest.TestCase):
    def test_velocity_is_continuous_without_keyframe_stops(self):
        epsilon = 0.00001
        for t, _ in animation._KEYS[1:-1]:
            before = pose_at(t - epsilon)
            at = pose_at(t)
            after = pose_at(t + epsilon)
            for field in animation.Pose.__dataclass_fields__:
                left = (getattr(at, field) - getattr(before, field)) / epsilon
                right = (getattr(after, field) - getattr(at, field)) / epsilon
                self.assertAlmostEqual(left, right, delta=0.2, msg=f"{field} at {t}")
        for t in (0.65, 0.8, 0.95, 1.25, 1.45):
            self.assertLess(
                (pose_at(t + epsilon).x - pose_at(t - epsilon).x) / (2 * epsilon), -1
            )

    def test_interpolation_preserves_keys_and_bounds(self):
        for i, (t, expected) in enumerate(animation._KEYS[:-1]):
            self.assertEqual(pose_at(t), expected)
            end, next_pose = animation._KEYS[i + 1]
            for step in range(1, 20):
                pose = pose_at(t + (end - t) * step / 20)
                for field in animation.Pose.__dataclass_fields__:
                    a, b, value = (
                        getattr(expected, field),
                        getattr(next_pose, field),
                        getattr(pose, field),
                    )
                    self.assertLessEqual(min(a, b), value)
                    self.assertLessEqual(value, max(a, b))

    def test_loop_and_direction(self):
        self.assertAlmostEqual(FRAME_COUNT * FRAME_INTERVAL, DURATION)
        self.assertEqual(pose_at(0), pose_at(DURATION))
        self.assertEqual(frame_pixels(0), frame_pixels(FRAME_COUNT - 1))
        jump = [pose_at(0.5 + i * 0.05) for i in range(25)]
        self.assertTrue(all(a.x >= b.x for a, b in pairwise(jump)))
        self.assertEqual(jump[0].bottom, 22)
        self.assertEqual(jump[-1].bottom, 22)
        self.assertTrue(all(p.bottom < 22 for p in jump[1:-1]))
        slide = [pose_at(2.2 + i * 0.05) for i in range(13)]
        self.assertTrue(all(a.x <= b.x for a, b in pairwise(slide)))
        self.assertTrue(all(p.bottom == 22 for p in slide))
        self.assertTrue(all(p.width == 16 and p.height == 8 for p in slide))
        self.assertEqual(slide[0].x, 11)
        self.assertEqual(slide[-1].x, 20)

    def test_crown_follows_up_then_lags_and_settles(self):
        self.assertTrue(all(pose_at(i * 0.05).crown_gap == 0 for i in range(23)))
        self.assertGreater(pose_at(1.7).crown_gap, 3)
        self.assertEqual(pose_at(1.95).crown_gap, 0)
        self.assertGreater(pose_at(2.05).crown_gap, 0)
        self.assertEqual(pose_at(2.2).crown_gap, 0)

    def test_every_frame_has_visible_face_crown_and_no_clipping(self):
        crown_masks = []
        for index in range(FRAME_COUNT):
            with self.subTest(frame=index):
                pixels = frame_pixels(index)
                self.assertEqual(len(pixels), HEIGHT)
                self.assertTrue(all(len(row) == WIDTH for row in pixels))
                self.assertTrue(
                    all(row[0] is None and row[-1] is None for row in pixels)
                )
                crown = [
                    (x, y)
                    for y, row in enumerate(pixels)
                    for x, pixel in enumerate(row)
                    if pixel == CROWN
                ]
                self.assertGreater(len(crown), 20)
                x0, y0 = min(x for x, _ in crown), min(y for _, y in crown)
                crown_masks.append({(x - x0, y - y0) for x, y in crown})
                pose = pose_at(index * FRAME_INTERVAL)
                face = face_pixels(pose)
                for x, y in face:
                    self.assertIsNone(pixels[y][x])
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            if (x + dx, y + dy) not in face:
                                self.assertEqual(
                                    pixels[y + dy][x + dx],
                                    BODY,
                                    "Face must never touch the silhouette edge",
                                )
                x, y = face[-1]
                self.assertEqual(pixels[y + 1][x], BODY)
                self.assertEqual(pixels[y][x - 1], BODY)
                self.assertEqual(pixels[y][x + 1], BODY)
                self.assertLess(x, pose.x)
        self.assertTrue(all(mask == crown_masks[0] for mask in crown_masks))

    def test_flat_poses_are_only_brief_impacts(self):
        poses = [pose_at(i * FRAME_INTERVAL) for i in range(FRAME_COUNT)]
        flat = [pose for pose in poses if pose.height < 7]
        self.assertLessEqual(len(flat) * FRAME_INTERVAL, 0.25)
        self.assertGreater(max(p.height for p in poses), 9)
        self.assertTrue(any(p.lean < -0.15 for p in poses))

    def test_cached_frames_are_not_mutated_by_consumers(self):
        mark = slime_mark(0)
        original = mark.plain
        mark.append("changed")
        self.assertEqual(slime_mark(0).plain, original)

    def test_more_samples_without_speeding_up(self):
        self.assertEqual(FRAME_COUNT, 192)
        self.assertEqual(FRAME_INTERVAL, 1 / 60)
        self.assertEqual(DURATION, 3.2)
        delay = sum(
            round((i + 1) * FRAME_INTERVAL * 100) - round(i * FRAME_INTERVAL * 100)
            for i in range(FRAME_COUNT)
        )
        self.assertEqual(delay, 320)
