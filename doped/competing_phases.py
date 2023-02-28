import contextlib
import copy
from pathlib import Path, PurePath
import warnings
import json

import pandas as pd

from pymatgen.ext.matproj import MPRester
from pymatgen.entries.computed_entries import ComputedStructureEntry
from pymatgen.analysis.phase_diagram import PhaseDiagram, PDEntry
from pymatgen.io.vasp.sets import DictSet
from pymatgen.io.vasp.inputs import Kpoints, UnknownPotcarWarning
from pymatgen.io.vasp.outputs import Vasprun
from pymatgen.core import Structure, Composition, Element
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

# globally ignore this shit
warnings.filterwarnings("ignore", category=UnknownPotcarWarning)
warnings.filterwarnings("ignore", message="No POTCAR file with matching TITEL fields")

# TODO: Currently the format for user defined `incar` and `potcar` settings is somewhat
#  inconsistent between `competing_phases` and `vasp_input`, and `pymatgen`. Ideally should all
#  correspond to `pymatgen`'s `DictSet` format.
# TODO: Add warning for when input `potcar_settings` don't match the expected format (i.e. if one
#  of the dict entries is not an element symbol)


class ChemPotAnalyzer:
    """
    Post processing for atomic chemical potentials used in defect calculations.
    """

    def __init__(self, **kwargs):
        """
        Args:
            bulk_ce: Pymatgen ComputedStructureEntry object for bulk entry / supercell
        """
        self.bulk_ce = kwargs.get("bulk_ce", None)

    def get_chempots_from_pd(self, pd):

        if not self.bulk_ce:
            msg = (
                "No bulk entry supplied. "
                "Cannot compute atomic chempots without knowing the bulk entry of interest."
            )
            raise ValueError(msg)

        bulk_composition = self.bulk_ce.composition
        redcomp = bulk_composition.reduced_composition
        # append bulk_ce to phase diagram, if not present
        entries = pd.all_entries
        if not any(
            [
                (
                    ent.composition == self.bulk_ce.composition
                    and ent.energy == self.bulk_ce.energy
                )
                for ent in entries
            ]
        ):
            entries.append(
                PDEntry(
                    self.bulk_ce.composition,
                    self.bulk_ce.energy,
                    attribute="Bulk Material",
                )
            )
            pd = PhaseDiagram(entries)

        chem_lims = pd.get_all_chempots(redcomp)

        return chem_lims

    def diff_bulk_sub_phases(self, face_list, sub_el=None):
        # method for pulling out phases within a facet of a phase diagram
        # which may include a substitutional element...
        # face_list is an array of phases in a facet
        # sub_el is the element to look out for within the face_list array
        blk = []
        sub_spcs = []
        for face in face_list:
            if sub_el:
                if sub_el in face:
                    sub_spcs.append(face)
                else:
                    blk.append(face)
            else:
                blk.append(face)
        blk.sort()
        sub_spcs.sort()
        blknom = "-".join(blk)
        subnom = "-".join(sub_spcs)
        return blk, blknom, subnom


class CompetingPhases:
    """
    Class to generate the input files for competing phases on the phase diagram for the host
    material (determining the chemical potential limits). Materials Project (MP) data is used,
    along with an uncertainty range specified by `e_above_hull`, to determine the relevant
    competing phases. Diatomic gaseous molecules are generated as molecules-in-a-box as appropriate.

    TODO: Add full_phase_diagram option.
    """

    def __init__(self, composition, e_above_hull=0.1):
        """
        Args:
            composition (str, Composition): Composition of host material
                (e.g. 'LiFePO4', or Composition('LiFePO4'), or Composition({"Li":1, "Fe":1,
                "P":1, "O":4}))
            e_above_hull (float): Maximum energy-above-hull of Materials Project entries to be
                considered as competing phases. This is an uncertainty range for the
                MP-calculated formation energies, which may not be accurate due to functional
                choice (GGA vs hybrid DFT / GGA+U / RPA etc.), lack of vdW corrections etc.
                Any phases that would border the host material on the phase diagram, if their
                relative energy was downshifted by `e_above_hull`, are included.
                Default is 0.1 eV/atom.
        """
        # create list of entries
        molecules_in_a_box = ["H2", "O2", "N2", "F2", "Cl2"]

        # TODO: Should hard code S (solid + S8), P and Se in here too. Common anions with a lot of
        #  unnecessary polymorphs on MP

        # all data collected from materials project
        self.data = [
            "pretty_formula",
            "e_above_hull",
            "band_gap",
            "nsites",
            "volume",
            "icsd_id",
            "formation_energy_per_atom",
            "energy_per_atom",
            "energy",
            "total_magnetization",
            "nelements",
            "elements",
        ]

        # set bulk composition (Composition(Composition("LiFePO4")) = Composition("LiFePO4")))
        self.bulk_comp = Composition(composition)

        stype = "initial"
        m = MPRester()
        cp_entries = m.get_entries_in_chemsys(
            list(self.bulk_comp.as_dict().keys()),
            inc_structure=stype,
            property_data=self.data,
        )
        cp_entries = [e for e in cp_entries if e.data["e_above_hull"] <= e_above_hull]
        cp_entries.sort(key=lambda x: x.data["e_above_hull"])  # sort by e_above_hull

        pd_entries = []
        # check that none of the elemental ones are on the naughty list... (molecules in a box)
        for e in cp_entries:
            sym = SpacegroupAnalyzer(e.structure)
            struc = sym.get_primitive_standard_structure()
            if e.data["pretty_formula"] in molecules_in_a_box:
                assert (
                    e.data["e_above_hull"] == 0
                )  # should be zero as only first matching entry
                struc, formula, magnetisation = make_molecule_in_a_box(
                    e.data["pretty_formula"]
                )
                molecular_entry = ComputedStructureEntry(
                    structure=struc,
                    energy=e.energy,  # set entry energy to be hull energy
                    composition=Composition(formula),
                    parameters=None,
                )
                molecular_entry.data["oxide_type"] = "None"
                molecular_entry.data["pretty_formula"] = formula
                molecular_entry.data["e_above_hull"] = 0
                molecular_entry.data["band_gap"] = None
                molecular_entry.data["nsites"] = 2
                molecular_entry.data["volume"] = 0
                molecular_entry.data["icsd_id"] = None
                molecular_entry.data["formation_energy_per_atom"] = 0
                molecular_entry.data["energy_per_atom"] = e.data["energy_per_atom"]
                molecular_entry.data["energy"] = e.data["energy"]
                molecular_entry.data["total_magnetization"] = magnetisation
                molecular_entry.data["nelements"] = 1
                molecular_entry.data["elements"] = [formula]
                molecular_entry.data["molecule"] = True
                pd_entries.append(molecular_entry)

                # remove all other entries with the same reduced_composition
                cp_entries = [
                    e
                    for e in cp_entries
                    if e.composition.reduced_composition
                    != Composition(formula).reduced_composition
                ]

            else:
                pd_entries.append(e)
                e.data["molecule"] = False

        # cull to only include any phases that would border the host material on the phase
        # diagram, if their relative energy was downshifted by `e_above_hull`:
        pd_entries.sort(key=lambda x: x.energy_per_atom)  # sort by energy per atom
        pd = PhaseDiagram(pd_entries)
        bulk_entries = [
            entry
            for entry in pd_entries
            if entry.composition.reduced_composition
            == self.bulk_comp.reduced_composition
        ]
        bulk_ce = bulk_entries[
            0
        ]  # lowest energy entry for bulk composition (after sorting)

        cpc = ChemPotAnalyzer(bulk_ce=bulk_ce)
        MP_gga_chempots = cpc.get_chempots_from_pd(pd)

        MP_bordering_phases = set(
            [phase for facet in MP_gga_chempots.keys() for phase in facet.split("-")]
        )
        self.entries = [
            entry
            for entry in pd_entries
            if entry.name in MP_bordering_phases or entry.is_element
        ]

        # add any phases that would border the host material on the phase diagram, if their relative
        # energy was downshifted by `e_above_hull`:
        for entry in pd_entries:
            if entry.name not in MP_bordering_phases and not entry.is_element:
                # decrease entry energy per atom by `e_above_hull` eV/atom
                renormalised_entry_dict = entry.as_dict().copy()
                renormalised_entry_dict["energy"] -= e_above_hull * sum(
                    entry.composition.values()
                )
                renormalised_entry = PDEntry.from_dict(renormalised_entry_dict)
                new_pd = PhaseDiagram(pd.entries + [renormalised_entry])
                new_MP_gga_chempots = cpc.get_chempots_from_pd(new_pd)

                if new_MP_gga_chempots != MP_gga_chempots:
                    # new bordering phase, add to list
                    self.entries.append(entry)

        self.entries.sort(key=lambda x: x.data["e_above_hull"])  # sort by e_above_hull

    def convergence_setup(
        self,
        kpoints_metals=(40, 120, 5),
        kpoints_nonmetals=(5, 60, 5),
        potcar_functional="PBE_54",
        user_potcar_settings=None,
        user_incar_settings=None,
    ):
        """
        Sets up input files for kpoints convergence testing
        Args:
            kpoints_metals (tuple): Kpoint density per inverse volume (Å-3) to be tested in
                (min, max, step) format for metals
            kpoints_nonmetals (tuple): Kpoint density per inverse volume (Å-3) to be tested in
                (min, max, step) format for nonmetals
            potcar_functional (str): POTCAR functional to use (e.g. PBE_54)
            user_potcar_settings (dict): Override the default POTCARs e.g. {"Li": "Li_sv"}
            user_incar_settings (dict): Override the default INCAR settings
                e.g. {"EDIFF": 1e-5, "LDAU": False}
        Returns:
            writes input files
        """
        # by default uses pbesol, but easy to switch to pbe or
        # pbe+u by using user_incar_settings
        # user incar settings applies the same settings so both
        file = str(Path(__file__).parent.joinpath("PBEsol_config.json"))
        with open(file) as f:
            cd = json.load(f)

        # kpoints should be set as (min, max, step)
        min_nm, max_nm, step_nm = kpoints_nonmetals
        min_m, max_m, step_m = kpoints_metals

        # separate metals and non-metals
        self.nonmetals = []
        self.metals = []
        for e in self.entries:
            if not e.data["molecule"]:
                if e.data["band_gap"] > 0:
                    self.nonmetals.append(e)
                else:
                    self.metals.append(e)
            else:
                print(
                    f"{e.name} is a molecule in a box, does not need convergence testing"
                )

        for e in self.nonmetals:
            if user_incar_settings is not None:
                uis = copy.deepcopy(user_incar_settings)
            else:
                uis = {}
            if e.data["magnetisation"] > 1:  # account for magnetic moment
                if "ISPIN" not in uis:
                    uis["ISPIN"] = 2

            for kpoint in range(min_nm, max_nm, step_nm):
                dis = DictSet(
                    e.structure,
                    cd,
                    user_potcar_functional=potcar_functional,
                    user_potcar_settings=user_potcar_settings,
                    user_kpoints_settings={"reciprocal_density": kpoint},
                    user_incar_settings=uis,
                    force_gamma=True,
                )

                kname = "k" + ",".join(str(k) for k in dis.kpoints.kpts[0])
                fname = "competing_phases/{}_EaH_{}/kpoint_converge/{}".format(
                    e["formula"], float(f"{e['ehull']:.4f}"), kname
                )
                dis.write_input(fname)

        for e in self.metals:
            if user_incar_settings is not None:
                uis = copy.deepcopy(user_incar_settings)
            else:
                uis = {}
            # change the ismear and sigma for metals
            uis["ISMEAR"] = -5
            uis["SIGMA"] = 0.2

            if e.data["magnetisation"] > 1:  # account for magnetic moment
                if "ISPIN" not in uis:
                    uis["ISPIN"] = 2

            for kpoint in range(min_m, max_m, step_m):
                dis = DictSet(
                    e.structure,
                    cd,
                    user_potcar_functional=potcar_functional,
                    user_potcar_settings=user_potcar_settings,
                    user_kpoints_settings={"reciprocal_density": kpoint},
                    user_incar_settings=uis,
                    force_gamma=True,
                )

                kname = "k" + ",".join(str(k) for k in dis.kpoints.kpts[0])
                fname = "competing_phases/{}_EaH_{}/kpoint_converge/{}".format(
                    e["formula"], float(f"{e['ehull']:.4f}"), kname
                )
                dis.write_input(fname)

    def vasp_std_setup(
        self,
        kpoints_metals=95,
        kpoints_nonmetals=45,
        potcar_functional="PBE_54",
        user_potcar_settings=None,
        user_incar_settings=None,
    ):
        """
        Sets up input files for vasp_std relaxations
        Args:
            kpoints_metals (int): Kpoint density per inverse volume (Å^-3) for metals
            kpoints_nonmetals (int): Kpoint density per inverse volume (Å^-3) for nonmetals
            potcar_functional (str): POTCAR to use (e.g. PBE_54)
            user_potcar_settings (dict): Override the default POTCARs e.g. {"Li": "Li_sv"}
            user_incar_settings (dict): Override the default INCAR settings
                e.g. {"EDIFF": 1e-5, "LDAU": False}
        Returns:
            saves to file
        """
        file = str(Path(__file__).parent.joinpath("HSE06_config_relax.json"))
        with open(file) as f:
            cd = json.load(f)

        # separate metals, non-metals and molecules
        self.nonmetals = []
        self.metals = []
        self.molecules = []
        for e in self.entries:
            if e.data["molecule"]:
                self.molecules.append(e)
            else:
                if e.data["band_gap"] > 0:
                    self.nonmetals.append(e)
                else:
                    self.metals.append(e)

        for e in self.nonmetals:
            if user_incar_settings is not None:
                uis = copy.deepcopy(user_incar_settings)
            else:
                uis = {}
            if e.data["magnetisation"] > 1:  # account for magnetic moment
                if "ISPIN" not in uis:
                    uis["ISPIN"] = 2

            dis = DictSet(
                e.structure,
                cd,
                user_potcar_functional=potcar_functional,
                user_kpoints_settings={"reciprocal_density": kpoints_nonmetals},
                user_incar_settings=uis,
                user_potcar_settings=user_potcar_settings,
                force_gamma=True,
            )

            fname = "competing_phases/{}_EaH_{}/vasp_std".format(
                e["formula"], float(f"{e['ehull']:.4f}")
            )
            dis.write_input(fname)

        for e in self.metals:
            if user_incar_settings is not None:
                uis = copy.deepcopy(user_incar_settings)
            else:
                uis = {}
            # change the ismear and sigma for metals
            uis["ISMEAR"] = 1
            uis["SIGMA"] = 0.2

            if e.data["magnetisation"] > 1:  # account for magnetic moment
                if "ISPIN" not in uis:
                    uis["ISPIN"] = 2

            dis = DictSet(
                e.structure,
                cd,
                user_potcar_functional=potcar_functional,
                user_kpoints_settings={"reciprocal_density": kpoints_metals},
                user_incar_settings=uis,
                user_potcar_settings=user_potcar_settings,
                force_gamma=True,
            )
            fname = "competing_phases/{}_EaH_{}/vasp_std".format(
                e["formula"], float(f"{e['ehull']:.4f}")
            )
            dis.write_input(fname)

        for e in self.molecules:  # gamma-only for molecules
            if user_incar_settings is not None:
                uis = copy.deepcopy(user_incar_settings)
            else:
                uis = {}

            uis["ISIF"] = 2  # can't change the volume

            if e.data["magnetisation"] > 1:  # account for magnetic moment
                if "ISPIN" not in uis:
                    uis["ISPIN"] = 2
                # the molecule set up to set this automatically?
                if e["formula"] == "O2":
                    uis["NUPDOWN"] = 2

            dis = DictSet(
                e.structure,
                cd,
                user_potcar_functional=potcar_functional,
                user_kpoints_settings=Kpoints().from_dict(
                    {
                        "comment": "Gamma-only kpoints for molecule-in-a-box",
                        "generation_style": "Gamma",
                    }
                ),
                user_incar_settings=uis,
                user_potcar_settings=user_potcar_settings,
                force_gamma=True,
            )
            fname = "competing_phases/{}_EaH_{}/vasp_std".format(
                e["formula"], float(f"{e['ehull']:.4f}")
            )
            dis.write_input(fname)


class AdditionalCompetingPhases(CompetingPhases):
    """
    If you want to add some extrinsic doping, or add another element to your chemical system,
    this is the class for you. Will make sure you're only calculating the extra phases
    """

    def __init__(self, system, extrinsic_species, e_above_hull=0.1):
        """
        Args:
            system (list): Chemical system under investigation, e.g. ['Mg', 'O']
            extrinsic_species (str): Dopant species
            e_above_hull (float): Maximum considered energy above hull
        """
        # the competing phases & entries of the OG system
        super().__init__(system, e_above_hull)
        self.og_competing_phases = copy.deepcopy(self.competing_phases)
        # the competing phases & entries of the OG system + all the additional
        # stuff from the extrinsic species
        system.append(extrinsic_species)
        super().__init__(system, e_above_hull)
        self.ext_competing_phases = copy.deepcopy(self.competing_phases)

        # only keep the ones that are actually new
        self.competing_phases = []
        for ext in self.ext_competing_phases:
            if ext not in self.og_competing_phases:
                self.competing_phases.append(ext)


# separate class for read from file with this as base class? can still use different init?
class CompetingPhasesAnalyzer:
    """
    Post processing competing phases data to calculate chemical potentials.
    """

    def __init__(self, system, extrinsic_species=None):
        """
        Args:
            system (str): The  'reduced formula' of the bulk composition
            extrinsic_species (str): Dopant species
        """

        self.bulk_composition = Composition(system)
        self.elemental = [str(c) for c in self.bulk_composition.elements]
        if extrinsic_species is not None:
            self.elemental.append(extrinsic_species)
            self.extrinsic_species = extrinsic_species

    def from_vaspruns(
        self, path, folder="vasp_std", csv_fname="competing_phases_energies.csv"
    ):
        """
        Reads in vaspruns, collates energies to csv. It isn't the best at removing higher energy
        elemental phases (if multiple are present), so double check that
        Args:
            path (list, str, pathlib Path): Either a list of strings or Paths to vasprun.xml(.gz)
            files, or a path to the base folder in which you have your formula_EaH_/vasp_std/vasprun.xml
            folder (str): The folder in which vasprun is, only use if you set base path
            (ie. change to vasp_ncl, relax whatever youve called it)
            csv_fname (str): csv filename
        Returns:
            saves csv with formation energies to file
        """
        self.vasprun_paths = []
        # fetch data
        # if path is just a list of all competing phases
        if isinstance(path, list):
            for p in path:
                if Path(p).name in {"vasprun.xml", "vasprun.xml.gz"}:
                    self.vasprun_paths.append(str(Path(p)))

                # try to find the file - will always pick the first match for vasprun.xml*
                elif len(list(Path(p).glob("vasprun.xml*"))) > 0:
                    vsp = list(Path(p).glob("vasprun.xml*"))[0]
                    self.vasprun_paths.append(str(vsp))

                else:
                    print(
                        f"Can't find a vasprun.xml(.gz) file for {p}, proceed with caution"
                    )

        # if path provided points to the doped created directories
        elif isinstance(path, PurePath) or isinstance(path, str):
            path = Path(path)
            for p in path.iterdir():
                if p.glob("EaH"):
                    vp = p / folder / "vasprun.xml"
                    if vp.is_file():
                        self.vasprun_paths.append(str(vp))
                    vpg = p / folder / "vasprun.xml.gz"
                    if vpg.is_file():
                        self.vasprun_paths.append(str(vpg))
                    else:
                        print(
                            f"Can't find a vasprun.xml(.gz) file for {p}, proceed with caution"
                        )

                else:
                    raise FileNotFoundError(
                        "Folders are not in the correct structure, provide them as a list of paths (or strings)"
                    )

        else:
            raise ValueError(
                "path should either be a list of paths, a string or a pathlib Path object"
            )

        # Ignore POTCAR warnings when loading vasprun.xml
        # pymatgen assumes the default PBE with no way of changing this
        warnings.filterwarnings("ignore", category=UnknownPotcarWarning)
        warnings.filterwarnings(
            "ignore", message="No POTCAR file with matching TITEL fields"
        )

        num = len(self.vasprun_paths)
        print(f"parsing {num} vaspruns, this may take a while")
        self.vaspruns = [Vasprun(e).as_dict() for e in self.vasprun_paths]
        self.elemental_vaspruns = []
        self.data = []

        # make a fake dictionary with all elemental energies set to 0
        temp_elemental_energies = {}
        for e in self.elemental:
            temp_elemental_energies[e] = 0

        # check if elemental, collect the elemental energies per atom (for
        # formation energies)
        vaspruns_for_removal = []
        for i, v in enumerate(self.vaspruns):
            comp = [str(c) for c in Composition(v["unit_cell_formula"]).elements]
            energy = v["output"]["final_energy_per_atom"]
            if len(comp) == 1:
                for key, val in temp_elemental_energies.items():
                    if comp[0] == key and energy < val:
                        temp_elemental_energies[key] = energy
                    elif comp[0] == key and energy > val:
                        vaspruns_for_removal.append(i)

        # get rid of elemental competing phases that aren't the lowest
        # energy ones
        if vaspruns_for_removal:
            for m in sorted(vaspruns_for_removal, reverse=True):
                del self.vaspruns[m]

        temp_data = []
        self.elemental_energies = {}
        for v in self.vaspruns:
            rcf = v["reduced_cell_formula"]
            formulas_per_unit = (
                list(v["unit_cell_formula"].values())[0] / list(rcf.values())[0]
            )
            final_energy = v["output"]["final_energy"]
            kpoints = "x".join(str(x) for x in v["input"]["kpoints"]["kpoints"][0])

            # check if elemental:
            if len(rcf) == 1:
                elt = v["elements"][0]
                self.elemental_energies[elt] = v["output"]["final_energy_per_atom"]

            d = {
                "formula": v["pretty_formula"],
                "kpoints": kpoints,
                "energy_per_fu": final_energy / formulas_per_unit,
                "energy": final_energy,
            }
            temp_data.append(d)

        df = _calculate_formation_energies(temp_data, self.elemental_energies)
        df.to_csv(csv_fname, index=False)
        self.data = df.to_dict(orient="records")

    def from_csv(self, csv):
        """
        Read in data from csv. Must have columns 'formula', 'energy_per_fu', 'energy' and 'formation_energy'
        """
        df = pd.read_csv(csv)
        columns = ["formula", "energy_per_fu", "energy", "formation_energy"]
        if all(x in list(df.columns) for x in columns):
            droplist = [i for i in df.columns if i not in columns]
            df.drop(droplist, axis=1, inplace=True)
            d = df.to_dict(orient="records")
            self.data = d

        else:
            raise ValueError(
                "supplied csv does not contain the correct headers, cannot read in the data"
            )

    def calculate_chempots(self, csv_fname="chempot_limits.csv"):
        """
        Calculates chemcial potential limits. For dopant species, it calculates the limiting potential based on the intrinsic chemical potentials (i.e. same as `full_sub_approach=False` in pycdt)
        Args:
            csv_fname (str): name of csv file to which chempot limits are saved
        Retruns:
            pandas dataframe
        """

        pd_entries_intrinsic = []
        extrinsic_formation_energies = []
        for d in self.data:
            e = PDEntry(d["formula"], d["energy_per_fu"])
            # presumably checks if the phase is intrinsic
            if set(Composition(d["formula"]).elements).issubset(
                self.bulk_composition.elements
            ):
                pd_entries_intrinsic.append(e)
                if e.composition == self.bulk_composition:
                    self.bulk_pde = e
            else:
                extrinsic_formation_energies.append(
                    {"formula": d["formula"], "formation_energy": d["formation_energy"]}
                )

        self.intrinsic_phase_diagram = PhaseDiagram(
            pd_entries_intrinsic, map(Element, self.bulk_composition.elements)
        )

        # check if it's stable and if not error out
        if self.bulk_pde not in self.intrinsic_phase_diagram.stable_entries:
            raise ValueError(
                f"{self.bulk_composition.reduced_formula} is not stable with respect to competing phases"
            )

        chem_lims = self.intrinsic_phase_diagram.get_all_chempots(self.bulk_composition)
        chem_limits = {
            "facets": chem_lims,
            "elemental_refs": {
                elt: ent.energy_per_atom
                for elt, ent in self.intrinsic_phase_diagram.el_refs.items()
            },
            "facets_wrt_el_refs": {},
        }

        # do the shenanigan to relate the facets to the elemental energies
        for facet, chempot_dict in chem_limits["facets"].items():
            relative_chempot_dict = copy.deepcopy(chempot_dict)
            for e in relative_chempot_dict.keys():
                relative_chempot_dict[e] -= chem_limits["elemental_refs"][e]
            chem_limits["facets_wrt_el_refs"].update({facet: relative_chempot_dict})

        # get chemical potentials as pandas dataframe
        self.chemical_potentials = []
        for k, v in chem_limits["facets_wrt_el_refs"].items():
            lst = []
            columns = []
            for k, v in v.items():
                lst.append(v)
                columns.append(str(k))
            self.chemical_potentials.append(lst)

        # make df, will need it in next step
        df = pd.DataFrame(self.chemical_potentials, columns=columns)

        if hasattr(self, "extrinsic_species"):
            print(f"Calculating chempots for {self.extrinsic_species}")
            for e in extrinsic_formation_energies:
                for el in self.elemental:
                    e[el] = Composition(e["formula"]).as_dict()[el]

            # gets the df into a slightly more convenient dict
            cpd = df.to_dict(orient="records")
            mins = []
            mins_formulas = []
            df3 = pd.DataFrame(extrinsic_formation_energies)
            for i, c in enumerate(cpd):
                name = f"mu_{self.extrinsic_species}_{i}"
                df3[name] = df3["formation_energy"]
                for k, v in c.items():
                    df3[name] -= df3[k] * v
                df3[name] /= df3[self.extrinsic_species]
                # find min at that chempot
                mins.append(df3[name].min())
                mins_formulas.append(df3.iloc[df3[name].idxmin()]["formula"])

            df[self.extrinsic_species] = mins
            df[f"{self.extrinsic_species}_limiting_phase"] = mins_formulas

            # 1. work out the formation energies of all dopant competing
            #    phases using the elemental energies
            # 2. for each of the chempots already calculated work out what
            #    the chemical potential of the dopant would be from
            #       mu_dopant = Hf(dopant competing phase) - sum(mu_elements)
            # 3. find the most negative mu_dopant which then becomes the new
            #    canonical chemical potential for that dopant species and the
            #    competing phase is the 'limiting phase' right?
            # 4. update the chemical potential limits table to reflect this?

        else:
            # reassign the attributes correctly i guess so we can find them
            self.pd_entries = pd_entries_intrinsic
            self.phase_diagram = PhaseDiagram(
                self.pd_entries, map(Element, self.elemental)
            )

        # save and print
        df.to_csv(csv_fname, index=False)
        print("Calculated chemical potential limits: \n")
        print(df)

    def cplap_input(self, dependent_variable=None):
        """For completeness' sake, automatically saves to input.dat
        Args:
            dependent variable (str) gotta pick one of the variables as dependent, the first element is chosen from the composition if this isn't set
        """
        with open("input.dat", "w") as f:
            with contextlib.redirect_stdout(f):
                for i in self.data:
                    comp = Composition(i["formula"]).as_dict()
                    if self.bulk_composition.as_dict() == comp:
                        print(len(comp))
                        for k, v in comp.items():
                            print(int(v), k, end=" ")
                        print(i["formation_energy"])
                if dependent_variable is not None:
                    print(dependent_variable)
                else:
                    print(self.elemental[0])
                print(len(self.data) - 1 - len(self.elemental))
                for i in self.data:
                    comp = Composition(i["formula"]).as_dict()
                    if self.bulk_composition.as_dict() != comp and len(comp) != 1:
                        print(len(comp))
                        for k, v in comp.items():
                            print(int(v), k, end=" ")
                        print(i["formation_energy"])


def make_molecule_in_a_box(element):
    # (but do try to fix it so that the nupdown is the same as magnetisation
    # so that it makes that assignment easier later on when making files)
    # the bond distances are taken from various sources and *not* thoroughly vetted
    lattice = [[30, 0, 0], [0, 30, 0], [0, 0, 30]]
    all_structures = {
        "O2": {
            "structure": Structure(
                lattice=lattice,
                species=["O", "O"],
                coords=[[15, 15, 15], [15, 15, 16.22]],
                coords_are_cartesian=True,
            ),
            "formula": "O2",
            "magnetisation": 2,
        },
        "N2": {
            "structure": Structure(
                lattice=lattice,
                species=["N", "N"],
                coords=[[15, 15, 15], [15, 15, 16.09]],
                coords_are_cartesian=True,
            ),
            "formula": "N2",
            "magnetisation": 0,
        },
        "H2": {
            "structure": Structure(
                lattice=lattice,
                species=["H", "H"],
                coords=[[15, 15, 15], [15, 15, 15.74]],
                coords_are_cartesian=True,
            ),
            "formula": "H2",
            "magnetisation": 0,
        },
        "F2": {
            "structure": Structure(
                lattice=lattice,
                species=["F", "F"],
                coords=[[15, 15, 15], [15, 15, 16.44]],
                coords_are_cartesian=True,
            ),
            "formula": "F2",
            "magnetisation": 0,
        },
        "Cl2": {
            "structure": Structure(
                lattice=lattice,
                species=["Cl", "Cl"],
                coords=[[15, 15, 15], [15, 15, 16.99]],
                coords_are_cartesian=True,
            ),
            "formula": "Cl2",
            "magnetisation": 0,
        },
    }

    if element in all_structures.keys():
        structure = all_structures[element]["structure"]
        formula = all_structures[element]["formula"]
        magnetisation = all_structures[element]["magnetisation"]

    return structure, formula, magnetisation


def _calculate_formation_energies(data, elemental):
    df = pd.DataFrame(data)
    for d in data:
        for e in elemental.keys():
            d[e] = Composition(d["formula"]).as_dict()[e]

    df2 = pd.DataFrame(data)
    df2["formation_energy"] = df2["energy_per_fu"]
    for k, v in elemental.items():
        df2["formation_energy"] -= df2[k] * v

    df["formation_energy"] = df2["formation_energy"]
    return df
