from python_agent.session.events import SessionEvent, SessionHeader
from python_agent.session.projection import derive_messages
from python_agent.session.session import Session


def test_session_sequences_are_contiguous() -> None:
    session = Session.new()
    session.append("turn/start", {"turn": 1})
    session.append("user/message", {"content": "hello"})
    assert [event.seq for event in session.events] == [0, 1]
    assert session.messages() == [{"role": "user", "content": "hello"}]


def test_projection_pairs_tool_call_and_result() -> None:
    events = [
        SessionEvent(seq=0, type="user/message", data={"content": "read"}),
        SessionEvent(
            seq=1,
            type="assistant/message",
            data={
                "content": None,
                "tool_calls": [{"id": "call-1", "name": "echo", "arguments": {"value": "ok"}}],
            },
        ),
        SessionEvent(
            seq=2, type="tool/call", data={"call_id": "call-1", "name": "echo", "arguments": {}}
        ),
        SessionEvent(
            seq=3, type="tool/result", data={"call_id": "call-1", "name": "echo", "content": "ok"}
        ),
    ]
    messages = derive_messages(events)
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == '{"value": "ok"}'
    assert messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "echo",
        "content": "ok",
    }


def test_projection_rejects_unknown_non_ignorable_event() -> None:
    session = Session(SessionHeader(id="session"))
    session.append("future/context", {"value": "must not disappear"})
    try:
        session.messages()
    except Exception as exc:
        assert "unknown non-ignorable" in str(exc)
    else:
        raise AssertionError("unknown event was silently projected")
