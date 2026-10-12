"""Create an independent real-core UI fixture; never use provider credentials."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--root',required=True,type=Path)
parser.add_argument('--source',required=True,type=Path)
args=parser.parse_args()
assert '/LitIsLand/' in str(args.root.resolve()) and '/LitIsLand/' in str(args.source.resolve())
root=Path(tempfile.mkdtemp(prefix='lg-library-host-',dir=args.root))
workspace=root/'novel';workspace.mkdir()
os.environ['LITERARYGIANT_REGISTRY_HOME']=str(root/'registry')
os.environ['HOME']=str(root/'home')
sys.path.insert(0,str(args.source/'lg-cli'))
from lg_cli.bookshelf import Bookshelf
from lg_cli.credentials import save_profile
Bookshelf(root).initialize()
Bookshelf(root).create('second')
save_profile('sandbox','fixture-b','synthetic-desktop-key',provider='stepfun',model='fixture-b-model')
save_profile('sandbox','fixture-a','synthetic-desktop-key',provider='stepfun',model='step-3.5-flash')
from lg_cli.init_project import init_workspace
from lg_cli.project_store import ProjectStore
init_workspace(workspace)
ProjectStore(workspace).create_document(kind='character',slug='lin',title='林砚',
    content='初始人物总结：维修学徒，重视工友安全。',state='accepted')
(workspace/'README.md').write_text('LitIsLand 独立写作 UI 验收作品。\n')
launcher=root/'fixture-python'
fixture=Path(__file__).resolve().parent/'fixtures/app_server_fixture.py'
source_path=str(args.source/'lg-cli')+':'+str(args.source)
launcher.write_text('#!'+sys.executable+'\nimport os,sys,runpy\n'
    +f'os.environ["PYTHONPATH"]={source_path!r}\n'
    +f'os.environ["LITERARYGIANT_REGISTRY_HOME"]={str(root/"registry")!r}\n'
    +f'os.environ["HOME"]={str(root/"home")!r}\n'
    +'os.environ["LG_DESKTOP_FIXTURE_KEY"]="synthetic-desktop-key"\n'
    +f'sys.path[:0]=[{str(args.source/"lg-cli")!r},{str(args.source)!r}]\n'
    +'assert sys.argv[1]=="-m"\nmodule=sys.argv[2]\ndel sys.argv[1:3]\n'
    +f'if module=="lg_cli.app_server": runpy.run_path({str(fixture)!r},run_name="__main__")\n'
    +'else:\n assert module=="lg_cli.desktop"\n runpy.run_module(module,run_name="__main__")\n')
launcher.chmod(0o700)
report={'workspace':str(workspace),'launcher':str(launcher),'python':sys.executable,'sourcePythonPath':source_path,'home':str(root/'home')}
(root/'setup.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
