"""SSE aggregation contracts."""

from qb2api.sse import StreamAggregator


class TestStreamAggregator:
    """Test StreamAggregator behavior."""

    def test_aggregates_content(self):
        aggregator = StreamAggregator(model="test")
        aggregator.process(
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]}
        )
        aggregator.process(
            {"choices": [{"delta": {"content": " World"}, "finish_reason": "stop"}]}
        )

        response = aggregator.response()
        assert response["choices"][0]["message"]["content"] == "Hello World"
        assert response["choices"][0]["finish_reason"] == "stop"

    def test_aggregates_reasoning(self):
        aggregator = StreamAggregator(model="test")
        aggregator.process(
            {"choices": [{"delta": {"reasoning_content": "Think"}, "finish_reason": None}]}
        )
        aggregator.process(
            {"choices": [{"delta": {"content": "Answer"}, "finish_reason": "stop"}]}
        )

        response = aggregator.response()
        assert response["choices"][0]["message"]["reasoning_content"] == "Think"
        assert response["choices"][0]["message"]["content"] == "Answer"

    def test_aggregates_multiple_tool_calls_with_index(self):
        aggregator = StreamAggregator(model="test")
        aggregator.process(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_aaa", "function": {"name": "get_weather", "arguments": ""}}
            ]}, "finish_reason": None}]}
        )
        aggregator.process(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"city":"Tokyo"}'}}
            ]}, "finish_reason": None}]}
        )
        aggregator.process(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 1, "id": "call_bbb", "function": {"name": "get_time", "arguments": ""}}
            ]}, "finish_reason": None}]}
        )
        aggregator.process(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 1, "function": {"arguments": '{"tz":"JST"}'}}
            ]}, "finish_reason": "tool_calls"}]}
        )

        response = aggregator.response()
        tool_calls = response["choices"][0]["message"]["tool_calls"]
        assert response["choices"][0]["finish_reason"] == "tool_calls"
        assert len(tool_calls) == 2
        assert tool_calls[0]["function"]["name"] == "get_weather"
        assert tool_calls[0]["function"]["arguments"] == '{"city":"Tokyo"}'
        assert tool_calls[1]["function"]["name"] == "get_time"
        assert tool_calls[1]["function"]["arguments"] == '{"tz":"JST"}'

    def test_created_is_not_zero(self):
        aggregator = StreamAggregator(model="test")
        aggregator.process(
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}
        )

        assert aggregator.response()["created"] > 0
