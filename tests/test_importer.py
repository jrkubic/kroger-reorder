# tests/test_importer.py
from app.importer import ParsedLine, parse


def names(lines):
    return [(l.name, l.quantity) for l in lines]


def test_empty_text_gives_nothing():
    assert parse("") == []
    assert parse("   \n\n") == []


def test_plain_lines_default_quantity_one():
    assert names(parse("milk\neggs\n\nbread")) == [("milk", 1), ("eggs", 1), ("bread", 1)]


def test_markdown_bullets_checkboxes_numbers_and_headings():
    text = "# Groceries\n- [ ] milk\n* eggs\n1. bread\n- [x] butter\n## Dairy\n"
    assert names(parse(text)) == [("milk", 1), ("eggs", 1), ("bread", 1), ("butter", 1)]


def test_quantity_patterns():
    text = "2x milk\neggs x3\nbread (4)\n5 X yogurt\n2% milk"
    assert names(parse(text)) == [("milk", 2), ("eggs", 3), ("bread", 4), ("yogurt", 5), ("2% milk", 1)]


def test_raw_is_preserved():
    line = parse("- [ ] 2x milk")[0]
    assert line == ParsedLine(raw="- [ ] 2x milk", name="milk", quantity=2)


def test_csv_with_header():
    text = "name,quantity\nmilk,2\neggs,1\nbread,\n"
    assert names(parse(text)) == [("milk", 2), ("eggs", 1), ("bread", 1)]


def test_csv_without_header_uses_first_two_columns():
    text = "milk,2\neggs,3\nbutter,x\n"
    assert names(parse(text)) == [("milk", 2), ("eggs", 3), ("butter", 1)]


def test_csv_header_columns_in_any_order():
    text = "qty,item\n2,milk\n1,eggs\n"
    assert names(parse(text)) == [("milk", 2), ("eggs", 1)]


def test_json_array_of_strings():
    assert names(parse('["milk", "2x eggs"]')) == [("milk", 1), ("eggs", 2)]


def test_json_array_of_objects():
    text = '[{"name": "milk", "quantity": 2}, {"name": "eggs"}, {"item": "bread", "qty": "3"}]'
    assert names(parse(text)) == [("milk", 2), ("eggs", 1), ("bread", 3)]


def test_json_object_with_items_key():
    text = '{"name": "Weekly", "items": [{"name": "milk", "quantity": 2}]}'
    assert names(parse(text)) == [("milk", 2)]


def test_invalid_json_falls_back_to_lines():
    assert names(parse("[milk\neggs")) == [("[milk", 1), ("eggs", 1)]


def test_bulleted_lines_with_commas_are_not_csv():
    text = "- milk, whole\n- eggs, large"
    assert names(parse(text)) == [("milk, whole", 1), ("eggs, large", 1)]
