"""Fixture exercising the prose-assert detector; never executed, only scanned."""


def _probe(out: str) -> None:
    assertIn("The quick brown fox jumps over the lazy dog again.", out)
    assertEqual(out, "A completely different sentence that also runs past forty.")
    assert "This membership sentence is longer than forty characters." in out
    assertTrue(out.startswith("A startswith sentence that is over forty characters."))
    assert out == "One final sentence that is well over forty characters long."
