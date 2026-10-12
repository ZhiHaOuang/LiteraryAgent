"""Shared LG writing instructions for terminal and extension app-server sessions."""
from importlib import resources


def writing_instructions(config, *, control_socket=None):
    instructions = resources.files('lg_cli.resources').joinpath('native-writing.txt').read_text()
    from .story_index import GRAPH_INSTRUCTIONS
    instructions += GRAPH_INSTRUCTIONS
    from .book_overview import overview_context
    instructions += overview_context(config.workspace)
    instructions += '\nYou are the main agent. Coordinate independent reviewers concurrently when useful. '
    instructions += 'Give each reviewer a descriptive name and a bounded task. Reviewers report to you; '
    instructions += 'integrate their findings for the author. Follow new author messages during a turn; '
    instructions += 'use collaboration tools to stop or redirect reviewers when requested. '
    instructions += 'Give short progress summaries, never expose private chain-of-thought.\n'
    if control_socket:
        instructions += ('\nYou alone coordinate UI-owned command workflows using workflow_status, '
            'stop_workflow and retry_workflow. Reviewers should not use these controls. '
            'When the author directs an active command, inspect its live status; stop it if '
            'requested, or stop and retry with the new guidance to change direction. '
            'A retry is asynchronous: report it as started, not completed. Never claim a '
            'control action succeeded without the tool result. Do not duplicate successful writes.\n')
    if config.project_instructions:
        instructions += '\nBook-specific instructions:\n' + config.project_instructions
    return instructions
