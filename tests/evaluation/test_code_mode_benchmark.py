"""Task correctness and response formatting are separate benchmark metrics."""

import pytest

from scripts.benchmark_code_mode import extract_answer


def test_grades_final_json_answer_without_discarding_format_violation():
    assert extract_answer('{"answer": 15600}') == (15600, True)
    assert extract_answer('The sum is 15600.\n{"answer": 15600}') == (15600, False)
    with pytest.raises(ValueError):
        extract_answer('The answer might be 15600.')
    with pytest.raises(ValueError):
        extract_answer('{"answer": true}')
