# coding: utf-8

from __future__ import division

import glob
import os
import unittest

from monty.json import MontyDecoder
from monty.serialization import loadfn
from monty.tempfile import ScratchDir
from pymatgen.core.structure import Structure
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar, Potcar

from doped.pycdt.utils.vasp import *

__status__ = "Development"

file_loc = os.path.abspath(os.path.join(__file__, "..", "..", "..", "test_files"))


class PotcarSingleModTest(unittest.TestCase):
    """
    This test is applicable for the specific case where POTCAR files are not
    organized according to The Materials Project directory layout, but
    in the default layout.
    """

    def setUp(self):
        pass

    def test_from_symbol_and_functional(self):
        try:
            potcar = PotcarSingleMod.from_symbol_and_functional("Ni")
        except:
            potcar = None

        self.assertIsNotNone(potcar)


class PotcarModTest(unittest.TestCase):
    def setUp(self):
        pass

    def test_set_symbols(self):
        try:
            potcar = PotcarMod(symbols=["Ni", "O"])
        except:
            potcar = None

        self.assertIsNotNone(potcar)


class DefectRelaxTest(unittest.TestCase):
    def setUp(self):
        self.structure = Structure.from_file(os.path.join(file_loc, "POSCAR_Cr2O3"))
        self.user_settings = loadfn(os.path.join(file_loc, "test_vasp_settings.yaml"))
        self.path = "Cr2O3"
        self.neutral_def_incar_min = {
            "LVHAR": True,
            "ISYM": 0,
            "ISMEAR": 0,
            "ISIF": 2,
            "ISPIN": 2,
        }
        self.def_keys = ["EDIFF", "EDIFFG", "IBRION"]

    def test_neutral_defect_incar(self):
        drs = DefectRelaxSet(self.structure)
        self.assertTrue(self.neutral_def_incar_min.items() <= drs.incar.items())
        self.assertTrue(set(self.def_keys).issubset(drs.incar))

    def test_charged_defect_incar(self):
        drs = DefectRelaxSet(self.structure, charge=1)
        self.assertIn("NELECT", drs.incar)
        self.assertTrue(self.neutral_def_incar_min.items() <= drs.incar.items())
        self.assertTrue(set(self.def_keys).issubset(drs.incar))

    def test_user_settings_defect_incar(self):
        user_incar_settings = {"EDIFF": 1e-8, "EDIFFG": 0.1, "ENCUT": 720}
        drs = DefectRelaxSet(
            self.structure, charge=1, user_incar_settings=user_incar_settings
        )
        self.assertTrue(self.neutral_def_incar_min.items() <= drs.incar.items())
        self.assertTrue(set(self.def_keys).issubset(drs.incar))
        self.assertEqual(drs.incar["ENCUT"], 720)
        self.assertEqual(drs.incar["EDIFF"], 1e-8)
        self.assertEqual(drs.incar["EDIFFG"], 0.1)


if __name__ == "__main__":
    unittest.main()
