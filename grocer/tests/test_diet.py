from fakes import prod
from grocer.diet import check

OAT = prod("1", "Planet Oat Original Oat Milk", ingredients="OATMILK (FILTERED WATER, OATS), CALCIUM CARBONATE, SEA SALT",
           allergens=[("Contains", "Oats"), ("Free from", "Milk and its derivates")],
           declarations=["Vegan", "Dairy Free", "Kosher"])
COOKIE = prod("2", "Butter cookie", ingredients="flour (wheat), butter, sugar, eggs",
              allergens=[("Contains", "Milk and its derivates"), ("Contains", "Eggs and their derivates"),
                         ("Contains", "Wheat and Their Derivatives")])
HONEY_BAR = prod("3", "Oat bar", ingredients="oats, honey, almonds", allergens=[("Contains", "Almond And Almond Products")])
PB = prod("4", "Peanut butter", ingredients="peanuts, salt", allergens=[("Contains", "Peanuts and their derivates")])
BANANA = prod("5", "Bananas", ingredients=None, allergens=[("Free from", "Does Not Contain Declaration Obligatory Allergens")],
              categories=["Produce"])
FLAVORED = prod("6", "Chips", ingredients="potatoes, oil, natural flavors, salt",
                allergens=[("May contain", "Milk and its derivates")])
NO_DATA = prod("7", "Mystery sauce")


def test_claims_and_allergens():
    assert check(OAT, "vegan")[0] == "likely"
    assert check(OAT, "dairy_free")[0] == "likely"
    assert check(COOKIE, "vegan") == ("fails", ["allergen: contains milk", "allergen: contains egg"])
    assert check(COOKIE, "gluten_free")[0] == "fails"


def test_ingredient_denylist_and_false_friends():
    assert check(HONEY_BAR, "vegan") == ("fails", ["ingredient: honey"])
    assert check(HONEY_BAR, "nut_free")[0] == "fails"
    assert check(PB, "vegan")[0] == "likely"         # "peanut butter" is not dairy
    assert check(PB, "nut_free")[0] == "fails"


def test_ambiguous_is_allowed_and_flagged():
    status, reasons = check(FLAVORED, "vegan")
    assert status == "likely"
    assert "may contain milk" in reasons and "check: natural flavors" in reasons


def test_produce_unknown_and_overrides():
    assert check(BANANA, "vegan")[0] == "likely"
    assert check(BANANA, "gluten_free")[0] == "unknown"
    assert check(NO_DATA, "vegan")[0] == "unknown"
    assert check(NO_DATA, "vegan", "ok")[0] == "ok_override"
    assert check(OAT, "vegan", "fails")[0] == "fails"
