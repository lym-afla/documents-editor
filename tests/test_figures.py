import pytest

from documents_editor.figures import numeric_tokens


@pytest.mark.parametrize("text,tokens", [
    ("Выручка 407,0 млн", ["407.0"]), ("(8.8%)", ["-8.8%"]), ("−12", ["-12"]),
    ("1 234,5", ["1234.5"]), ("1 234,5", ["1234.5"]),
    ("2.5x и 3х", ["2.5x", "3х"]), ("2023A", ["2023"]), ("нет", []),
    ("2023-2024", ["2023", "2024"]), ("a -5 и word-5", ["-5", "5"]),
    ("2023 100", ["2023", "100"]), ("5xyz", ["5"]), ("-3%", ["-3%"]),
])
def test_numeric_tokens(text, tokens):
    assert numeric_tokens(text) == tokens
