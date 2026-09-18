"""Regression tests for the asc_core.analysis package (CFG)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from dex_fixture import make_dex
from droidasc.asc_core.utils.tinydex import DEX
from droidasc.asc_core.analysis.cfg import (
    build_method_cfg, cfg_to_dot, cfg_to_json, decode_instructions, build_blocks,
)


def _fixture_dex():
    return DEX.parse(memoryview(make_dex()), 'fixture.dex')


class TinydexGetClassTests(unittest.TestCase):
    def test_get_class(self):
        dex = _fixture_dex()
        self.assertIsNone(dex.get_class('Lexample/Missing;'))
        clazz = dex.get_class('Lexample/Test;')
        self.assertIsNotNone(clazz)
        self.assertEqual({m.name for m in clazz.methods}, {'first', 'second'})


class CfgTests(unittest.TestCase):
    def test_cfg_fixture_method(self):
        dex = _fixture_dex()
        cfg = build_method_cfg(dex, 'Lexample/Test;', 'first')
        self.assertEqual(len(cfg['blocks']), 1)
        dot = cfg_to_dot(cfg)
        self.assertIn('digraph CFG', dot)
        self.assertIn('const-string v0', dot)
        data = cfg_to_json(cfg)
        self.assertIn('"method": "Lexample/Test;->first"', data)

    def test_cfg_unknown_method_raises(self):
        with self.assertRaises(ValueError):
            build_method_cfg(_fixture_dex(), 'Lexample/Test;', 'missing')

    def test_branch_splits_blocks(self):
        # const/4 v0,#0 ; if-eqz v0,+4u -> 10 ; const-string v0,@0 ; return-void
        code = bytes([
            0x12, 0x10,
            0x38, 0x00, 0x04, 0x00,
            0x1a, 0x00, 0x00, 0x00,
            0x0e, 0x00,
        ])
        insns = decode_instructions(code)
        self.assertEqual(insns[1].target, 10)
        blocks = build_blocks(insns)
        self.assertEqual(len(blocks), 3)
        kinds = sorted(kind for _t, kind, _l in blocks[0].edges)
        self.assertEqual(kinds, ['conditional_false', 'conditional_true'])

    def test_packed_switch_payload(self):
        sw = bytes([
            0x2b, 0x00, 0x04, 0x00, 0x00, 0x00,   # packed-switch v0, +4u -> payload at 8
            0x0e, 0x00,                           # 6: return-void
            0x00, 0x01,                           # 8: ident 0x0100
            0x02, 0x00,                           # 10: size 2
            0x05, 0x00, 0x00, 0x00,               # 12: first_key 5
            0x03, 0x00, 0x00, 0x00,               # 16: rel +3u -> 6
            0x03, 0x00, 0x00, 0x00,               # 20: rel +3u -> 6
        ])
        insns = decode_instructions(sw)
        self.assertEqual(insns[0].payload_off, 8)
        self.assertEqual(insns[0].case_targets, [(5, 6), (6, 6)])
        self.assertTrue(insns[2].is_payload)


if __name__ == "__main__":
    unittest.main()
