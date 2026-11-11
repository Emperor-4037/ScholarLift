from scholarship_finder.domain.req_hints import extract_hints, hints_block

def test_ielts_decimal_and_int():
    assert extract_hints("IELTS 6.5 required")["ielts"]["groups"][0] == "6.5"
    assert extract_hints("IELTS overall band of 7")["ielts"]["groups"][0] == "7"

def test_toefl_gpa_pct_deadline_amount():
    assert "90" in extract_hints("TOEFL iBT 90")["toefl"]["groups"][0]
    g = extract_hints("minimum GPA of 3.0 out of 4.0")["gpa"]["groups"]
    assert g[0] == "3.0" and g[1] == "4.0"
    assert "gpa_pct" in extract_hints("at least 75% marks")
    assert extract_hints("Deadline: 15 March 2027")["deadline"]
    assert extract_hints("closing date March 15, 2027")["deadline"]
    assert extract_hints("€1,200 per month")["amount"]

def test_no_ielts_false_positive():
    h = extract_hints("IELTS candidates need 2 years of experience")
    assert "ielts" not in h or h["ielts"]["groups"][0] != "2"

def test_hints_block_empty():
    assert hints_block({}) == ""
