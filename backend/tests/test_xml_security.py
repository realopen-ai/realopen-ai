"""Code-scanning security regression tests."""




def test_pptx_xml_regex_deliberately_accepts_unicode_and_rejects_controls():
    from app.services.pptx_gen import _XML_INVALID_RE

    allowed = "\t\n\r ~\u00a0\ud7ff\ue000\ufffd\U00010000\U0010ffffالعربية😀"
    assert _XML_INVALID_RE.sub("", allowed) == allowed
    forbidden = "\x00\x08\x0b\x1f\x7f\x9f\ud800\udfff\ufffe\uffff"
    assert _XML_INVALID_RE.sub("", forbidden) == ""
