"""Synthetic classifier replies for real-runtime tests, never a production router."""
import json
import re

from lg_cli.intent_router import MARKER,proposal


def current_user(messages):
    for message in reversed(messages):
        if message.get('role')!='user':continue
        content=message.get('content','')
        if isinstance(content,str):return content
        text=next((part.get('text','') for part in reversed(content) if part.get('type') in {'text','input_text'}),'')
        if text:return text
    return ''


def routed_reply(text):
    if not text.startswith(MARKER):return None
    data=json.loads(text.split('INPUT_JSON:\n',1)[1]);author=data['author']
    saved=data['context']['saved']['documents']
    chapter=next((item for item in saved if item['slug'] in {'one','original'}),None)
    target=chapter['target'] if chapter else None
    case=re.search(r'CASE:([a-z]+)',author)
    case=case.group(1) if case else 'text'
    if case=='ambiguous':
        return proposal(kind='revision',target=target,passage='作者已采纳的原稿。') if '作者补充' in author else proposal(decision='clarify')
    if '只做规划' in author:return proposal(kind='planning',stage='plan')
    if '不保存' in author:return proposal()
    if case=='plan':return proposal(kind='planning',stage='position')
    if case in {'reference','refdeny'}:return proposal(kind='planning',stage='world')
    if case in {'deny','denied','accepted'}:return proposal(kind='overview')
    if case=='edit':return proposal(kind='revision',target=target,wholeDocument=True)
    if case=='restore':return proposal(kind='restore',target=target,versionNumber=1)
    if case=='adoptref':return proposal(kind='adopt',target=next(item['target'] for item in saved if item['slug']=='lin'),versionNumber=1)
    return proposal()
