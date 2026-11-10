from grocer.text import normalize_key, parse_list, parse_size, Size


def test_bullets_headers_quantities_and_diets():
    pl = parse_list("""Produce:
- [ ] bananas
• 2 oat milk (vegan)
☐ dish soap x2
1. bread - gluten free
✓ eggs
3× limes
diet: vegan
""")
    got = [(i.text, i.quantity, i.diet) for i in pl.items]
    assert got == [("bananas", 1, []), ("oat milk", 2, ["vegan"]), ("dish soap", 2, []),
                   ("bread", 1, ["gluten_free"]), ("eggs", 1, []), ("limes", 3, [])]
    assert pl.sections == ["Produce"]
    assert pl.list_diet == ["vegan"]


def test_notes_and_comma_line_and_merge():
    pl = parse_list("oat milk (unsweetened), tortillas, oat milk (unsweetened)")
    assert [(i.text, i.quantity, i.note, i.merged) for i in pl.items] == [
        ("oat milk", 2, "unsweetened", 2), ("tortillas", 1, "", 1)]


def test_numbers_in_names_survive():
    pl = parse_list("2% milk\n7up\n")
    assert [i.text for i in pl.items] == ["2% milk", "7up"]


def test_normalize_key():
    assert normalize_key("Tomatoes") == "tomato"
    assert normalize_key("Bananas 2 lb") == "banana"
    assert normalize_key("Oat Milk 64oz") == "oat milk"
    assert normalize_key("hummus") == "hummus"
    assert normalize_key("garbanzo beans", {"garbanzo bean": "chickpea"}) == "chickpea"


def test_sizes():
    assert parse_size("52 fl oz") == Size(52, "fl oz")
    assert parse_size("14.5 oz / 4 pk") == Size(58, "oz")
    assert parse_size("24 bottles / 16.9 fl oz") == Size(405.6, "fl oz")
    assert parse_size("24 ct / 6 lb") == Size(96, "oz")
    assert parse_size("4 sticks / 1 lb") == Size(16, "oz")
    assert parse_size("11OZA") == Size(11, "oz")
    assert parse_size("15 FO") == Size(15, "fl oz")
    assert parse_size("1 gal") == Size(128, "fl oz")
    assert parse_size("18 rolls") == Size(18, "ct")
    assert parse_size("1 lb") == Size(16, "oz")
    assert parse_size("") is None
