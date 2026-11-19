import sqlite3
import unittest

from hvh import state_machine
from hvh.applications import submit
from hvh.db import transaction
from hvh.state_machine import TRANSITIONS, ApplicationNotFound, InvalidTransition, State
from tests.helpers import CATALOG, DbTestCase, sample_body


class StateMachineTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_id = submit(self.conn, CATALOG, "c", "k", sample_body()).application_id

    def move(self, target: State) -> State:
        with transaction(self.conn):
            return state_machine.transition(self.conn, self.app_id, target, "test", 1.0)

    def test_happy_path_records_every_step(self) -> None:
        for target in (State.SCREENED, State.ASSESSED, State.SHORTLISTED):
            self.move(target)
        steps = [(e["from_state"], e["to_state"]) for e in state_machine.history(self.conn, self.app_id)]
        self.assertEqual(steps, [(None, "applied"), ("applied", "screened"),
                                 ("screened", "assessed"), ("assessed", "shortlisted")])

    def test_cannot_skip_a_step(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.move(State.SHORTLISTED)
        state = self.conn.execute("SELECT state FROM applications WHERE id=?", (self.app_id,)).fetchone()["state"]
        self.assertEqual(state, "applied")  # rolled back, untouched

    def test_terminal_states_have_no_exits(self) -> None:
        self.move(State.SCREENED)
        self.move(State.REJECTED)
        for target in State:
            with self.assertRaises(InvalidTransition):
                self.move(target)

    def test_transition_table_matches_every_pair(self) -> None:
        allowed = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
        self.assertEqual(allowed, {
            (State.APPLIED, State.SCREENED), (State.SCREENED, State.ASSESSED), (State.SCREENED, State.REJECTED),
            (State.ASSESSED, State.SHORTLISTED), (State.ASSESSED, State.REJECTED)})

    def test_unknown_application(self) -> None:
        with self.assertRaises(ApplicationNotFound):
            with transaction(self.conn):
                state_machine.transition(self.conn, "missing", State.SCREENED, "x", 1.0)

    def test_events_cannot_be_updated_or_deleted(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE events SET reason = 'tampered'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM events")
        self.assertEqual(len(state_machine.history(self.conn, self.app_id)), 1)


if __name__ == "__main__":
    unittest.main()
