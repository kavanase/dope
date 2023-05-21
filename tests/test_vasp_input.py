import os
import shutil
import unittest

from monty.serialization import loadfn
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar

from doped import vasp_input


class VaspInputTestCase(unittest.TestCase):
    def setUp(self):
        self.DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
        self.CDTE_DATA_DIR = os.path.join(self.DATA_DIR, "CdTe")
        self.cdte_defects_generator = loadfn(
            os.path.join(self.CDTE_DATA_DIR, "CdTe_defects_generator.json")
        )

    def tearDown(self):
        for folder in os.listdir(
            self.CDTE_DATA_DIR
        ):  # remove all generated CdTe defect folders
            if os.path.isdir(folder):
                shutil.rmtree(folder)

    def check_generated_vasp_inputs(
        self,
        data_dir=None,
        generated_dir=".",
        vasp_type="vasp_gam",
        check_poscar=True,
        check_potcar_spec=False,
        single_defect_dir=False,
    ):
        def _check_single_vasp_dir(
            data_dir=None,
            generated_dir=".",
            folder="",
            vasp_type="vasp_gam",
            check_poscar=True,
            check_potcar_spec=False,
        ):
            print(f"{generated_dir}/{folder}")
            self.assertTrue(os.path.exists(f"{generated_dir}/{folder}"))
            self.assertTrue(os.path.exists(f"{generated_dir}/{folder}/{vasp_type}"))

            # load the Incar, Poscar and Kpoints and check it matches the previous:
            test_incar = Incar.from_file(f"{data_dir}/{folder}/{vasp_type}/INCAR")
            incar = Incar.from_file(f"{generated_dir}/{folder}/{vasp_type}/INCAR")
            # remove NUPDOWN and NELECT entries from test_incar:
            test_incar.pop("NUPDOWN", None)
            incar.pop("NUPDOWN", None)
            test_incar.pop("NELECT", None)
            incar.pop("NELECT", None)
            self.assertEqual(test_incar, incar)

            if check_poscar:
                test_poscar = Poscar.from_file(
                    f"{data_dir}/{folder}/vasp_gam/POSCAR"  # POSCAR always checked
                    # against vasp_gam unperturbed POSCAR
                )
                poscar = Poscar.from_file(
                    f"{generated_dir}/{folder}/{vasp_type}/POSCAR"
                )
                self.assertEqual(test_poscar.structure, poscar.structure)

            if check_potcar_spec:
                with open(
                    f"{generated_dir}/{folder}/{vasp_type}/POTCAR.spec", "r"
                ) as file:
                    contents = file.readlines()
                    self.assertIn(contents[0], ["Cd", "Cd\n"])
                    self.assertIn(contents[1], ["Te", "Te\n"])
                    if "Se" in folder:
                        self.assertIn(contents[2], ["Se", "Se\n"])

            test_kpoints = Kpoints.from_file(f"{data_dir}/{folder}/{vasp_type}/KPOINTS")
            kpoints = Kpoints.from_file(f"{generated_dir}/{folder}/{vasp_type}/KPOINTS")
            self.assertDictEqual(test_kpoints.as_dict(), kpoints.as_dict())

        if data_dir is None:
            data_dir = self.CDTE_DATA_DIR

        if single_defect_dir:
            _check_single_vasp_dir(
                data_dir=data_dir,
                generated_dir=generated_dir,
                folder="",
                vasp_type=vasp_type,
                check_poscar=check_poscar,
                check_potcar_spec=check_potcar_spec,
            )

        else:
            for folder in os.listdir(data_dir):
                if os.path.isdir(f"{data_dir}/{folder}"):
                    _check_single_vasp_dir(
                        data_dir=data_dir,
                        generated_dir=generated_dir,
                        folder=folder,
                        vasp_type=vasp_type,
                        check_poscar=check_poscar,
                        check_potcar_spec=check_potcar_spec,
                    )

    def test_vasp_gam_files(self):
        vasp_input.vasp_gam_files(
            self.cdte_defects_generator,
            user_incar_settings={"ENCUT": 350},
            potcar_spec=True,
            user_potcar_functional=None,
        )

        # assert that the same folders in self.CDTE_DATA_DIR are present in the current directory
        self.check_generated_vasp_inputs(check_potcar_spec=True)

        # test custom POTCAR choice:
        # dict call acts on DefectsGenerator.defect_entries dict attribute
        vac_Te_dict = {defect_species: defect_entry for defect_species, defect_entry in
                       self.cdte_defects_generator.items() if "v_Te" in defect_species}

        vasp_input.vasp_gam_files(
            vac_Te_dict,
            user_potcar_settings={"Cd": "Cd_sv_GW", "Te": "Te_sv_GW"},
            user_potcar_functional=None,
            potcar_spec=True,
        )

        with open("v_Te_s1_0/vasp_gam/POTCAR.spec", "r") as file:
            contents = file.readlines()
            self.assertIn(contents[0], ["Cd_sv_GW", "Cd_sv_GW\n"])
            self.assertIn(contents[1], ["Te_sv_GW", "Te_sv_GW\n"])

        # test no files written with write_files=False
        self.tearDown()
        vasp_input.vasp_gam_files(
            self.cdte_defects_generator,
            user_potcar_functional=None,
            write_files=False,
        )
        for folder in os.listdir(self.CDTE_DATA_DIR):
            if os.path.isdir(f"{self.CDTE_DATA_DIR}/{folder}"):
                self.assertFalse(os.path.exists(f"./{folder}"))

    def test_vasp_gam_files_single_defect_entry(self):
        # dict call acts on DefectsGenerator.defect_entries dict attribute:
        single_defect_entry = self.cdte_defects_generator["v_Cd_s0_0"]  # V_Cd
        vasp_input.vasp_gam_files(
            single_defect_entry,
            user_incar_settings={"ENCUT": 350},
            potcar_spec=True,
            user_potcar_functional=None,
        )

        # assert that the same folders in self.CDTE_DATA_DIR are present in the current directory
        self.check_generated_vasp_inputs(
            generated_dir="v_Cd_s0_0",
            data_dir=f"{self.CDTE_DATA_DIR}/v_Cd_s0_0",
            check_potcar_spec=True,
            single_defect_dir=True,
        )

        # assert only neutral directory written:
        self.assertFalse(os.path.exists("v_Cd_s0_-1"))

        # test custom POTCAR choice:
        # dict call acts on DefectsGenerator.defect_entries dict attribute
        vac_Te_dict = {defect_species: defect_entry for defect_species, defect_entry in
                       self.cdte_defects_generator.items() if "v_Te" in defect_species}

        vasp_input.vasp_gam_files(
            vac_Te_dict,
            user_potcar_settings={"Cd": "Cd_sv_GW", "Te": "Te_sv_GW"},
            user_potcar_functional=None,
            potcar_spec=True,
        )

        with open("v_Te_s1_0/vasp_gam/POTCAR.spec", "r") as file:
            contents = file.readlines()
            self.assertIn(contents[0], ["Cd_sv_GW", "Cd_sv_GW\n"])
            self.assertIn(contents[1], ["Te_sv_GW", "Te_sv_GW\n"])

        # test no files written with write_files=False
        self.tearDown()
        vasp_input.vasp_gam_files(
            vac_Te_dict, user_potcar_functional=None, write_files=False
        )
        self.assertFalse(os.path.exists(f"v_Te_s1_0"))

    def test_vasp_std_files(self):
        vasp_input.vasp_std_files(
            self.cdte_defects_generator,
            user_incar_settings={
                "ENCUT": 350,
                "LREAL": "Auto",
                "IBRION": 5,
                "ADDGRID": True,
            },
            user_potcar_functional=None,
        )

        # assert that the same folders in self.CDTE_DATA_DIR are present in the current directory
        self.check_generated_vasp_inputs(vasp_type="vasp_std", check_poscar=False)

        vasp_input.vasp_std_files(
            self.cdte_defects_generator,
            user_incar_settings={
                "ENCUT": 350,
                "LREAL": "Auto",
                "IBRION": 5,
                "ADDGRID": True,
            },
            unperturbed_poscar=True,
            user_potcar_functional=None,
        )
        self.check_generated_vasp_inputs(vasp_type="vasp_std", check_poscar=True)

        # test no files written with write_files=False
        self.tearDown()
        vasp_input.vasp_std_files(
            self.cdte_defects_generator,
            user_potcar_functional=None,
            write_files=False,
        )
        for folder in os.listdir(self.CDTE_DATA_DIR):
            if os.path.isdir(f"{self.CDTE_DATA_DIR}/{folder}"):
                self.assertFalse(os.path.exists(f"./{folder}"))

    def test_vasp_std_files_single_defect_entry(self):
        # test interstitials this time:
        single_defect_entry = self.cdte_defects_generator["Cd_i_m1b_+2"]  # Cd_i
        # TODO: Fails because defect_entry.name is nuked when reloading from json, will fix!
        vasp_input.vasp_std_files(
            single_defect_entry,
            user_incar_settings={
                "ENCUT": 350,
                "LREAL": "Auto",
                "IBRION": 5,
                "ADDGRID": True,
            },
            user_potcar_functional=None,
        )

        # assert that the same folders in self.CDTE_DATA_DIR are present in the current directory
        print(os.listdir())
        self.check_generated_vasp_inputs(
            generated_dir="Cd_i_m1b_+2",
            data_dir=f"{self.CDTE_DATA_DIR}/Cd_i_m1b_+2",
            vasp_type="vasp_std",
            check_poscar=False,
            single_defect_dir=True,
        )

        # test unperturbed POSCAR:
        vasp_input.vasp_std_files(
            single_defect_dict,
            user_incar_settings={
                "ENCUT": 350,
                "LREAL": "Auto",
                "IBRION": 5,
                "ADDGRID": True,
            },
            user_potcar_functional=None,
            unperturbed_poscar=True,
        )

        self.check_generated_vasp_inputs(
            generated_dir=f"Cd_i_m1b_+2",
            data_dir=f"{self.CDTE_DATA_DIR}/Cd_i_m1b_+2",
            vasp_type="vasp_std",
            check_poscar=False,
            single_defect_dir=True,
        )

        # test no files written with write_files=False
        self.tearDown()
        vasp_input.vasp_gam_files(
            single_defect_entry, user_potcar_functional=None, write_files=False
        )
        self.assertFalse(os.path.exists(f"Cd_i_m1b_+2"))

    def test_vasp_ncl_files(self):
        vasp_input.vasp_ncl_files(
            self.cdte_defects_generator,
            user_incar_settings={"ENCUT": 750, "LREAL": True, "ADDGRID": False},
            user_potcar_functional=None,
        )

        # assert that the same folders in self.CDTE_DATA_DIR are present in the current directory
        self.check_generated_vasp_inputs(vasp_type="vasp_ncl", check_poscar=False)

        vasp_input.vasp_ncl_files(
            self.cdte_defects_generator,
            user_incar_settings={"ENCUT": 750, "LREAL": True, "ADDGRID": False},
            unperturbed_poscar=True,
            user_potcar_functional=None,
        )
        self.check_generated_vasp_inputs(vasp_type="vasp_ncl", check_poscar=True)

        # test no files written with write_files=False
        self.tearDown()
        vasp_input.vasp_ncl_files(
            self.cdte_defects_generator,
            user_potcar_functional=None,
            write_files=False,
        )
        for folder in os.listdir(self.CDTE_DATA_DIR):
            if os.path.isdir(f"{self.CDTE_DATA_DIR}/{folder}"):
                self.assertFalse(os.path.exists(f"./{folder}"))

    def test_vasp_ncl_files_single_defect_dict(self):
        # test substitutions this time:
        single_defect_dict = self.cdte_generated_defect_dict["substitutions"][
            0
        ]  # sub_2_Se_on_Te
        vasp_input.vasp_ncl_files(
            single_defect_dict,
            user_incar_settings={"ENCUT": 750, "LREAL": True, "ADDGRID": False},
            user_potcar_functional=None,
        )

        # assert that the same folders in self.CDTE_DATA_DIR are present in the current directory
        for charge in range(-1, 6):
            self.check_generated_vasp_inputs(
                generated_dir=f"sub_2_Se_on_Te_{charge}",
                data_dir=f"{self.CDTE_DATA_DIR}/sub_2_Se_on_Te_{charge}",
                vasp_type="vasp_ncl",
                check_poscar=False,
                single_defect_dir=True,
            )

        # test unperturbed POSCAR:
        vasp_input.vasp_ncl_files(
            single_defect_dict,
            user_incar_settings={"ENCUT": 750, "LREAL": True, "ADDGRID": False},
            user_potcar_functional=None,
            unperturbed_poscar=True,
        )

        for charge in range(-1, 6):
            self.check_generated_vasp_inputs(
                generated_dir=f"sub_2_Se_on_Te_{charge}",
                data_dir=f"{self.CDTE_DATA_DIR}/sub_2_Se_on_Te_{charge}",
                vasp_type="vasp_ncl",
                check_poscar=True,
                single_defect_dir=True,
            )

        # test no files written with write_files=False
        self.tearDown()
        vasp_input.vasp_ncl_files(
            single_defect_dict, user_potcar_functional=None, write_files=False
        )
        for charge in range(-1, 6):
            self.assertFalse(os.path.exists(f"sub_2_Se_on_Te_{charge}"))


if __name__ == "__main__":
    unittest.main()
