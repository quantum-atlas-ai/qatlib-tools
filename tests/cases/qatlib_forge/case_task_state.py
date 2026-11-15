"""V2 task card control-marker routing tests."""

import tests._bootstrap as _bootstrap  # noqa: F401
import unittest
from qatlib_forge import task_state as ts

HEAD = 'a' * 40


class WorkflowTests(unittest.TestCase):
    def test_draft_and_ready_have_identical_solver_routing(self):
        for state in ('partial-solved', 'complete-solved'):
            results = [ts.classify({'head': HEAD, 'draft': draft,
                       'body': ts.replace_control('Card', HEAD, state)}) for draft in (True, False)]
            self.assertEqual([r['route'] for r in results], ['reviewer', 'reviewer'])
            self.assertTrue(all(not r['mathematical_acceptance'] for r in results))

    def test_displayed_status_mismatch_requires_review(self):
        body = ts.status_line('working') + '\n' + ts.marker(HEAD, 'complete-solved')
        self.assertEqual(ts.classify({'head': HEAD, 'draft': False, 'body': body})['route'], 'manual-review')

    def test_legacy_states_do_not_route_to_old_roles(self):
        for state in ('proved', 'rework-failed', 'absorb-ready', 'complete-constructed'):
            body = '<!-- qatlas-task {"head":"' + HEAD + '","state":"' + state + '"} -->'
            self.assertEqual(ts.classify({'head': HEAD, 'draft': False, 'body': body})['route'], 'manual-review')
            with self.assertRaises(ValueError):
                ts.marker(HEAD, state)

    def test_conflicting_and_stale_markers_require_review(self):
        for body in (ts.marker('b' * 40, 'complete-solved'),
                     ts.marker(HEAD, 'working') + '\n' + ts.marker(HEAD, 'complete-solved')):
            self.assertEqual(ts.classify({'head': HEAD, 'draft': True, 'body': body})['route'], 'manual-review')

    def test_examples_and_nested_comments_do_not_control_task(self):
        body = ('```text\n' + ts.marker(HEAD, 'working') + '\n```\n<!-- example\n'
                + ts.marker(HEAD, 'working') + '\n-->\n' + ts.marker(HEAD, 'partial-solved'))
        self.assertEqual(ts.classify({'head': HEAD, 'draft': True, 'body': body})['state'], 'partial-solved')

    def test_card_replacement_preserves_goals_and_dependencies(self):
        body = ts.replace_control('G1 -> Base.Core\nG2 depends on G1.', HEAD, 'working')
        new = ts.replace_control(body, HEAD, 'complete-solved')
        self.assertIn('G2 depends on G1.', new)
        self.assertEqual(len(ts.controls(new)), 1)
        self.assertEqual(ts.classify({'head': HEAD, 'draft': True, 'body': new})['state'], 'complete-solved')

    def test_replace_preserves_identical_fenced_example(self):
        example = '```text\n' + ts.marker(HEAD, 'working') + '\n```'
        body = example + '\n' + ts.marker(HEAD, 'working') + '\nG1: Base.Core'
        result = ts.replace_control(body, HEAD, 'complete-solved')
        self.assertIn(example, result)
        self.assertEqual(ts.controls(result), [{'head': HEAD, 'state': 'complete-solved'}])


if __name__ == '__main__':
    unittest.main()
