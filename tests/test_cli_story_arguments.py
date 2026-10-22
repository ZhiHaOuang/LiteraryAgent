import unittest

from lg_cli.main import build_parser


class StoryArgumentTests(unittest.TestCase):
    def test_request_can_follow_story_flags_without_becoming_an_unknown_option(self):
        for command in (['scene','plan','gate','--dry-run','只规划城门'],
                        ['edit','one','--mode','grammar','fix continuity'],
                        ['bible','curate','one','--dry-run','extract supported facts']):
            with self.subTest(command=command):
                args=build_parser().parse_args(command)
                self.assertEqual(args.request,[command[-1]])

    def test_mixed_stdin_request_remains_visible_for_the_existing_rejection(self):
        args=build_parser().parse_args(['edit','one','--mode','grammar','--request-stdin','mixed-request'])
        self.assertTrue(args.request_stdin)
        self.assertEqual(args.request,['mixed-request'])

    def test_unknown_flags_or_unrelated_commands_are_never_treated_as_prose(self):
        for args in (['edit','one','--unknown','x'],['status','trailing']):
            with self.subTest(args=args),self.assertRaises(SystemExit):
                build_parser().parse_args(args)
