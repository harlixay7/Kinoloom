import pytest
from llm import extract_json


def test_extract_json_markdown_code_fences():
    """Extracts JSON enclosed within markdown ```json and ``` fences."""
    text_with_json_fence = (
        "Certainly! Here is the generated treatment:\n"
        "```json\n"
        '{\n  "treatment": "A cyberpunk detective in Neo-Tokyo",\n  "acts": 3\n}\n'
        "```\n"
        "Let me know if you would like any modifications."
    )
    result = extract_json(text_with_json_fence)
    assert result is not None
    assert result["treatment"] == "A cyberpunk detective in Neo-Tokyo"
    assert result["acts"] == 3

    # Generic triple-backtick fence without language tag
    text_plain_fence = '```\n{"treatment": "High fantasy quest", "target_runtime": 120}\n```'
    res_plain = extract_json(text_plain_fence)
    assert res_plain == {"treatment": "High fantasy quest", "target_runtime": 120}


def test_extract_json_trailing_commas():
    """Tolerates and strips trailing commas inside JSON objects and arrays."""
    text_trailing = """
    {
      "characters": [
        "hero",
        "villain",
      ],
      "settings": {
        "theme": "noir",
      },
    }
    """
    result = extract_json(text_trailing)
    assert result is not None
    assert result["characters"] == ["hero", "villain"]
    assert result["settings"] == {"theme": "noir"}


def test_extract_json_truncated_with_open_brackets():
    """Recovers truncated JSON replies via stack closer reconstruction."""
    truncated_text = (
        '{"treatment": "Subterranean expedition", '
        '"beats": [{"act": 1, "summary": "Decent into the caves"'
    )
    result = extract_json(truncated_text)
    assert result is not None
    assert result["treatment"] == "Subterranean expedition"
    assert len(result["beats"]) == 1
    assert result["beats"][0]["summary"] == "Decent into the caves"


def test_extract_json_prose_surrounding_and_markers():
    """Extracts JSON embedded inside conversational prose."""
    text = (
        "Here is the plan you requested.\n"
        '{"treatment": "Orbital station incident", "shots_count": 24}\n'
        "Hope this helps!"
    )
    result = extract_json(text)
    assert result is not None
    assert result["treatment"] == "Orbital station incident"
    assert result["shots_count"] == 24


def test_extract_json_last_fence_wins():
    """The echoed prompt's example fence comes first; the reply is the last."""
    text = ("Prompt example:\n```json\n{\"example\": true}\n```\n"
            "Reply:\n```json\n{\"treatment\": {\"title\": \"Real\"}}\n```")
    assert extract_json(text) == {"treatment": {"title": "Real"}}


def test_extract_json_truncated_large_reply_is_fast():
    """Salvaging a large truncated reply must be bounded (was O(n^2))."""
    import time
    shots = ",".join('{"id": "s%d", "prompt": "a long prompt text here"}' % i
                     for i in range(4000))
    text = 'Sure:\n{"shots": {"shots": [' + shots
    text = text[:-20]                         # cut mid-object
    t0 = time.perf_counter()
    result = extract_json(text)
    assert time.perf_counter() - t0 < 2.0
    assert result is not None
    assert len(result["shots"]["shots"]) >= 3998


def test_extract_json_truncated_prefers_reply_marker():
    """A truncated reply salvages from its stage marker, not from an example
    object that appears earlier in the echoed prompt."""
    text = ('Format: {"example": 1}\n'
            '{"beats": [{"act": 1, "summary": "Opening"}, {"act": 2')
    result = extract_json(text)
    assert result is not None and "beats" in result
    assert result["beats"][0]["summary"] == "Opening"


def test_extract_json_invalid_or_empty():
    """Returns None for non-JSON text or empty strings."""
    assert extract_json("") is None
    assert extract_json("   ") is None
    assert extract_json("This response contains no JSON at all.") is None
