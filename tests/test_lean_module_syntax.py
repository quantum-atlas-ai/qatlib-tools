"""Shared production lexer regressions retained from the retired retrieval suite."""
from pathlib import Path
import unittest
from qatlib_search.lean_doc_source import parse_module_text
from qatlib_search.lean_declarations import declaration_kinds
from qatlib_search.lean_source import mask_static_tactic_macros

class ModuleSyntaxTests(unittest.TestCase):
    def parse(self, source):
        return parse_module_text(source, Path('Qatlib/Concepts/Flat.lean'), Path('.'))

    def assert_public_names(self, source, expected):
        self.assertEqual({'.'.join(name) for name in declaration_kinds(source)}, expected)
        self.assertEqual({decl.fqn for decl in self.parse(source).decls}, expected)

    def test_module_visibility_follows_nested_public_section_scope(self):
        source = '''module
public import Mathlib.Data.Nat.Basic
namespace Example
/-- A module-local helper. -/
def hidden : Nat := 0
/-- An explicitly exported value. -/
public def explicit : Nat := 1
public section Exports
variable (n : Nat)
noncomputable section Internal
/-- A section-exported identity. -/
theorem inside : n = n := rfl
/-- A private helper inside a public section. -/
private theorem hiddenInside : n = n := rfl
end Internal
namespace Nested
/-- A nested exported identity. -/
theorem nested : n = n := rfl
end Nested
end Exports
/-- A helper after the public section closes. -/
def hiddenAfter : Nat := 2
/-- An explicitly exported theorem after the section. -/
public theorem outside : True := True.intro
end Example
'''
        self.assert_public_names(source, {
            'Example.explicit', 'Example.inside', 'Example.Nested.nested', 'Example.outside'})
        decls = {decl.fqn: decl for decl in self.parse(source).decls}
        self.assertEqual(decls['Example.inside'].variable_context, ('variable (n : Nat)',))
        self.assertEqual(decls['Example.outside'].variable_context, ())
        self.assertEqual(decls['Example.inside'].doc, 'A section-exported identity.')

    def test_staging_header_preserves_attributed_public_section_and_docstrings(self):
        source = '''\ufeff/- Copyright and license. -/
module
public import Qatlib.Concepts.Shared
public meta import Lean

@[expose] public section
/-!
# An exported module

Exported results from the staging syntax.
-/
noncomputable section
namespace Qatlib.Concepts.Flat
/-- The exported bound holds. -/
theorem bound : 0 ≤ (1 : Nat) := Nat.zero_le 1
end Qatlib.Concepts.Flat
'''
        self.assert_public_names(source, {'Qatlib.Concepts.Flat.bound'})
        module = self.parse(source)
        self.assertEqual(module.header, 'Exported results from the staging syntax.')
        self.assertEqual(module.decls[0].doc, 'The exported bound holds.')
        self.assertEqual(module.decls[0].signature, ': 0 ≤ (1 : Nat)')

    def test_comments_and_strings_do_not_change_module_visibility(self):
        source = '''module
-- public section
/- public section
theorem fake : True := True.intro
-/
public def text : String := "public section
theorem fakeString : True := True.intro"
def hidden : Nat := 0
public theorem exported : True := True.intro
'''
        self.assert_public_names(source, {'text', 'exported'})
        legacy = '''/- module -/
def module' : Nat := 0
public theorem exported : True := True.intro
'''
        self.assert_public_names(legacy, {"module'", 'exported'})

    def test_module_header_does_not_enable_generated_declaration_scanning(self):
        source = 'module\npublic section\nrun_cmd pure ()\n'
        for parser in (declaration_kinds, self.parse):
            with self.subTest(parser=parser.__name__):
                with self.assertRaisesRegex(ValueError, 'generated declarations'):
                    parser(source)

    def test_parameterized_term_application_macros_keep_following_declarations(self):
        source = '''module
public section
namespace Example
/-- Internal matrix notation. -/
macro "matmul " a:term:max b:term:max : term =>
  `(@HMul.hMul _ _ _ Matrix.instHMulOfFintypeOfMulOfAddCommMonoid $a $b)
/-- The exported multiplication identity. -/
theorem exported : True := True.intro
end Example
'''
        masked = mask_static_tactic_macros(source)
        self.assertEqual(len(masked), len(source))
        self.assertEqual([i for i, char in enumerate(masked) if char == '\n'],
                         [i for i, char in enumerate(source) if char == '\n'])
        self.assert_public_names(source, {'Example.exported'})
        declaration, = self.parse(source).decls
        self.assertEqual(declaration.doc, 'The exported multiplication identity.')
        self.assertEqual(declaration.line, source[:source.index('theorem exported')].count('\n') + 1)

    def test_term_macro_argument_splices_and_parentheses_are_structural(self):
        for prefix in ('', 'local ', 'scoped '):
            with self.subTest(prefix=prefix):
                source = prefix + 'macro "pair " a:term b:term:42 : term => `(term| Prod.mk ($a) ($b))\n'
                source += 'theorem result : True := True.intro\n'
                self.assert_public_names(source, {'result'})

    def test_term_macro_metaprograms_and_undeclared_splices_fail_closed(self):
        unsupported = [
            'macro "bad " a:term : command => `(command| theorem fake : True := by trivial)',
            'macro "bad " a:term : term => `(by run_tac addDecl)',
            'macro "bad " a:term : term => do pure a',
            'macro "bad " a:term : term => `($missing)',
            'macro "bad " a:term : term => `($(a))',
            'macro "bad " a:term : term => `(`($a))',
            'macro "bad " a:term a:term : term => `($a)',
            'macro "bad " a:term : term => `(Prod.mk ($a])',
            'macro "bad " a:term : term => `(Prod.mk $a) >>= pure',
            'macro "bad " a:term : term => `(Prod.mk $a)\n  >>= pure',
            'macro "bad " a:term : term => `($a)\nrun_cmd pure ()',
        ]
        for body in unsupported:
            for parser in (declaration_kinds, self.parse):
                with self.subTest(body=body, parser=parser.__name__):
                    with self.assertRaises(ValueError):
                        parser(body + '\ntheorem real : True := True.intro\n')

    def test_comments_and_strings_do_not_impersonate_term_macros(self):
        source = '''/- macro "bad " a:term : term => `($missing) -/
def message := "macro not executable"
theorem result : True := True.intro
'''
        self.assert_public_names(source, {'message', 'result'})
