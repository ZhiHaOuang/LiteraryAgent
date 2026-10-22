"""CLI and owned-worker argument boundaries; downstream execution is replaced."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lg_cli import cli_backend, story_cli, workflow_io
from lg_cli.main import build_parser


class StoryRequestArgumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = SimpleNamespace(workspace=Path(self.directory.name))

    def arguments(self, command='edit', *, stdin=True, passage=None, inline=None):
        argv = {'edit': ['edit', 'one', '--mode', 'light'], 'scene': ['scene', 'plan', 'one'],
                'bible': ['bible', 'curate', 'one']}[command]
        if stdin:
            argv += ['--request-stdin']
        if passage is not None:
            argv += ['--passage', passage]
        if inline is not None:
            argv += [inline]
        return build_parser().parse_args(argv)

    def invoke(self, entry, args, stream):
        captured = {}

        class Console:
            async def workflow(self, params):
                captured.update(params)
                return 0

        async def client(config, parsed, action):
            self.assertIs(config, self.config)
            self.assertIs(parsed, args)
            return await action(Console())

        def domain(parsed, config, store):
            self.assertIs(parsed, args)
            captured.update(request=' '.join(parsed.request), passage=parsed.passage,
                            revisionMode=parsed.mode, sourceVersion=parsed.source_version)
            return 0

        args.backend_worker = entry == 'worker'
        with patch('sys.stdin', stream), patch.object(cli_backend, '_with_client', side_effect=client), \
                patch.object(story_cli, '_dispatch_edit', side_effect=domain):
            value = (cli_backend.dispatch_story if entry == 'cli' else
                     story_cli.dispatch_story_command)(args, self.config)
        self.assertEqual(value, 0)
        return captured

    def test_both_entry_points_enforce_the_shared_argument_boundary(self):
        class BoundaryReached(Exception):
            pass
        # Red baseline: each entry still has its own implementation and bypasses
        # this single boundary. This does not replace parsing in behavior tests.
        for entry in ('cli', 'worker'):
            with self.subTest(entry=entry), patch.object(workflow_io, 'apply_story_request',
                    side_effect=BoundaryReached, create=True) as boundary:
                args = self.arguments()
                stream = io.StringIO('{"request":"test"}')
                with self.assertRaises(BoundaryReached):
                    self.invoke(entry, args, stream)
                boundary.assert_called_once_with(args, stream)

    def test_mixed_inline_request_rejects_before_reading_stdin(self):
        for entry in ('cli', 'worker'):
            for command in ('edit', 'scene', 'bible'):
                with self.subTest(entry=entry, command=command):
                    args = self.arguments(command, inline='author inline')
                    stream = io.StringIO('invalid JSON must not be read')
                    with self.assertRaises(ValueError) as error:
                        self.invoke(entry, args, stream)
                    self.assertEqual(str(error.exception), 'Use stdin request or command-line text, not both')
                    self.assertEqual(stream.tell(), 0)
                    self.assertEqual(args.request, ['author inline'])

    def test_inline_passage_even_empty_rejects_before_reading_stdin(self):
        for entry in ('cli', 'worker'):
            for passage in ('SELECTED', ''):
                with self.subTest(entry=entry, passage=passage):
                    args = self.arguments(passage=passage)
                    stream = io.StringIO('{"request":"test"}')
                    with self.assertRaises(ValueError) as error:
                        self.invoke(entry, args, stream)
                    self.assertEqual(str(error.exception), 'Use stdin request or command-line text, not both')
                    self.assertEqual(stream.tell(), 0)

    def test_non_edit_payload_passage_rejects_without_mutating_arguments(self):
        for entry in ('cli', 'worker'):
            for command in ('scene', 'bible'):
                with self.subTest(entry=entry, command=command):
                    args = self.arguments(command)
                    before = list(args.request)
                    with self.assertRaises(ValueError) as error:
                        self.invoke(entry, args, io.StringIO('{"request":"test","passage":"SELECTED"}'))
                    self.assertEqual(str(error.exception), 'Selected passage applies only to edit')
                    self.assertEqual(args.request, before)
                    self.assertFalse(hasattr(args, 'passage'))

    def test_invalid_envelopes_preserve_existing_errors_and_arguments(self):
        cases = [('not json', 'Story request must be a JSON object'),
                 ('[]', 'Invalid story request fields'),
                 ('{"request":"test","passage":""}', 'Invalid story request fields'),
                 ('{"request":"test","extra":true}', 'Invalid story request fields'),
                 ('x' * (4 * 1024 * 1024 + 1), 'Story request exceeds size limit')]
        for entry in ('cli', 'worker'):
            for payload, message in cases:
                with self.subTest(entry=entry, message=message, size=len(payload)):
                    args = self.arguments()
                    before = list(args.request)
                    with self.assertRaises(ValueError) as error:
                        self.invoke(entry, args, io.StringIO(payload))
                    self.assertEqual(str(error.exception), message)
                    self.assertEqual(args.request, before)
                    self.assertIsNone(args.passage)

    def test_edit_payload_preserves_unicode_whitespace_and_passage(self):
        for entry in ('cli', 'worker'):
            with self.subTest(entry=entry):
                args = self.arguments()
                request = '  作者🖋\n仅改语句  '
                passage = '选区\n保留换行'
                captured = self.invoke(entry, args, io.StringIO(json.dumps(
                    {'request': request, 'passage': passage}, ensure_ascii=False)))
                self.assertEqual(args.request, [request])
                self.assertEqual(args.passage, passage)
                self.assertEqual(captured['request'], request)
                self.assertEqual(captured['passage'], passage)

    def test_payload_without_passage_preserves_existing_edit_defaults(self):
        for entry in ('cli', 'worker'):
            with self.subTest(entry=entry):
                args = self.arguments()
                mode = args.mode
                captured = self.invoke(entry, args, io.StringIO('{"request":""}'))
                self.assertEqual(args.request, [''])
                self.assertIsNone(args.passage)
                self.assertEqual(captured['request'], '')
                self.assertEqual(captured['revisionMode'], mode)

    def test_without_stdin_flag_does_not_consume_stream_or_invoke_boundary(self):
        for entry in ('cli', 'worker'):
            with self.subTest(entry=entry):
                args = self.arguments(stdin=False, inline='author inline', passage='SELECTED')
                stream = io.StringIO('must remain unread')
                with patch.object(workflow_io, 'apply_story_request',
                        side_effect=AssertionError('Unexpected stdin consumption'), create=True):
                    captured = self.invoke(entry, args, stream)
                self.assertEqual(stream.tell(), 0)
                self.assertEqual(captured['request'], 'author inline')
                self.assertEqual(captured['passage'], 'SELECTED')
