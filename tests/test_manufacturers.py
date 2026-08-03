# This file is part of schema-convert.
#
# Copyright (C) 2026 Mark Haslinghuis
#
# schema-convert is free software. You can redistribute this software
# and/or modify this software under the terms of the GNU General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version.
#
# schema-convert is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this software. If not, see <https://www.gnu.org/licenses/>.

"""
manufacturers.py - the MANUFACTURER_ID registry snapshot.

An id absent from the config repo's Manufacturers.md produces a target the
configurator refuses to offer, and nothing in a build catches it. Two things are
tested here, and neither needs a schematic or a config checkout:

  * the markdown parser, on synthetic tables containing the shapes the real file
    has (an empty contact column, an id with no name) and the shapes it does not
    yet have but a future edit could introduce - a duplicate, a short row, a
    lower-case id. A row that cannot be read must be *reported*, never dropped,
    because a manufacturer missing from the snapshot rejects a config that is in
    fact valid.

  * the committed snapshot itself: it resolves the reserved ids, rejects an
    invented one, and carries no local path. This file is published - a previous
    data file leaked a home directory. The assertions deliberately use only the
    reserved ids: naming a real manufacturer here would say which vendors this
    corpus holds submissions from, which is the association CLAUDE.md keeps out
    of the repository.
"""

import json
import re
import unittest

import support  # noqa: F401  (puts mcu-parser on sys.path)

import manufacturers as M


TABLE = """\
# Manufacturer Ids

Some prose about the list.

|Manufacturer Id|Name|Contact|
|-|-|-|
|CUST|'Custom', to be used for homebrew targets||
|EXMP|Example Robotics Ltd|http://example.invalid/|
|YYRC|JLDZ||
"""


# --------------------------------------------------------------------------- #
# The markdown parser
# --------------------------------------------------------------------------- #

class Parser(unittest.TestCase):

    def test_reads_the_table(self):
        entries, problems = M.parse_markdown(TABLE)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(entries), ["CUST", "EXMP", "YYRC"])
        self.assertEqual(entries["EXMP"]["name"], "Example Robotics Ltd")
        self.assertEqual(entries["EXMP"]["url"], "http://example.invalid/")

    def test_trailing_empty_cell_is_a_column_not_a_missing_one(self):
        """`|YYRC|JLDZ||` has three columns, the last one blank."""
        entries, problems = M.parse_markdown(TABLE)
        self.assertEqual(problems, [])
        self.assertEqual(entries["YYRC"], {"name": "JLDZ", "url": ""})

    def test_prose_and_header_are_not_entries(self):
        entries, _ = M.parse_markdown(TABLE)
        for junk in ("Manufacturer Id", "-", "Some"):
            self.assertNotIn(junk, entries)

    def test_short_row_is_reported_not_dropped_silently(self):
        entries, problems = M.parse_markdown(TABLE + "|ABCD|No contact column|\n")
        self.assertNotIn("ABCD", entries)
        self.assertTrue(any("line 10" in p and "2" in p for p in problems), problems)

    def test_duplicate_id_keeps_the_first_and_reports(self):
        entries, problems = M.parse_markdown(TABLE + "|EXMP|Impostor|http://x/|\n")
        self.assertEqual(entries["EXMP"]["name"], "Example Robotics Ltd")
        self.assertTrue(any("duplicate" in p for p in problems), problems)

    def test_odd_shaped_id_is_recorded_and_reported(self):
        """
        The registry decides what is registered, not this parser's idea of an
        id. Rejecting the row would make a real manufacturer fail validation;
        accepting it silently would hide a broken edit. So: both.
        """
        entries, problems = M.parse_markdown(TABLE + "|spdx2|Lower And Long||\n")
        self.assertIn("spdx2", entries)
        self.assertTrue(any("spdx2" in p for p in problems), problems)

    def test_missing_header_is_reported(self):
        _, problems = M.parse_markdown("|EXMP|Example|http://x/|\n")
        self.assertTrue(any("header" in p for p in problems), problems)

    def test_code_fence_contents_are_ignored(self):
        entries, _ = M.parse_markdown(
            TABLE + "```\n|FAKE|Example row inside a fence||\n```\n")
        self.assertNotIn("FAKE", entries)

    def test_empty_id_is_reported(self):
        entries, problems = M.parse_markdown(TABLE + "||Nameless|http://x/|\n")
        self.assertEqual(len(entries), 3)
        self.assertTrue(any("empty" in p for p in problems), problems)


# --------------------------------------------------------------------------- #
# Lookup and validation - the API genconfig calls
# --------------------------------------------------------------------------- #

def registry_from(table: str) -> M.Registry:
    entries, _ = M.parse_markdown(table)
    return M.Registry({
        "schema": M.SCHEMA,
        "reserved": [m for m in M.RESERVED_IDS if m in entries],
        "manufacturers": entries,
    })


class Lookup(unittest.TestCase):

    def setUp(self):
        self.reg = registry_from(TABLE)

    def test_registered_id_resolves_to_its_name(self):
        hit = self.reg.lookup("CUST")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.name, self.reg.lookup("CUST").name)

    def test_unregistered_id_resolves_to_none(self):
        self.assertIsNone(self.reg.lookup("ZZZZ"))
        self.assertFalse("ZZZZ" in self.reg)

    def test_lookup_is_case_insensitive_but_returns_the_canonical_spelling(self):
        self.assertEqual(self.reg.lookup(" cust ").id, "CUST")

    def test_reserved_ids_are_flagged_but_valid(self):
        res = self.reg.check("CUST")
        self.assertTrue(res.ok)
        self.assertTrue(res.reserved)

    def test_check_distinguishes_unregistered_from_malformed(self):
        self.assertEqual(self.reg.check("ZZZZ").reason, "unregistered")
        self.assertEqual(self.reg.check("Example Robotics").reason, "malformed")
        self.assertEqual(self.reg.check("").reason, "empty")
        self.assertEqual(self.reg.check("CUST").reason, "ok")

    def test_failed_check_carries_a_message_and_a_near_miss(self):
        # A one-character typo of a reserved id: near-miss suggestion without
        # naming a real manufacturer.
        res = self.reg.check("CUSZ")
        self.assertFalse(res.ok)
        self.assertIn("CUST", res.suggestions)
        self.assertIn("CUST", res.message())

    def test_ok_check_has_no_message(self):
        self.assertEqual(self.reg.check("CUST").message(), "")

    def test_check_returns_the_id_to_emit(self):
        """genconfig emits Check.id, so it must be canonical even on odd input."""
        self.assertEqual(self.reg.check("cust").id, "CUST")

    def test_none_and_whitespace_do_not_raise(self):
        for junk in (None, "", "   ", "\t"):
            self.assertFalse(self.reg.check(junk).ok)
            self.assertIsNone(self.reg.lookup(junk))


# --------------------------------------------------------------------------- #
# The committed snapshot
# --------------------------------------------------------------------------- #

class Snapshot(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(M.DATA_FILE.read_text())
        cls.reg = M.load()

    def test_data_file_is_committed_and_loads(self):
        self.assertTrue(M.DATA_FILE.exists(), f"{M.DATA_FILE} is not committed")
        self.assertEqual(self.reg.schema, M.SCHEMA)

    def test_it_holds_the_whole_registry_not_a_fragment(self):
        """~200 rows upstream; a parse that collapses would show up as a cliff."""
        self.assertGreater(len(self.reg), 120)

    def test_known_ids_resolve(self):
        # Reserved ids only. They are vendor-neutral by construction, so this
        # proves the snapshot resolves without recording which manufacturers
        # have submitted boards to this corpus.
        for mid in ("CUST", "FOSS", "COMM", "LEGA"):
            hit = self.reg.lookup(mid)
            self.assertIsNotNone(hit, f"{mid} missing from the snapshot")
            self.assertTrue(hit.name.strip())

    def test_invented_id_does_not_resolve(self):
        self.assertIsNone(self.reg.lookup("ZZZZ"))
        self.assertFalse(M.is_registered("ZZZZ"))

    def test_reserved_ids_are_present_and_flagged(self):
        for mid in M.RESERVED_IDS:
            hit = self.reg.lookup(mid)
            self.assertIsNotNone(hit, f"{mid} missing from the snapshot")
            self.assertTrue(hit.reserved, f"{mid} not flagged reserved")

    def test_the_tool_own_example_id_validates(self):
        """README and the tests generate boards as CUST; that must stay valid."""
        self.assertTrue(M.check("CUST").ok)

    def test_every_id_is_canonical(self):
        odd = [m for m in self.reg.ids() if not M.ID_SHAPE.match(m)]
        self.assertEqual(odd, [], f"non-canonical ids in the snapshot: {odd}")

    def test_provenance_is_recorded(self):
        src = self.raw["source"]
        for field in ("repo", "file", "rev", "commit_date", "sha256"):
            self.assertTrue(src.get(field), f"source.{field} is empty")
        self.assertTrue(src["repo"].startswith("https://"), src["repo"])
        self.assertRegex(src["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(src["commit_date"], r"^\d{4}-\d\d-\d\d$")

    def test_no_local_path_is_published(self):
        """
        This file is committed, unlike firmware.json. A home directory leaked
        through a data file once already.
        """
        text = M.DATA_FILE.read_text()
        for pattern in (r"/home/", r"/Users/", r"[A-Z]:\\\\", r'"\s*/'):
            self.assertIsNone(re.search(pattern, text),
                              f"{M.DATA_FILE} contains {pattern!r}")

    def test_names_carry_no_control_characters(self):
        for mid in self.reg.ids():
            for field in (self.reg.lookup(mid).name, self.reg.lookup(mid).url):
                self.assertNotIn("|", field, mid)
                self.assertEqual(field, field.strip(), mid)


if __name__ == "__main__":
    unittest.main()
