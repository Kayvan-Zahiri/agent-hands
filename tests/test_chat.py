"""Tests for the chat front door.

Stdlib unittest, no browser and no network: resolving a sentence is pure, which
is the reason it lives in Python rather than in the page.

    .venv/bin/python -m pytest tests/test_chat.py -q
"""

from __future__ import annotations

import unittest

from agent_hands.chat import Understanding, read_intent, read_slots, understand


class TestIntent(unittest.TestCase):
    """Which job a sentence is asking for."""

    def test_the_phrasings_a_teller_would_use(self) -> None:
        for text, want in [
            ("what's the balance on 103001", "balance"),
            ("how much does 100234 have", "balance"),
            ("who is member 100987", "inquiry"),
            ("look up the member named Vaughan", "inquiry"),
            ("move $25.00 from 103001-MMKT-3 to 103001-S0070-7", "transfer"),
            ("send money for 101555", "transfer"),
            ("open a new share for 103001", "open_share"),
            ("freeze account 103001-MMKT-3", "hold"),
            ("update the email for 103001 to d@example.test", "update"),
            ("sign in as teller1", "signon"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(want, read_intent(text))

    def test_a_sentence_nobody_understood_resolves_to_nothing(self) -> None:
        # None is a real answer. Guessing a capability for an unparsed sentence
        # is how a chat box ends up moving money.
        for text in ("make me a sandwich", "cancel that", "hold on a second"):
            with self.subTest(text=text):
                self.assertIsNone(read_intent(text))


class TestHoldIsNotAnInterjection(unittest.TestCase):
    """"hold on" means wait, and freezing an account on a filler word is the
    worst mistake this module could make.

    Found by testing, not by reading: the first version scored "hold on, check
    the balance for 103001" as a request to freeze that account.
    """

    def test_the_interjection_does_not_freeze_anybody(self) -> None:
        self.assertEqual("balance", read_intent("hold on, check the balance for 103001"))
        self.assertIsNone(read_intent("hold on a second"))

    def test_and_the_veto_does_not_eat_the_real_instruction(self) -> None:
        # The obvious fix -- veto anything containing "hold on" -- swallows this,
        # which is why the explicit phrasings are scored first.
        self.assertEqual("hold", read_intent("put a hold on member 102777"))
        self.assertEqual("hold", read_intent("place a hold on 103001-MMKT-3"))
        self.assertEqual("hold", read_intent("freeze 103001-MMKT-3"))


class TestSlots(unittest.TestCase):
    """The values a person actually said."""

    def test_a_member_number_is_not_taken_from_a_share_id(self) -> None:
        s = read_slots("check 103001-MMKT-3 for member 100234")
        self.assertEqual("103001-MMKT-3", s["share_id"])
        self.assertEqual("100234", s["member_id"])

    def test_two_accounts_become_from_and_to(self) -> None:
        s = read_slots("move $25.00 from 103001-MMKT-3 to 103001-S0070-7")
        self.assertEqual("103001-MMKT-3", s["from_share"])
        self.assertEqual("103001-S0070-7", s["to_share"])
        self.assertEqual("25.00", s["amount"])

    def test_a_thousands_separator_survives(self) -> None:
        self.assertEqual("1250.50", read_slots("transfer $1,250.50 today")["amount"])

    def test_contact_details(self) -> None:
        s = read_slots("update 103001 to d.vaughan@example.test and 555-0199")
        self.assertEqual("d.vaughan@example.test", s["email"])
        self.assertEqual("555-0199", s["phone"])

    def test_a_last_name_needs_an_explicit_marker(self) -> None:
        # The other bug the tests found: "for Member 103001" was read as a
        # surname, which turns a lookup by number into one that finds nobody.
        self.assertEqual("Vaughan", read_slots("the member named Vaughan")["last_name"])
        self.assertEqual("Smith", read_slots("search for Smith")["last_name"])
        self.assertNotIn("last_name", read_slots("check the balance for Member 103001"))


class TestCarryingContext(unittest.TestCase):
    """"what about 100987" is a whole sentence to a person."""

    def test_a_bare_number_reuses_the_last_intent(self) -> None:
        first = understand("what's the balance on 103001")
        self.assertEqual("balance", first.capability)

        second = understand("what about 100987", first)

        self.assertEqual("balance", second.capability)
        self.assertEqual("100987", second.slots["member_id"])

    def test_a_turn_with_its_own_intent_is_not_hijacked(self) -> None:
        first = understand("what's the balance on 103001")
        second = understand("now freeze 103001-MMKT-3", first)
        self.assertEqual("hold", second.capability)

    def test_nothing_is_carried_into_a_sentence_with_no_values(self) -> None:
        first = understand("what's the balance on 103001")
        self.assertIsNone(understand("thanks", first).capability)

    def test_it_survives_a_round_trip_through_json(self) -> None:
        u = understand("balance for 103001")
        self.assertEqual({"capability", "slots", "text"}, set(u.to_json()))
        self.assertEqual("balance", Understanding(**u.to_json()).capability)


CATALOG = [
    {"id": "meridian_balance_recorded", "irreversible": False, "business_codes": [],
     "params": [{"name": "operator", "required": True, "example": "teller1"},
                {"name": "password", "required": True, "example": None},
                {"name": "member_id", "required": True, "example": "103001"}]},
    {"id": "meridian_member_balance", "irreversible": False,
     "business_codes": [{"code": "member_not_found"}],
     "params": [{"name": "operator", "required": True, "example": "teller1"},
                {"name": "password", "required": True, "example": None},
                {"name": "member_id", "required": True, "example": "103001"},
                {"name": "share_id", "required": True, "example": None}]},
    {"id": "meridian_member_inquiry", "irreversible": False,
     "business_codes": [{"code": "no_search_match"}],
     "params": [{"name": "operator", "required": True, "example": "teller1"},
                {"name": "password", "required": True, "example": None},
                {"name": "search_by", "required": True, "example": "number"},
                {"name": "value", "required": True, "example": "103001"}]},
    {"id": "meridian_place_hold", "irreversible": True,
     "business_codes": [{"code": "supervisor_required"}],
     "params": [{"name": "operator", "required": True, "example": "teller1"},
                {"name": "password", "required": True, "example": None},
                {"name": "member_id", "required": True, "example": "103001"},
                {"name": "share_id", "required": True, "example": None},
                {"name": "reason", "required": True, "example": "LEGAL"}]},
]
DEFAULTS = {"password": "password"}


def _plan(text, **kw):
    from agent_hands.chat import plan
    return plan(understand(text), CATALOG, DEFAULTS, **kw)


class TestResolving(unittest.TestCase):
    """Which capability serves the request."""

    def test_it_takes_the_one_that_understands_a_refusal(self) -> None:
        # Both do the job. Only one carries business rules, and without them the
        # application refusing you reads as a broken automation.
        from agent_hands.chat import resolve
        self.assertEqual("meridian_member_balance", resolve("balance", CATALOG)["id"])

    def test_an_intent_with_nothing_recorded_for_it_is_admitted(self) -> None:
        self.assertEqual("puzzled", _plan("open a new share for 103001").action)


class TestPlanning(unittest.TestCase):
    def test_a_complete_request_is_ready_to_run(self) -> None:
        p = _plan("balance for 103001 account 103001-MMKT-3")
        self.assertEqual("invoke", p.action)
        self.assertEqual("meridian_member_balance", p.capability)
        self.assertEqual("103001", p.args["member_id"])
        self.assertEqual("103001-MMKT-3", p.args["share_id"])
        self.assertEqual("password", p.args["password"])

    def test_a_missing_value_is_asked_for_rather_than_guessed(self) -> None:
        p = _plan("what's the balance on 103001")
        self.assertEqual("ask", p.action)
        self.assertEqual("share_id", p.missing)
        self.assertIn("account", p.message.lower())

    def test_a_write_is_read_back_before_it_runs(self) -> None:
        p = _plan("freeze 103001-MMKT-3 for member 103001 reason LEGAL")
        self.assertEqual("confirm", p.action)
        self.assertEqual("meridian_place_hold", p.capability)

    def test_and_runs_once_it_is_confirmed(self) -> None:
        p = _plan("freeze 103001-MMKT-3 for member 103001 reason LEGAL", confirmed=True)
        self.assertEqual("invoke", p.action)

    def test_a_sentence_nobody_understood_never_reaches_a_capability(self) -> None:
        p = _plan("make me a sandwich")
        self.assertEqual("puzzled", p.action)
        self.assertIsNone(p.capability)


class TestSearchingByLastName(unittest.TestCase):
    """The inquiry screen takes a field and a value, not a member number.

    This is how the brief's "search by member number *or* by last name" is
    reached at all, so it is worth pinning both directions.
    """

    def test_a_name_searches_by_name(self) -> None:
        p = _plan("look up the member named Vaughan")
        self.assertEqual("invoke", p.action)
        self.assertEqual("name", p.args["search_by"])
        self.assertEqual("Vaughan", p.args["value"])

    def test_a_number_searches_by_number(self) -> None:
        p = _plan("who is member 100987")
        self.assertEqual("number", p.args["search_by"])
        self.assertEqual("100987", p.args["value"])


class TestDescribing(unittest.TestCase):
    """The three endings, kept apart, in words."""

    def test_success_reads_back_what_it_found(self) -> None:
        from agent_hands.chat import describe
        said = describe({"outcome": "success", "outputs": {"savings_balance": "$780.50"}})
        self.assertIn("$780.50", said)

    def test_a_business_outcome_is_named_as_an_answer(self) -> None:
        from agent_hands.chat import describe
        said = describe({"outcome": "business_outcome",
                         "message": "this function needs a supervisor to sign on"})
        self.assertIn("supervisor", said)
        self.assertIn("not a fault", said)
        self.assertIn("nothing was changed", said.lower())

    def test_a_failure_says_nothing_changed(self) -> None:
        from agent_hands.chat import describe
        said = describe({"outcome": "failure", "message": "unresolved control"})
        self.assertIn("could not finish", said)
        self.assertIn("nothing was changed", said.lower())


class TestConversation(unittest.TestCase):
    """The little bit of state a thread needs to feel like one."""

    def talk(self):
        from agent_hands.chat import Conversation
        return Conversation(defaults=DEFAULTS)

    def test_a_bare_reply_answers_the_question_just_asked(self) -> None:
        # "103001-MMKT-3" is a whole sentence in a conversation and nothing at
        # all to a parser looking for an intent.
        c = self.talk()
        self.assertEqual("ask", c.say("what's the balance on 103001", CATALOG).action)

        p = c.say("103001-MMKT-3", CATALOG)

        self.assertEqual("invoke", p.action)
        self.assertEqual("103001-MMKT-3", p.args["share_id"])
        self.assertEqual("103001", p.args["member_id"])

    def test_yes_means_the_thing_just_read_back(self) -> None:
        c = self.talk()
        self.assertEqual("confirm", c.say("freeze 103001-MMKT-3 for 103001 reason LEGAL",
                                          CATALOG).action)

        p = c.say("yes", CATALOG)

        self.assertEqual("invoke", p.action)
        self.assertEqual("meridian_place_hold", p.capability)

    def test_no_drops_it_and_says_nothing_changed(self) -> None:
        c = self.talk()
        c.say("freeze 103001-MMKT-3 for 103001 reason LEGAL", CATALOG)

        p = c.say("no", CATALOG)

        self.assertEqual("puzzled", p.action)
        self.assertIn("nothing was changed", p.message.lower())
        self.assertIsNone(c.proposed)

    def test_yes_on_its_own_authorizes_nothing(self) -> None:
        # Nothing has been read back, so there is nothing to agree to.
        self.assertNotEqual("invoke", self.talk().say("yes", CATALOG).action)

    def test_changing_the_subject_is_not_treated_as_an_answer(self) -> None:
        c = self.talk()
        c.say("what's the balance on 103001", CATALOG)

        p = c.say("actually who is member 100987", CATALOG)

        self.assertEqual("meridian_member_inquiry", p.capability)
        self.assertEqual("100987", p.args["value"])

    def test_a_value_is_never_invented_from_the_recording(self) -> None:
        # The Service Desk seeds its form from the artifact's examples, which is
        # fine because the form is on screen to be read. Here there is nothing
        # to read, so an account nobody named has to be asked for.
        p = self.talk().say("what's the balance on 103001", CATALOG)
        self.assertEqual("ask", p.action)
        self.assertEqual("share_id", p.missing)
