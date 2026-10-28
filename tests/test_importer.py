# tests/test_importer.py
from kroger_reorder.importer import ParsedLine, parse


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


def test_tsv_from_spreadsheet_paste():
    assert names(parse("milk\t2\neggs\t3")) == [("milk", 2), ("eggs", 3)]
    assert names(parse("name\tqty\nmilk\t2")) == [("milk", 2)]


def test_utf8_bom_is_ignored():
    assert names(parse("﻿name,quantity\nmilk,2")) == [("milk", 2)]


def test_bare_bullets_yield_nothing():
    assert names(parse("- \n-\n- milk")) == [("milk", 1)]


def test_quantity_is_capped():
    assert names(parse("500x milk")) == [("milk", 99)]
    assert names(parse("name,quantity\nmilk,500")) == [("milk", 99)]
    assert names(parse('[{"name": "milk", "quantity": 500}]')) == [("milk", 99)]


def test_json_non_scalar_name_is_skipped():
    assert names(parse('[{"name": {"a": 1}}, {"name": "milk"}]')) == [("milk", 1)]


# -- exact rows (exported CSV / list JSON carrying a UPC) ---------------------------

EXPORT = ("upc,name,size,quantity,included,price,promo,image\n"
          "0001111041700,\"Kroger Whole Milk, Vitamin D\",1 gal,2,true,3.49,2.99,https://img/milk.jpg\n"
          "0001,Eggs (12),12 ct,1,false,,,\n"
          ",Bread x2,,1,true,,,\n")


def test_csv_with_upc_column_yields_exact_rows_without_quantity_parsing():
    lines = parse(EXPORT)
    assert [(l.name, l.quantity) for l in lines] == [("Kroger Whole Milk, Vitamin D", 2), ("Eggs (12)", 1), ("Bread", 1)]
    assert lines[0].exact == {"upc": "0001111041700", "size": "1 gal", "included": True, "price": 3.49,
                              "promo": 2.99, "image": "https://img/milk.jpg"}
    assert lines[1].exact == {"upc": "0001", "size": "12 ct", "included": False, "price": None, "promo": None,
                              "image": None}
    assert lines[2].exact is None  # no UPC: falls back to the name search flow


def test_csv_without_upc_column_has_no_exact():
    assert all(l.exact is None for l in parse("name,quantity\nmilk,2\n"))


def test_json_list_file_with_upcs_yields_exact_rows():
    text = '{"name": "Standard", "items": [{"upc": "0001", "name": "Milk", "size": "1 gal", "quantity": 3, "included": true, "price": 3.49}]}'
    (line,) = parse(text)
    assert (line.name, line.quantity) == ("Milk", 3)
    assert line.exact == {"upc": "0001", "size": "1 gal", "included": True, "price": 3.49, "promo": None, "image": None}


def test_exact_row_without_name_is_skipped():
    assert parse("upc,name,quantity\n0001,,2\n") == []
