"""Public command layout, local-body exclusion, and written universe parameters."""
from pathlib import Path
import unittest

from qatlib_search.lean_doc_source import parse_module_text


class LeanDocSourceLayoutTests(unittest.TestCase):
    def parse(self, source):
        return parse_module_text(source, Path('Qatlib/Concepts/Layout.lean'), Path('.'))

    def test_indented_top_level_theorem_after_a_proof_keeps_its_doc_and_context(self):
        source = '''module
@[expose] public section
namespace Qatlib.Concepts.Layout
variable (n : Nat)
theorem first : n = n := by
  rfl
 /-- The indented public identity. -/
 @[simp] theorem indented : n = n := by
   rfl
/-- The next public identity. -/
theorem after : n = n := rfl
end Qatlib.Concepts.Layout
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], [
            'Qatlib.Concepts.Layout.first', 'Qatlib.Concepts.Layout.indented',
            'Qatlib.Concepts.Layout.after'])
        self.assertEqual(declarations[1].doc, 'The indented public identity.')
        self.assertEqual(declarations[1].signature, ': n = n')
        self.assertEqual(declarations[1].variable_context, ('variable (n : Nat)',))
        self.assertEqual(declarations[2].doc, 'The next public identity.')

    def test_indented_namespace_section_and_variable_commands_restore_scope(self):
        source = '''namespace Example
  section Context
  variable (n : Nat)
  /-- A local variable is written in the exported signature context. -/
  theorem inside : n = n := by
    rfl
  end Context
  /-- Section variables have gone out of scope. -/
  theorem outside : True := True.intro
end Example
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], ['Example.inside', 'Example.outside'])
        self.assertEqual(declarations[0].variable_context, ('variable (n : Nat)',))
        self.assertEqual(declarations[1].variable_context, ())

    def test_declaration_like_commands_in_a_by_body_are_not_exported(self):
        source = '''namespace Example
theorem outer : True := by
  /-- A proof-local helper must not enter the public index. -/
  theorem localHelper : True := True.intro
  namespace Fake
  variable (wrong : Nat)
  end Fake
  exact True.intro
 /-- The next top-level result. -/
 theorem next : True := True.intro
end Example
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], ['Example.outer', 'Example.next'])
        self.assertEqual(declarations[1].variable_context, ())
        self.assertEqual(declarations[1].doc, 'The next top-level result.')

    def test_where_helpers_are_not_exported_or_attached_to_the_next_result(self):
        source = '''namespace Example
def outer (n : Nat) : Nat := helper n
where
  /-- A private auxiliary definition. -/
  def helper (n : Nat) : Nat := n
  /-- A private auxiliary fact. -/
  theorem hidden : True := True.intro
 /-- A public fact after the auxiliary block. -/
 theorem next : True := True.intro
end Example
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], ['Example.outer', 'Example.next'])
        self.assertEqual(declarations[1].doc, 'A public fact after the auxiliary block.')

    def test_anonymous_examples_still_own_their_proof_bodies(self):
        source = '''example : True := by
  /-- This belongs to the anonymous proof. -/
  theorem hidden : True := True.intro
  exact True.intro
/-- A named public result. -/
theorem actual : True := True.intro
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], ['actual'])
        self.assertEqual(declarations[0].doc, 'A named public result.')

    def test_column_zero_comments_do_not_end_an_active_proof(self):
        source = '''theorem outer : True := by
  have h : True := True.intro
/-- A misleading comment inside the proof. -/
  theorem localHelper : True := True.intro
  exact h
theorem next : True := True.intro
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], ['outer', 'next'])
        self.assertEqual(declarations[1].doc, '')

    def test_universe_parameters_stay_in_the_signature_without_a_name_suffix(self):
        source = '''namespace Example
/-- An abstract universe-polymorphic interface. -/
structure HSPInterface.{u, v} (G : Type u) where
  Solver : G → Type v
/-- A universe-polymorphic root declaration. -/
def _root_.identity.{u} (G : Type u) (g : G) : G := g
end Example
'''
        declarations = self.parse(source).decls
        self.assertEqual([d.fqn for d in declarations], ['Example.HSPInterface', 'identity'])
        self.assertEqual(declarations[0].signature, '.{u, v} (G : Type u)')
        self.assertEqual(declarations[1].signature, '.{u} (G : Type u) (g : G) : G')


if __name__ == '__main__':
    unittest.main()
