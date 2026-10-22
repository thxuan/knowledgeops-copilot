import json
import pytest
from app.generation.models import parse_generation, resolve_citations
from app.generation.context import retrieval_query, continuation_context


def test_follow_up_is_separate_and_survives_citation_resolution():
    answer=parse_generation(json.dumps({'insufficient_context':False,'claims':[{'text':'有依据的通用说明。','citations':['E1']}],'follow_up':'你使用什么系统？'}))
    resolved=resolve_citations(answer,{'E1':'canonical'})
    assert resolved.answer=='有依据的通用说明。' and resolved.citations==['canonical']
    assert resolved.follow_up=='你使用什么系统？'
    clarification=parse_generation(json.dumps({'insufficient_context':False,'claims':[],'follow_up':'请补充错误提示。'}))
    assert clarification.answer=='' and clarification.citations==[] and clarification.follow_up
    assert parse_generation('{"insufficient_context":true,"claims":[]}').follow_up is None


def test_follow_up_does_not_allow_uncited_facts_or_bypass_refusal_contract():
    invalid=[
        {'insufficient_context':False,'claims':[]},
        {'insufficient_context':False,'claims':[{'text':'无引用事实','citations':[]}],'follow_up':'什么系统？'},
        {'insufficient_context':False,'claims':[],'follow_up':'什么系统？','answer':'额外事实'},
        {'insufficient_context':True,'claims':[],'follow_up':'什么系统？'},
        {'insufficient_context':False,'claims':[],'follow_up':'?'*501},
    ]
    for value in invalid:
        with pytest.raises(ValueError):parse_generation(json.dumps(value))


def test_short_generic_clarification_replies_keep_problem_and_latest_user_conditions():
    pending={'original_question':'我的电脑为什么发热？','question':'什么系统、什么场景？','source_message_id':2}
    history=[{'role':'user','content':'我的电脑为什么发热？'},{'role':'user','content':'Windows 11，待机时也热'}]
    query=retrieval_query('笔记本',history,pending)
    assert all(text in query for text in ['笔记本','电脑为什么发热','Windows 11','待机时也热'])
    pending={'original_question':'Docker 启动失败','question':'报错代码是什么？','source_message_id':3}
    assert 'Docker' in retrieval_query('ERR-123',[],pending)
    assert continuation_context('换个问题，VPN 如何安装？',pending) is None
    assert retrieval_query('换个问题，VPN 如何安装？',[],pending)=='换个问题，VPN 如何安装？'
