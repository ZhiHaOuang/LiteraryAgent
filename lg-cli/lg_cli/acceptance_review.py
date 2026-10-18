"""Owned review worker: saves candidate analyses and never activates manuscript text."""
import json
import sys
from pathlib import Path

from .config import load_config
from .main import build_parser, _apply_runtime_overrides
from .project_store import ProjectStore
from .supervision import review_for_acceptance
from .ui import RunEventRenderer


def main(argv=None):
    args=build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command!='version' or args.version_command!='accept' or not args.cwd or args.shelf:
        raise ValueError('Review worker requires one book, document and version')
    root=Path(args.cwd).expanduser().resolve()
    config=_apply_runtime_overrides(load_config(root,environment=args.environment),args)
    store=ProjectStore(root)
    try:
        document=store.get_document(args.reference)
        version=store.get_version(document.id,args.version)
        review_for_acceptance(config,store,args.reference,args.version,final=args.final,
            on_event=RunEventRenderer(json_mode=True))
        print(json.dumps({'type':'acceptance.reviewed','documentId':document.id,
            'versionId':version.id,'final':args.final}))
        return 0
    except Exception:
        print(json.dumps({'type':'acceptance.failed','error':'Candidate review failed; inspect saved analysis runs.'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
