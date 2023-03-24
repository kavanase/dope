import contextlib
import copy
from pathlib import Path, PurePath
import warnings
import json
import pandas as pd

from pymatgen.ext.matproj import MPRester
from pymatgen.entries.computed_entries import ComputedStructureEntry
from pymatgen.analysis.phase_diagram import PhaseDiagram, PDEntry
from pymatgen.io.vasp.sets import DictSet, BadInputSetWarning
from pymatgen.io.vasp.inputs import Kpoints, UnknownPotcarWarning
from pymatgen.io.vasp.outputs import Vasprun
from pymatgen.core import Structure, Composition, Element
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from doped.pycdt.utils.parse_calculations import get_vasprun
from doped.pycdt.utils.vasp import _import_psp


# globally ignore:
warnings.filterwarnings("ignore", category=BadInputSetWarning)
warnings.filterwarnings("ignore", category=UnknownPotcarWarning)
warnings.filterwarnings("ignore", message="No POTCAR file with matching TITEL fields")
warnings.filterwarnings(
    "ignore", message="You are using the legacy MPRester"
)  # currently rely on this so shouldn't show warning

# TODO: Confirm `README` potcar instructions do indeed work as expected!
# TODO: Currently the format for user defined `incar` and `potcar` settings is somewhat
#  inconsistent between `competing_phases` and `vasp_input`, and `pymatgen`. Ideally should all
#  correspond to `pymatgen`'s `DictSet` format.
# TODO: Add warning for when input `potcar_settings` don't match the expected format (i.e. if one
#  of the dict entries is not an element symbol)
# TODO: Should refactor all this to "Chemical Potentials" etc rather than 'competing phases' as
#  this is a more common term in the field so likely more familiar to users. Will do when all
#  chempot functionality has been transferred over and old `pycdt` chempot code deleted
# TODO: Check default error when user attempts `CompetingPhases()` with no API key setup; if not
#  sufficiently informative, add try except catch to give more informative error message for this.
# TODO: Make sure elements in output `chempot_limits` are in the form of string objects so are
#  JSONable


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


def _make_molecular_entry(computed_entry):
    """
    Generate a new ComputedStructureEntry for a molecule in a box, for the input elemental
    ComputedEntry
    """
    assert len(computed_entry.composition.elements) == 1  # Elemental!
    struc, formula, magnetisation = make_molecule_in_a_box(
        computed_entry.data["pretty_formula"]
    )
    molecular_entry = ComputedStructureEntry(
        structure=struc,
        energy=computed_entry.energy_per_atom * 2,  # set entry energy to be hull energy
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
    molecular_entry.data["energy_per_atom"] = computed_entry.data["energy_per_atom"]
    molecular_entry.data["energy"] = computed_entry.data["energy_per_atom"] * 2
    molecular_entry.data["total_magnetization"] = magnetisation
    molecular_entry.data["nelements"] = 1
    molecular_entry.data["elements"] = [formula]
    molecular_entry.data["molecule"] = True

    return molecular_entry


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

    df["num_atoms_in_fu"] = df["energy_per_fu"] / df["energy_per_atom"]
    df["num_species"] = df["formula"].apply(lambda x: len(Composition(x).as_dict()))

    # sort by num_species, then alphabetically, then by num_atoms_in_fu, then by formation_energy
    df.sort_values(
        by=["num_species", "formula", "num_atoms_in_fu", "formation_energy"],
        inplace=True,
    )
    # drop num_atoms_in_fu and num_species
    df.drop(columns=["num_atoms_in_fu", "num_species"], inplace=True)

    return df


def _renormalise_entry(entry, renormalisation_energy_per_atom):
    """
    Regenerate the input entry with an energy per atom decreased by renormalisation_energy_per_atom
    """
    renormalised_entry_dict = entry.as_dict().copy()
    renormalised_entry_dict[
        "energy"
    ] = entry.energy - renormalisation_energy_per_atom * sum(
        entry.composition.values()
    )  # entry.energy includes MP corrections as desired
    renormalised_entry = PDEntry.from_dict(renormalised_entry_dict)
    return renormalised_entry


def get_chempots_from_pd(bulk_ce, pd):
    """
    Get the chemical potential limits for the bulk computed entry in the supplied phase diagram.

    Args:
        bulk_ce: Pymatgen ComputedStructureEntry object for bulk entry / supercell
        pd: Pymatgen PhaseDiagram object for the system of interest
    """
    bulk_composition = bulk_ce.composition
    redcomp = bulk_composition.reduced_composition
    # append bulk_ce to phase diagram, if not present
    entries = pd.all_entries
    if not any(
        [
            (ent.composition == bulk_ce.composition and ent.energy == bulk_ce.energy)
            for ent in entries
        ]
    ):
        entries.append(
            PDEntry(
                bulk_ce.composition,
                bulk_ce.energy,
                attribute="Bulk Material",
            )
        )
        pd = PhaseDiagram(entries)

    chem_lims = pd.get_all_chempots(redcomp)

    return chem_lims


class CompetingPhases:
    """
    Class to generate the input files for competing phases on the phase diagram for the host
    material (determining the chemical potential limits). Materials Project (MP) data is used,
    along with an uncertainty range specified by `e_above_hull`, to determine the relevant
    competing phases. Diatomic gaseous molecules are generated as molecules-in-a-box as appropriate.

    TODO: Add full_phase_diagram option.
    TODO: Need to add functionality to deal with cases where the bulk composition is not listed
    on the MP – warn user (i.e. check your shit) and generate the competing phases according to
    composition position within phase diagram.
    E.g. from pycdt chemical_potentials:
    #                 "However, no stable entry with this composition exists "
    #                 "in the MP database!\nPlease consider submitting the "
    #                 "POSCAR to the MP xtaltoolkit, so future users will "
    #                 "know about this structure:"
    #                 " https://materialsproject.org/#apps/xtaltoolkit\n" – see
    analyze_GGA_chempots code for example.
    TODO: Add note to notebook that if your bulk phase is lower energy than its version on the MP
    (e.g. distorted perovskite), then you should use this for your bulk competing phase calculation.
    """

    def __init__(self, composition, e_above_hull=0.1, api_key=None):
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
            api_key (str): Materials Project (MP) API key, needed to access the MP database for
                competing phase generation. If not supplied, will attempt to read from
                environment variable `PMG_MAPI_KEY` (in `~/.pmgrc.yaml`) – see the `doped`
                homepage (https://github.com/SMTG-UCL/doped) for instructions on setting this up.
                This should correspond to the legacy MP API; from
                https://legacy.materialsproject.org/open.
        """
        self.api_key = api_key

        # create list of entries
        self._molecules_in_a_box = ["H2", "O2", "N2", "F2", "Cl2"]

        # TODO: Should hard code S (solid + S8), P and Se in here too. Common anions with a lot of
        #  unnecessary polymorphs on MP
        # P_EaH=0 is red phosphorus (HSE06 groundstate), P_EaH=0.037 is black phosphorus (thermo
        # stable at RT), so only need to generate these

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

        # first if just for the tests
        if api_key is not None:
            if len(api_key) == 32:
                raise ValueError(
                    "You are trying to use the new Materials Project (MP) API which is not "
                    "supported by doped. Please use the legacy MP API ("
                    "https://legacy.materialsproject.org/open)."
                )
            elif 15 <= len(api_key) <= 20:
                self.eah = "e_above_hull"
            else:
                raise ValueError(
                    f"API key {api_key} is not a valid legacy Materials Project API "
                    f"key. These are available at "
                    f"https://legacy.materialsproject.org/open"
                )

        with MPRester(api_key=self.api_key) as mpr:
            self.MP_full_pd_entries = mpr.get_entries_in_chemsys(
                list(self.bulk_comp.as_dict().keys()),
                inc_structure="initial",
                property_data=self.data,
            )
        self.MP_full_pd_entries = [
            e for e in self.MP_full_pd_entries if e.data["e_above_hull"] <= e_above_hull
        ]
        self.MP_full_pd_entries.sort(
            key=lambda x: x.data["e_above_hull"]
        )  # sort by e_above_hull

        # the has attr is there just for testing, so we don't have to keep querying MP
        if not hasattr(self, "entries"):
            self.entries = m.get_entries_in_chemsys(
                self.system, inc_structure=stype, property_data=self.data
            )

        pd_entries = []
        # check that none of the elemental ones are molecules in a box
        for entry in self.MP_full_pd_entries.copy():
            if (
                entry.data["pretty_formula"] in self._molecules_in_a_box
                and entry.data["e_above_hull"] == 0
            ):
                # only first matching molecular entry
                molecular_entry = _make_molecular_entry(entry)
                if not any(
                    [
                        ent.data["molecule"]
                        and ent.data["pretty_formula"]
                        == molecular_entry.data["pretty_formula"]
                        for ent in pd_entries
                    ]
                ):  # first entry only
                    pd_entries.append(molecular_entry)
                    self.MP_full_pd_entries.append(molecular_entry)

            elif entry.data["pretty_formula"] not in self._molecules_in_a_box:
                entry.data["molecule"] = False
                pd_entries.append(entry)

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
        self.MP_bulk_ce = bulk_ce

        MP_gga_chempots = get_chempots_from_pd(bulk_ce, pd)

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
                renormalised_entry = _renormalise_entry(entry, e_above_hull)
                new_pd = PhaseDiagram(pd.entries + [renormalised_entry])
                new_MP_gga_chempots = get_chempots_from_pd(bulk_ce, new_pd)

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
        **kwargs,
    ):
        """
        Sets up input files for kpoints convergence testing
        Args:
            kpoints_metals (tuple): Kpoint density per inverse volume (Å^-3) to be tested in
                (min, max, step) format for metals
            kpoints_nonmetals (tuple): Kpoint density per inverse volume (Å^-3) to be tested in
                (min, max, step) format for nonmetals
            potcar_functional (str): POTCAR functional to use (e.g. PBE_54)
            user_potcar_settings (dict): Override the default POTCARs e.g. {"Li": "Li_sv"}
            user_incar_settings (dict): Override the default INCAR settings
                e.g. {"EDIFF": 1e-5, "LDAU": False}
        Returns:
            Writes VASP input files
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
                dis.write_input(fname, **kwargs)

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
                dis.write_input(fname, **kwargs)

    def vasp_std_setup(
        self,
        kpoints_metals=95,
        kpoints_nonmetals=45,
        potcar_functional="PBE_54",
        user_potcar_settings=None,
        user_incar_settings=None,
        **kwargs,
    ):
        """
        Sets up input files for vasp_std relaxations
        Args:
            kpoints_metals (int): Kpoint density per inverse volume (Å^-3) for metals
            kpoints_nonmetals (int): Kpoint density per inverse volume (Å^-3) for nonmetals
            potcar_functional (str): POTCAR to use (e.g. LDA, PBE, PBE_52, PBE_54)
            user_potcar_settings (dict): Override the default POTCARs e.g. {"Li": "Li_sv"}
            user_incar_settings (dict): Override the default INCAR settings
                e.g. {"EDIFF": 1e-5, "LDAU": False}
        Returns:
            Writes VASP input files
        """
        # TODO: Update this to use:
        #  sym = SpacegroupAnalyzer(e.structure)
        #  struc = sym.get_primitive_standard_structure() -> output this structure
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
            dis.write_input(fname, **kwargs)

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
            dis.write_input(fname, **kwargs)

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
            dis.write_input(fname, **kwargs)


# TODO: Add full_sub_approach option
# TODO: Add warnings for full_sub_approach=True, especially if done with multiple
#  extrinsic species.
class ExtrinsicCompetingPhases(CompetingPhases):
    """
    This class generates the competing phases that need to be calculated to obtain the chemical
    potential limits when doping with extrinsic species / impurities. Ensures that only the
    necessary additional competing phases are generated.
    """

    def __init__(
        self,
        composition,
        extrinsic_species,
        e_above_hull=0.1,
        full_sub_approach=False,
        codoping=False,
        api_key=None,
    ):
        """
        Args:
            composition (str, Composition): Composition of host material
                (e.g. 'LiFePO4', or Composition('LiFePO4'), or Composition({"Li":1, "Fe":1,
                "P":1, "O":4}))
            extrinsic_species (str, list): Extrinsic dopant/impurity species
                (e.g. "Mg" or ["Mg", "Na"])
            e_above_hull (float): Maximum energy-above-hull of Materials Project entries to be
                considered as competing phases. This is an uncertainty range for the
                MP-calculated formation energies, which may not be accurate due to functional
                choice (GGA vs hybrid DFT / GGA+U / RPA etc.), lack of vdW corrections etc.
                Any phases that would border the host material on the phase diagram, if their
                relative energy was downshifted by `e_above_hull`, are included.
                Default is 0.1 eV/atom.
            full_sub_approach (bool): Generate competing phases by considering the full phase
                diagram, including chemical potential limits with multiple extrinsic phases.
                Only recommended when looking at high (non-dilute) doping concentrations.
                Default = False. Described in further detail below.
            codoping (bool): Whether to consider extrinsic competing phases containing multiple
                extrinsic species. Only relevant to high (non-dilute) co-doping concentrations.
                If set to True, then `full_sub_approach` is also set to True.
                Default = False.
            api_key (str): Materials Project (MP) API key, needed to access the MP database for
                competing phase generation. If not supplied, will attempt to read from
                environment variable `PMG_MAPI_KEY` (in `~/.pmgrc.yaml`) – see the `doped`
                homepage (https://github.com/SMTG-UCL/doped) for instructions on setting this up.
                This should correspond to the legacy MP API; from
                https://legacy.materialsproject.org/open.

        This code uses the Materials Project (MP) phase diagram data along with the
        `e_above_hull` error range to generate potential competing phases.

        NOTE on 'full_sub_approach':
            The default approach for substitutional elements (`full_sub_approach = False`) is to
            only consider chemical potential limits with a maximum of 1 extrinsic phase
            (composition with extrinsic species present). This is a valid approximation for the
            case of dilute dopant/impurity concentrations. For high (non-dilute) concentrations
            of extrinsic species, use `full_sub_approach = True`.
        """
        # the competing phases & entries of the OG system
        super().__init__(composition, e_above_hull, api_key)
        self.intrinsic_entries = copy.deepcopy(self.entries)
        self.entries = []
        self.intrinsic_species = [
            s.symbol for s in self.bulk_comp.reduced_composition.elements
        ]
        self.MP_intrinsic_full_pd_entries = (
            self.MP_full_pd_entries
        )  # includes molecules-in-boxes

        if isinstance(extrinsic_species, str):
            extrinsic_species = [
                extrinsic_species,
            ]
        elif not isinstance(extrinsic_species, list):
            raise TypeError(
                f"`extrinsic_species` must be a string (i.e. the extrinsic species "
                f"symbol, e.g. 'Mg') or a list (e.g. ['Mg', 'Na']), got type "
                f"{type(extrinsic_species)} instead!"
            )
        self.extrinsic_species = extrinsic_species

        # if codoping = True, should have multiple extrinsic species
        if codoping:
            if len(extrinsic_species) < 2:
                warnings.warn(
                    "`codoping` is set to True, but `extrinsic_species` only contains 1 "
                    "element, so `codoping` will be set to False."
                )
                codoping = False

            elif not full_sub_approach:
                full_sub_approach = True

        if (
            full_sub_approach
        ):  # can be time-consuming if several extrinsic_species supplied
            if codoping:
                # TODO: When `full_phase_diagram` option added to `CompetingPhases`, can remove
                #  this code block and just use:
                #  super()__init__(composition = (
                #  self.intrinsic_species+self.extrinsic_species).join(""), e_above_hull, api_key)
                #  )
                #  self.intrinsic_entries = [phase for phase in self.entries if
                #  not any([extrinsic in phase for extrinsic in self.extrinsic_species])]
                #  entries = [phase for phase in self.entries if phase not in
                #  self.intrinsic_entries]
                #  self.entries = entries
                #  self.MP_intrinsic_full_pd_entries = [entry for entry in
                #  self.MP_full_pd_entries if not any([extrinsic in entry.composition.reduced_formula
                #  for extrinsic in self.extrinsic_species])]
                #  MP_full_pd_entries = [entry for entry in self.MP_full_pd_entries if entry not in
                #  self.MP_intrinsic_full_pd_entries]
                #  self.MP_full_pd_entries = MP_full_pd_entries  # includes molecules-in-boxes

                with MPRester(api_key=self.api_key) as mpr:
                    self.MP_full_pd_entries = mpr.get_entries_in_chemsys(
                        self.intrinsic_species + self.extrinsic_species,
                        inc_structure="initial",
                        property_data=self.data,
                    )
                self.MP_full_pd_entries = [
                    e
                    for e in self.MP_full_pd_entries
                    if e.data["e_above_hull"] <= e_above_hull
                ]

                # sort by e_above_hull:
                self.MP_full_pd_entries.sort(key=lambda x: x.data["e_above_hull"])

                for entry in self.MP_full_pd_entries.copy():
                    if any(
                        [
                            sub_elt in entry.composition
                            for sub_elt in self.extrinsic_species
                        ]
                    ):
                        if (
                            entry.data["pretty_formula"] in self._molecules_in_a_box
                            and entry.data["e_above_hull"] == 0
                        ):  # only first matching entry
                            molecular_entry = _make_molecular_entry(entry)
                            if not any(
                                [
                                    entry.data["molecule"]
                                    and entry.data["pretty_formula"]
                                    == molecular_entry.data["pretty_formula"]
                                    for entry in self.entries
                                ]
                            ):  # first entry only
                                self.MP_full_pd_entries.append(molecular_entry)
                                self.entries.append(molecular_entry)
                        elif (
                            entry.data["pretty_formula"] not in self._molecules_in_a_box
                        ):
                            entry.data["molecule"] = False
                            self.entries.append(entry)

            else:  # full_sub_approach but not co-doping
                self.MP_full_pd_entries = []
                for sub_elt in self.extrinsic_species:
                    with MPRester(api_key=self.api_key) as mpr:
                        MP_full_pd_entries = mpr.get_entries_in_chemsys(
                            self.intrinsic_species
                            + [
                                sub_elt,
                            ],
                            inc_structure="initial",
                            property_data=self.data,
                        )
                    MP_full_pd_entries = [
                        e
                        for e in MP_full_pd_entries
                        if e.data["e_above_hull"] <= e_above_hull
                    ]
                    # sort by e_above_hull:
                    MP_full_pd_entries.sort(key=lambda x: x.data["e_above_hull"])

                    for entry in MP_full_pd_entries.copy():
                        if entry not in self.MP_full_pd_entries:
                            self.MP_full_pd_entries.append(entry)
                        if sub_elt in entry.composition:
                            if (
                                entry.data["pretty_formula"] in self._molecules_in_a_box
                                and entry.data["e_above_hull"] == 0
                            ):
                                # only first matching entry
                                molecular_entry = _make_molecular_entry(entry)
                                if not any(
                                    [
                                        entry.data["molecule"]
                                        and entry.data["pretty_formula"]
                                        == molecular_entry.data["pretty_formula"]
                                        for entry in self.entries
                                    ]
                                ):  # first entry only
                                    self.MP_full_pd_entries.append(molecular_entry)
                                    self.entries.append(molecular_entry)
                            elif (
                                entry.data["pretty_formula"]
                                not in self._molecules_in_a_box
                            ):
                                entry.data["molecule"] = False
                                self.entries.append(entry)

        else:  # full_sub_approach = False; recommended approach for extrinsic species (assumes
            # dilute concentrations)

            # now compile substitution entries:
            self.MP_full_pd_entries = []
            extrinsic_pd_entries = []

            for sub_elt in self.extrinsic_species:
                with MPRester(api_key=self.api_key) as mpr:
                    MP_full_pd_entries = mpr.get_entries_in_chemsys(
                        self.intrinsic_species
                        + [
                            sub_elt,
                        ],
                        inc_structure="initial",
                        property_data=self.data,
                    )
                self.MP_full_pd_entries = [
                    e
                    for e in MP_full_pd_entries
                    if e.data["e_above_hull"] <= e_above_hull
                ]
                # sort by e_above_hull:
                self.MP_full_pd_entries.sort(key=lambda x: x.data["e_above_hull"])

                for entry in self.MP_full_pd_entries.copy():
                    if (
                        entry.data["pretty_formula"] in self._molecules_in_a_box
                        and entry.data["e_above_hull"] == 0
                    ):
                        # only first matching entry
                        molecular_entry = _make_molecular_entry(entry)
                        if not any(
                            [
                                entry.data["molecule"]
                                and entry.data["pretty_formula"]
                                == molecular_entry.data["pretty_formula"]
                                for entry in extrinsic_pd_entries
                            ]
                        ):  # first entry only
                            self.MP_full_pd_entries.append(molecular_entry)
                            extrinsic_pd_entries.append(molecular_entry)
                    elif entry.data["pretty_formula"] not in self._molecules_in_a_box:
                        entry.data["molecule"] = False
                        extrinsic_pd_entries.append(entry)

                # Adding substitutional phases to extrinsic competing phases list only when the
                # phases in equilibria are those from the bulk phase diagram. This is essentially
                # the assumption that the majority of elements in the total composition will be
                # from the host composition rather than the extrinsic species (a good
                # approximation for dilute concentrations)

                if not extrinsic_pd_entries:
                    raise ValueError(
                        f"No Materials Project entries found for the given chemical "
                        f"system: {self.intrinsic_species + [sub_elt,]}"
                    )

                extrinsic_pd = PhaseDiagram(extrinsic_pd_entries)
                MP_extrinsic_gga_chempots = get_chempots_from_pd(
                    self.MP_bulk_ce, extrinsic_pd
                )
                MP_extrinsic_bordering_phases = []

                for facet in MP_extrinsic_gga_chempots.keys():
                    # if the number of intrinsic competing phases for this facet is equal to the
                    # number of species in the bulk composition, then include the extrinsic phase(s)
                    # for this facet (full_sub_approach = False approach)
                    MP_intrinsic_bordering_phases = set(
                        [phase for phase in facet.split("-") if sub_elt not in phase]
                    )
                    if len(MP_intrinsic_bordering_phases) == len(
                        self.intrinsic_species
                    ):
                        # add to list of extrinsic bordering phases, if not already present:
                        MP_extrinsic_bordering_phases.extend(
                            [
                                phase
                                for phase in facet.split("-")
                                if sub_elt in phase
                                and phase not in MP_extrinsic_bordering_phases
                            ]
                        )

                # add any phases that would border the host material on the phase diagram,
                # if their relative energy was downshifted by `e_above_hull`:
                for entry in MP_full_pd_entries:
                    if (
                        entry.name not in MP_extrinsic_bordering_phases
                        and not entry.is_element
                        and sub_elt in entry.composition
                    ):
                        # decrease entry energy per atom by `e_above_hull` eV/atom
                        renormalised_entry = _renormalise_entry(entry, e_above_hull)
                        new_extrinsic_pd = PhaseDiagram(
                            extrinsic_pd.entries + [renormalised_entry]
                        )
                        new_MP_extrinsic_gga_chempots = get_chempots_from_pd(
                            self.MP_bulk_ce, new_extrinsic_pd
                        )

                        if new_MP_extrinsic_gga_chempots != MP_extrinsic_gga_chempots:
                            # new bordering phase, check if not an over-dependent facet:

                            for facet in new_MP_extrinsic_gga_chempots.keys():
                                if facet not in MP_extrinsic_gga_chempots.keys():
                                    # new facet, check if not an over-dependent facet:
                                    MP_intrinsic_bordering_phases = set(
                                        [
                                            phase
                                            for phase in facet.split("-")
                                            if sub_elt not in phase
                                        ]
                                    )
                                    if len(MP_intrinsic_bordering_phases) == len(
                                        self.intrinsic_species
                                    ):
                                        MP_extrinsic_bordering_phases.extend(
                                            [
                                                phase
                                                for phase in facet.split("-")
                                                if sub_elt in phase
                                                and phase
                                                not in MP_extrinsic_bordering_phases
                                            ]
                                        )

                extrinsic_entries = [
                    entry
                    for entry in extrinsic_pd_entries
                    if entry.name in MP_extrinsic_bordering_phases
                    or (entry.is_element and sub_elt in entry.name)
                ]

                # check that extrinsic competing phases list is not empty (can happen with
                # 'over-dependent' facets); if so then set full_sub_approach = True and re-run
                # the extrinsic phase addition process
                if not extrinsic_entries:
                    warnings.warn(
                        "Determined chemical potentials to be over dependent on an "
                        "extrinsic species. This means we need to revert to "
                        "`full_sub_approach = True` – running now."
                    )
                    full_sub_approach = True
                    extrinsic_entries = [
                        entry
                        for entry in self.MP_full_pd_entries
                        if sub_elt in entry.composition
                    ]

                extrinsic_entries.sort(
                    key=lambda x: x.data["e_above_hull"]
                )  # sort by e_above_hull
                self.entries += extrinsic_entries


class CompetingPhasesAnalyzer:
    """
    Post processing competing phases data to calculate chemical potentials.
    """

    def __init__(self, system, extrinsic_species=None):
        """
        Args:
            system (str): The  'reduced formula' of the bulk composition
            extrinsic_species (str): Dopant species
            system (str): The  'reduced formula' of the bulk composition
            extrinsic_species (str): Dopant species - can only deal with one at a time (see
            notebook in examples folder for more complex cases)
        """

        self.bulk_composition = Composition(system)
        self.elemental = [str(c) for c in self.bulk_composition.elements]
        self.extrinsic_species = extrinsic_species

        if extrinsic_species:
            self.elemental.append(extrinsic_species)

    def from_vaspruns(self, path, folder="vasp_std", csv_fname=None):
        """
        Reads in vaspruns, collates energies to csv.

        Args:
            path (list, str, pathlib Path): Either a list of strings or Paths to vasprun.xml(.gz)
            files, or a path to the base folder in which you have your
            formula_EaH_/vasp_std/vasprun.xml
            folder (str): The folder in which vasprun is, only use if you set base path
            (i.e. change to vasp_ncl or if vaspruns are in the formula_EaH folder).
            csv_fname (str): If set will save to csv with this name
        Returns:
            None, sets self.data and self.elemental_energies
        """
        # TODO: Change this to just recursively search for vaspruns within the specified path
        # TODO: Add check for matching INCAR and POTCARs from these calcs, as we also want with
        #  defect parsing
        self.vasprun_paths = []
        # fetch data
        # if path is just a list of all competing phases
        if isinstance(path, list):
            for p in path:
                if "vasprun.xml" in Path(p).name:
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
                    try:
                        vr, vr_path = get_vasprun(vp)
                        self.vasprun_paths.append(vr_path)

                    except FileNotFoundError:
                        try:
                            vp = p / "vasprun.xml"
                            vr, vr_path = get_vasprun(vp)
                            self.vasprun_paths.append(vr_path)

                        except FileNotFoundError:
                            print(
                                f"Can't find a vasprun.xml(.gz) file in {p} or {p/folder}, "
                                f"proceed with caution"
                            )
                            continue

                else:
                    raise FileNotFoundError(
                        "Folders are not in the correct structure, provide them as a list of "
                        "paths (or strings)"
                    )

        else:
            raise ValueError(
                "Path should either be a list of paths, a string or a pathlib Path object"
            )

        # Ignore POTCAR warnings when loading vasprun.xml
        # pymatgen assumes the default PBE with no way of changing this
        warnings.filterwarnings("ignore", category=UnknownPotcarWarning)
        warnings.filterwarnings(
            "ignore", message="No POTCAR file with matching TITEL fields"
        )

        num = len(self.vasprun_paths)
        print(
            f"Parsing {num} vaspruns and pruning to include only lowest-energy polymorphs..."
        )
        self.vaspruns = [Vasprun(e).as_dict() for e in self.vasprun_paths]
        self.data = []

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
                if elt not in self.elemental_energies:
                    self.elemental_energies[elt] = v["output"]["final_energy_per_atom"]
                    if elt not in self.elemental:  # new (extrinsic) element
                        self.extrinsic_species = elt
                        self.elemental.append(elt)

                elif (
                    v["output"]["final_energy_per_atom"] < self.elemental_energies[elt]
                ):
                    # only include lowest energy elemental polymorph
                    self.elemental_energies[elt] = v["output"]["final_energy_per_atom"]

            d = {
                "formula": v["pretty_formula"],
                "kpoints": kpoints,
                "energy_per_fu": final_energy / formulas_per_unit,
                "energy_per_atom": v["output"]["final_energy_per_atom"],
                "energy": final_energy,
            }
            temp_data.append(d)

        df = _calculate_formation_energies(temp_data, self.elemental_energies)
        if csv_fname is not None:
            df.to_csv(csv_fname, index=False)
            print(f"Competing phase formation energies have been saved to {csv_fname}.")

        self.data = df.to_dict(orient="records")

    def from_csv(self, csv):
        """
        Read in data from csv.
        Args:
            csv (str): Path to csv file. Must have columns 'formula',
                'energy_per_fu', 'energy' and 'formation_energy'
        Returns:
            None, sets self.data and self.elemental_energies
        """
        df = pd.read_csv(csv)
        columns = [
            "formula",
            "energy_per_fu",
            "energy_per_atom",
            "energy",
            "formation_energy",
        ]
        if all(x in list(df.columns) for x in columns):
            droplist = [i for i in df.columns if i not in columns]
            df.drop(droplist, axis=1, inplace=True)
            d = df.to_dict(orient="records")
            self.data = d

            self.elemental_energies = {}
            for i in d:
                c = Composition(i["formula"])
                if len(c.elements) == 1:
                    elt = c.chemical_system
                    self.elemental_energies[elt] = i["energy_per_atom"]

        else:
            raise ValueError(
                "supplied csv does not contain the correct headers, cannot read in the data"
            )

    def calculate_chempots(self, csv_fname=None):
        """
        Calculates chemcial potential limits. For dopant species, it calculates the limiting
        potential based on the intrinsic chemical potentials (i.e. same as
        `full_sub_approach=False` in pycdt)
        Args:
            csv_fname (str): If set, will save chemical potential limits to csv
        Retruns:
            Pandas DataFrame, optionally saved to csv
        """

        pd_entries_intrinsic = []
        extrinsic_formation_energies = []
        bulk_pde_list = []
        for d in self.data:
            e = PDEntry(d["formula"], d["energy_per_fu"])
            # presumably checks if the phase is intrinsic
            if set(Composition(d["formula"]).elements).issubset(
                self.bulk_composition.elements
            ):
                pd_entries_intrinsic.append(e)
                if e.composition == self.bulk_composition:  # bulk phase
                    bulk_pde_list.append(e)
            else:
                extrinsic_formation_energies.append(
                    {"formula": d["formula"], "formation_energy": d["formation_energy"]}
                )

        if len(bulk_pde_list) == 0:
            raise ValueError(
                f"Could not find bulk phase for "
                f"{self.bulk_composition.reduced_formula} in the supplied data. "
                f"Found phases: "
                f"{set([e.composition.reduced_formula for e in pd_entries_intrinsic])}"
            )
        elif len(bulk_pde_list) > 0:
            # lowest energy bulk phase
            self.bulk_pde = sorted(bulk_pde_list, key=lambda x: x.energy_per_atom)[0]

        self.intrinsic_phase_diagram = PhaseDiagram(
            pd_entries_intrinsic, map(Element, self.bulk_composition.elements)
        )

        # check if it's stable and if not error out
        if self.bulk_pde not in self.intrinsic_phase_diagram.stable_entries:
            eah = self.intrinsic_phase_diagram.get_e_above_hull(self.bulk_pde)
            raise ValueError(
                f"{self.bulk_composition.reduced_formula} is not stable with respect to competing "
                f"phases, EaH={eah:.4f} eV/atom"
            )

        chem_lims = self.intrinsic_phase_diagram.get_all_chempots(self.bulk_composition)
        # remove Element to make it jsonable
        no_element_chem_lims = {}
        for k, v in chem_lims.items():
            temp_dict = {}
            for kk, vv in v.items():
                temp_dict[str(kk)] = vv
            no_element_chem_lims[k] = temp_dict

        self.intrinsic_chem_limits = {
            "facets": no_element_chem_lims,
            "elemental_refs": {
                str(elt): ent.energy_per_atom
                for elt, ent in self.intrinsic_phase_diagram.el_refs.items()
            },
            "facets_wrt_el_refs": {},
        }

        # relate the facets to the elemental energies
        for facet, chempot_dict in self.intrinsic_chem_limits["facets"].items():
            relative_chempot_dict = copy.deepcopy(chempot_dict)
            for e in relative_chempot_dict.keys():
                relative_chempot_dict[e] -= self.intrinsic_chem_limits[
                    "elemental_refs"
                ][e]
            self.intrinsic_chem_limits["facets_wrt_el_refs"].update(
                {facet: relative_chempot_dict}
            )

        # get chemical potentials as pandas dataframe
        chemical_potentials = []
        for k, v in self.intrinsic_chem_limits["facets_wrt_el_refs"].items():
            lst = []
            columns = []
            for k, v in v.items():
                lst.append(v)
                columns.append(str(k))
            chemical_potentials.append(lst)

        # make df, will need it in next step
        df = pd.DataFrame(chemical_potentials, columns=columns)

        if self.extrinsic_species is not None:
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
            col_name = f"{self.extrinsic_species}_limiting_phase"
            df[col_name] = mins_formulas

            # 1. work out the formation energies of all dopant competing
            #    phases using the elemental energies
            # 2. for each of the chempots already calculated work out what
            #    the chemical potential of the dopant would be from
            #       mu_dopant = Hf(dopant competing phase) - sum(mu_elements)
            # 3. find the most negative mu_dopant which then becomes the new
            #    canonical chemical potential for that dopant species and the
            #    competing phase is the 'limiting phase' right?
            # 4. update the chemical potential limits table to reflect this?

            # reverse engineer chem lims for extrinsic
            df4 = df.copy().to_dict(orient="records")
            cl2 = {
                "elemental_refs": {
                    elt: ene for elt, ene in self.elemental_energies.items()
                },
                "facets_wrt_el_refs": {},
                "facets": {},
            }

            for i, d in enumerate(df4):
                key = (
                    list(self.intrinsic_chem_limits["facets_wrt_el_refs"].keys())[i]
                    + "-"
                    + d[col_name]
                )
                new_vals = list(
                    self.intrinsic_chem_limits["facets_wrt_el_refs"].values()
                )[i]
                new_vals[f"{self.extrinsic_species}"] = d[f"{self.extrinsic_species}"]
                cl2["facets_wrt_el_refs"][key] = new_vals

            # relate the facets to the elemental
            # energies but in reverse this time
            for facet, chempot_dict in cl2["facets_wrt_el_refs"].items():
                relative_chempot_dict = copy.deepcopy(chempot_dict)
                for e in relative_chempot_dict.keys():
                    relative_chempot_dict[e] += cl2["elemental_refs"][e]
                cl2["facets"].update({facet: relative_chempot_dict})

            self.chem_limits = cl2

        # save and print
        if csv_fname is not None:
            df.to_csv(csv_fname, index=False)
            print("Saved chemical potential limits to csv file: ", csv_fname)

        print("Calculated chemical potential limits: \n")
        print(df)

        return df

    def cplap_input(self, dependent_variable=None, filename="input.dat"):
        """For completeness' sake, automatically saves to input.dat for cplap
        Args:
            dependent_variable (str) Pick one of the variables as dependent, the first element is
                chosen from the composition if this isn't set
            filename (str): filename, should end in .dat
        Returns
            None, writes input.dat file
        """
        with open(filename, "w") as f:
            with contextlib.redirect_stdout(f):
                # get lowest energy bulk phase
                bulk_entries = [sub_dict for sub_dict in self.data if
                              self.bulk_composition.as_dict() == Composition(sub_dict[
                                                                                 "formula"]).as_dict()]
                bulk_entry = min(bulk_entries, key=lambda x: x["formation_energy"])
                print(len(self.bulk_composition.as_dict()))
                for k, v in self.bulk_composition.as_dict().items():
                    print(int(v), k, end=" ")
                print(bulk_entry["formation_energy"])

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
