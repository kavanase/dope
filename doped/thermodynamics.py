"""
Code for analysing the thermodynamics of defect formation in solids, including
calculation of formation energies as functions of Fermi level and chemical
potentials, charge transition levels, defect/carrier concentrations etc.
"""

import contextlib
import importlib.util
import os
import warnings
from copy import deepcopy
from functools import reduce
from itertools import chain, product
from typing import TYPE_CHECKING, Any, Optional, Union

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import colors
from matplotlib.figure import Figure
from monty.json import MSONable
from monty.serialization import dumpfn, loadfn
from pymatgen.core.composition import Composition
from pymatgen.electronic_structure.dos import Dos, FermiDos, Spin, f0
from pymatgen.io.vasp import Vasprun
from pymatgen.util.typing import PathLike
from scipy.optimize import brentq
from scipy.spatial import HalfspaceIntersection
from tqdm import tqdm

from doped import _doped_obj_properties_methods
from doped.chemical_potentials import ChemicalPotentialGrid, get_X_poor_limit, get_X_rich_limit
from doped.core import (
    DefectEntry,
    _get_dft_chempots,
    _no_chempots_warning,
    _orientational_degeneracy_warning,
)
from doped.generation import _sort_defect_entries
from doped.utils.parsing import (
    _compare_incar_tags,
    _compare_kpoints,
    _compare_potcar_symbols,
    _get_bulk_supercell,
    _get_defect_supercell_site,
    get_nelect_from_vasprun,
    get_vasprun,
)
from doped.utils.plotting import _rename_key_and_dicts, _TLD_plot
from doped.utils.symmetry import _get_all_equiv_sites, get_sga

if TYPE_CHECKING:
    # from pymatgen.util.typing import PathLike

    with contextlib.suppress(ImportError):
        from py_sc_fermi.defect_species import DefectSpecies
        from py_sc_fermi.defect_system import DefectSystem
        from py_sc_fermi.dos import DOS


def bold_print(string: str) -> None:
    """
    Does what it says on the tin.

    Prints the input string in bold.
    """
    print("\033[1m" + string + "\033[0m")


def _raise_limit_with_user_chempots_error(no_chempots=True):
    problem = (
        (
            "the supplied chempots are not in the doped format (i.e. with `limits` in the chempots dict), "
            "and instead correspond to just a single chemical potential limit"
        )
        if no_chempots
        else "no `chempots` have been supplied"
    )
    raise ValueError(
        f"You have specified a chemical potential limit, but {problem}, so `limit` cannot be used here!"
    )


def _parse_limit(chempots: dict, limit: Optional[str] = None):
    if limit is not None:
        if limit in chempots["limits"]:
            return limit  # direct match, just return limit name
        if "limits" not in chempots or "User Chemical Potentials" in chempots["limits"]:
            # user specified chempots
            _raise_limit_with_user_chempots_error(no_chempots=True)
        if "No User Chemical Potentials" in chempots["limits"]:
            _raise_limit_with_user_chempots_error(no_chempots=False)
        if "rich" in limit:
            limit = get_X_rich_limit(limit.split("-")[0], chempots)
        elif "poor" in limit:
            limit = get_X_poor_limit(limit.split("-")[0], chempots)

    return limit


def get_rich_poor_limit_dict(chempots: dict) -> dict:
    """
    Get a dictionary of {"X-rich": limit, "X-poor": limit...} for each element
    X in the chempots phase diagram.

    Args:
        chempots (dict): The chempots dict, in the doped format.
    """
    if (
        "limits" not in chempots
        or "User Chemical Potentials" in chempots["limits"]
        or "No User Chemical Potentials" in chempots["limits"]
    ):
        raise ValueError(
            "The supplied chempots are not in the doped format (i.e. with `limits` in the "
            "chempots dict), and so the X-rich/poor limits cannot be determined!"
        )

    comps = {comp for key in chempots["limits"] for comp in key.split("-")}
    elts = {element.symbol for comp in comps for element in Composition(comp).elements}
    limit_dict = {f"{elt}-rich": get_X_rich_limit(elt, chempots) for elt in elts}
    limit_dict.update({f"{elt}-poor": get_X_poor_limit(elt, chempots) for elt in elts})
    return limit_dict


def _get_limit_name_from_dict(limit, limit_rich_poor_dict, bracket=False):
    if limit_rich_poor_dict and limit in limit_rich_poor_dict.values():
        # get first key with matching value:
        x_rich_poor = list(limit_rich_poor_dict.keys())[list(limit_rich_poor_dict.values()).index(limit)]
        return f"{x_rich_poor} ({limit})" if bracket else x_rich_poor
    return limit


def _update_old_chempots_dict(chempots: Optional[dict] = None) -> Optional[dict]:
    """
    Update a chempots dict in the old ``doped`` format (i.e. with ``facets``
    rather than ``limits``) to that of the new format.

    Also replaces any usages of ``"elt_refs"`` with ``"el_refs"``.
    """
    if chempots is not None:
        chempots = deepcopy(chempots)  # don't modify original dict
        for key, subdict in list(chempots.items()):
            chempots[key.replace("elt_refs", "el_refs").replace("facets", "limits")] = subdict

    return chempots


def _parse_chempots(
    chempots: Optional[dict] = None, el_refs: Optional[dict] = None, update_el_refs: bool = False
) -> tuple[Optional[dict], Optional[dict]]:
    """
    Parse the chemical potentials input, formatting them in the ``doped``
    format for use in analysis functions.

    Can be either ``doped`` format or user-specified format.

    Returns parsed ``chempots`` and ``el_refs``
    """
    for i in [chempots, el_refs]:
        if i is not None and not isinstance(i, dict):
            raise ValueError(
                f"Invalid chempots/el_refs format:\nchempots: {type(chempots)}\nel_refs: {type(el_refs)}\n"
                "Must be a dict (e.g. from `CompetingPhasesAnalyzer.chempots`) or `None`!"
            )

    chempots = _update_old_chempots_dict(chempots)  # this deepcopies, making sure we don't overwrite

    if chempots is None:
        if el_refs is not None:
            chempots = {
                "limits": {"User Chemical Potentials": el_refs},
                "elemental_refs": el_refs,
                "limits_wrt_el_refs": {"User Chemical Potentials": {el: 0 for el in el_refs}},
            }

        return chempots, el_refs

    if "limits_wrt_el_refs" in chempots:  # doped format
        if not update_el_refs:
            el_refs = chempots.get("elemental_refs", el_refs)

        if el_refs is not None:  # update el_refs in chempots dict
            chempots["elemental_refs"] = el_refs
            chempots["limits"] = {
                limit: {
                    el: relative_chempot + el_refs[el]
                    for el, relative_chempot in chempots["limits_wrt_el_refs"][limit].items()
                }
                for limit in chempots["limits_wrt_el_refs"]
            }

        return chempots, chempots.get("elemental_refs")

    if el_refs is None:
        chempots = {
            "limits": {"User Chemical Potentials": chempots},
            "elemental_refs": {el: 0 for el in chempots},
            "limits_wrt_el_refs": {"User Chemical Potentials": chempots},
        }

    else:  # relative chempots and el_refs given
        relative_chempots = chempots
        chempots = {"limits_wrt_el_refs": {"User Chemical Potentials": relative_chempots}}
        chempots["elemental_refs"] = el_refs
        chempots["limits"] = {
            "User Chemical Potentials": {
                el: relative_chempot + el_refs[el] for el, relative_chempot in relative_chempots.items()
            }
        }

    return chempots, chempots.get("elemental_refs")


def raw_energy_from_chempots(composition: Union[str, dict, Composition], chempots: dict) -> float:
    """
    Given an input composition (as a ``str``, ``dict`` or ``pymatgen``
    ``Composition`` object) and chemical potentials dictionary, get the
    corresponding raw energy of the composition (i.e. taking the energies given
    in the ``'limits'`` subdicts of ``chempots``, in the ``doped`` chemical
    potentials dictionary format).

    Args:
        composition (Union[str, dict, Composition]):
            Composition to get the raw energy of.
        chempots (dict):
            Chemical potentials dictionary.

    Returns:
        Raw energy of the composition.
    """
    if not isinstance(composition, Composition):
        composition = Composition(composition)

    if "limits" not in chempots:
        chempots, _el_refs = _parse_chempots(chempots)  # type: ignore

    raw_energies_dict = dict(next(iter(chempots["limits"].values())))

    if any(el.symbol not in raw_energies_dict for el in composition.elements):
        warnings.warn(
            f"The chemical potentials dictionary (with elements {list(raw_energies_dict.keys())} does not "
            f"contain all the elements in the host composition "
            f"({[el.symbol for el in composition.elements]})!"
        )

    # note this can also be achieved with: (implements exact same code)
    # from pymatgen.core.composition import ChemicalPotential
    # raw_energy = ChemicalPotential(raw_energies_dict).get_energy(composition)

    return sum(raw_energies_dict.get(el.symbol, 0) * stoich for el, stoich in composition.items())


def group_defects_by_distance(
    entry_list: list[DefectEntry], dist_tol: float = 1.5
) -> dict[str, dict[tuple, list[DefectEntry]]]:
    """
    Given an input list of DefectEntry objects, returns a dictionary of {simple
    defect name: {(equivalent defect sites): [DefectEntry]}, where 'simple
    defect name' is the nominal defect type (e.g. ``Te_i`` for
    ``Te_i_Td_Cd2.83_+2``), ``(equivalent defect sites)`` is a tuple of the
    equivalent defect sites (in the bulk supercell), and ``[DefectEntry]`` is a
    list of DefectEntry objects with the same simple defect name and a closest
    distance between symmetry-equivalent sites less than ``dist_tol``
    (1.5 Å by default).

    If a DefectEntry's site has a closest distance less than ``dist_tol`` to
    multiple sets of equivalent sites, then it is matched to the one with
    the lowest minimum distance.

    Args:
        entry_list ([DefectEntry]):
            A list of DefectEntry objects to group together.
        dist_tol (float):
            Threshold for the closest distance (in Å) between equivalent
            defect sites, for different species of the same defect type,
            to be grouped together (for plotting and transition level
            analysis). If the minimum distance between equivalent defect
            sites is less than ``dist_tol``, then they will be grouped
            together, otherwise treated as separate defects.
            (Default: 1.5)

    Returns:
        dict: {simple defect name: {(equivalent defect sites): [DefectEntry]}
    """
    # TODO: This algorithm works well for the vast majority of cases, however it can be sensitive to how
    #  many defects are parsed at once. For instance, in the full parsed CdTe defect dicts in test data,
    #  when parsing with metastable states, all Te_i are combined as each entry is within `dist_tol =
    #  1.5` of another interstitial, but without metastable states (`wout_meta`), Te_i_+2 is excluded (
    #  because it's not within `dist_tol` of any other _stable_ Te_i). Ideally our clustering algorithm
    #  would be independent of this... but challenging to setup without complex clustering approaches (
    #  for now this works very well as is, and this is a rare case and usually not a problem anyway as
    #  dist_tol can just be adjusted as needed)

    # initial group by Defect.name (same nominal defect), then distance to equiv sites
    # first make dictionary of nominal defect name: list of entries with that name
    defect_name_dict = {}
    for entry in entry_list:
        if entry.defect.name not in defect_name_dict:
            defect_name_dict[entry.defect.name] = [entry]
        else:
            defect_name_dict[entry.defect.name].append(entry)

    defect_site_dict: dict[str, dict[tuple, list[DefectEntry]]] = (
        {}
    )  # {defect name: {(equiv defect sites): entry list}}
    bulk_supercell = _get_bulk_supercell(entry_list[0])
    bulk_lattice = bulk_supercell.lattice
    bulk_supercell_sga = get_sga(bulk_supercell)
    symm_bulk_struct = bulk_supercell_sga.get_symmetrized_structure()
    bulk_symm_ops = bulk_supercell_sga.get_symmetry_operations()

    for name, entry_list in defect_name_dict.items():
        defect_site_dict[name] = {}
        sorted_entry_list = sorted(
            entry_list, key=lambda x: abs(x.charge_state)
        )  # sort by charge, starting with closest to zero, for deterministic behaviour
        for entry in sorted_entry_list:
            entry_bulk_supercell = _get_bulk_supercell(entry)
            if entry_bulk_supercell.lattice != bulk_lattice:
                # recalculate bulk_symm_ops if bulk supercell differs
                bulk_supercell_sga = get_sga(entry_bulk_supercell)
                symm_bulk_struct = bulk_supercell_sga.get_symmetrized_structure()
                bulk_symm_ops = bulk_supercell_sga.get_symmetry_operations()

            bulk_site = entry.calculation_metadata.get("bulk_site") or _get_defect_supercell_site(entry)
            # need to use relaxed defect site if bulk_site not in calculation_metadata

            min_dist_list = [
                min(  # get min dist for all equiv site tuples, in case multiple less than dist_tol
                    bulk_site.distance_and_image(site)[0] for site in equiv_site_tuple
                )
                for equiv_site_tuple in defect_site_dict[name]
            ]
            if min_dist_list and min(min_dist_list) < dist_tol:  # less than dist_tol, add to corresponding
                idxmin = np.argmin(min_dist_list)  # entry list
                if min_dist_list[idxmin] > 0.05:  # likely interstitials, need to add equiv sites to tuple
                    # pop old tuple, add new tuple with new equiv sites, and add entry to new tuple
                    orig_tuple = list(defect_site_dict[name].keys())[idxmin]
                    defect_entry_list = defect_site_dict[name].pop(orig_tuple)
                    equiv_site_tuple = (
                        tuple(  # tuple because lists aren't hashable (can't be dict keys)
                            _get_all_equiv_sites(
                                bulk_site.frac_coords,
                                symm_bulk_struct,
                                bulk_symm_ops,
                            )
                        )
                        + orig_tuple
                    )
                    defect_entry_list.extend([entry])
                    defect_site_dict[name][equiv_site_tuple] = defect_entry_list

                else:  # less than dist_tol, add to corresponding entry list
                    defect_site_dict[name][list(defect_site_dict[name].keys())[idxmin]].append(entry)

            else:  # no match found, add new entry
                try:
                    equiv_site_tuple = tuple(  # tuple because lists aren't hashable (can't be dict keys)
                        symm_bulk_struct.find_equivalent_sites(bulk_site)
                    )
                except ValueError:  # likely interstitials, need to add equiv sites to tuple
                    equiv_site_tuple = tuple(  # tuple because lists aren't hashable (can't be dict keys)
                        _get_all_equiv_sites(
                            bulk_site.frac_coords,
                            symm_bulk_struct,
                            bulk_symm_ops,
                        )
                    )

                defect_site_dict[name][equiv_site_tuple] = [entry]

    return defect_site_dict


def group_defects_by_name(entry_list: list[DefectEntry]) -> dict[str, list[DefectEntry]]:
    """
    Given an input list of DefectEntry objects, returns a dictionary of
    ``{defect name without charge: [DefectEntry]}``, where the values are lists
    of DefectEntry objects with the same defect name (excluding charge state).

    The ``DefectEntry.name`` attributes are used to get the defect names.
    These should be in the format:
    "{defect_name}_{optional_site_info}_{charge_state}".
    If the ``DefectEntry.name`` attribute is not defined or does not end with the
    charge state, then the entry will be renamed with the doped default name
    for the `unrelaxed` defect (i.e. using the point symmetry of the defect
    site in the bulk cell).

    For example, ``v_Cd_C3v_+1``, ``v_Cd_Td_+1`` and ``v_Cd_C3v_+2`` will be grouped
    as {``v_Cd_C3v``: [``v_Cd_C3v_+1``, ``v_Cd_C3v_+2``], ``v_Cd_Td``: [``v_Cd_Td_+1``]}.

    Args:
        entry_list ([DefectEntry]):
            A list of DefectEntry objects to group together by defect name
            (without charge).

    Returns:
        dict: Dictionary of {defect name without charge: [DefectEntry]}.
    """
    from doped.analysis import check_and_set_defect_entry_name

    grouped_entries: dict[str, list[DefectEntry]] = {}  # dict for groups of entries with the same prefix

    for _i, entry in enumerate(entry_list):
        # check defect entry name and (re)define if necessary
        check_and_set_defect_entry_name(entry, entry.name)
        entry_name_wout_charge = entry.name.rsplit("_", 1)[0]

        # If the prefix is not yet in the dictionary, initialize it with empty lists
        if entry_name_wout_charge not in grouped_entries:
            grouped_entries[entry_name_wout_charge] = []

        # Append the current entry to the appropriate group
        grouped_entries[entry_name_wout_charge].append(entry)

    return grouped_entries


class DefectThermodynamics(MSONable):
    """
    Class for analysing the calculated thermodynamics of defects in solids.
    Similar to a pymatgen PhaseDiagram object, allowing the analysis of
    formation energies as functions of Fermi level and chemical potentials
    (i.e. defect formation energy / transition level diagrams), charge
    transition levels, defect/carrier concentrations etc.

    This class is able to get:
        a) stability of charge states for a given defect,
        b) list of all formation energies,
        c) transition levels in the gap,
        d) used as input to doped plotting/analysis functions
    """

    def __init__(
        self,
        defect_entries: Union[dict[str, DefectEntry], list[DefectEntry]],
        chempots: Optional[dict] = None,
        el_refs: Optional[dict] = None,
        vbm: Optional[float] = None,
        band_gap: Optional[float] = None,
        dist_tol: float = 1.5,
        check_compatibility: bool = True,
        bulk_dos: Optional[FermiDos] = None,
        skip_check: bool = False,
    ):
        r"""
        Create a DefectThermodynamics object, which can be used to analyse the
        calculated thermodynamics of defects in solids (formation energies,
        transition levels, concentrations etc).

        Usually initialised using DefectsParser.get_defect_thermodynamics(), but
        can also be initialised with a list or dict of DefectEntry objects (e.g.
        from DefectsParser.defect_dict).

        Note that the ``DefectEntry.name`` attributes are used to label the defects in
        plots.

        Args:
            defect_entries (dict[str, DefectEntry] or list[DefectEntry]):
                A dict or list of ``DefectEntry`` objects. Note that ``DefectEntry.name``
                attributes are used for grouping and plotting purposes! These should
                be in the format "{defect_name}_{optional_site_info}_{charge_state}".
                If the ``DefectEntry.name`` attribute is not defined or does not end with
                the charge state, then the entry will be renamed with the doped
                default name.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies. This can have the form of
                ``{"limits": [{'limit': [chempot_dict]}]}`` (the format generated by
                ``doped``\'s chemical potential parsing functions (see tutorials)) which
                allows easy analysis over a range of chemical potentials - where limit(s)
                (chemical potential limit(s)) to analyse/plot can later be chosen using
                the ``limits`` argument.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases
                in order to show the formal (relative) chemical potentials above the
                formation energy plot, in which case it is the formal chemical potentials
                (i.e. relative to the elemental references) that should be given here,
                otherwise the absolute (DFT) chemical potentials should be given.

                If ``None`` (default), sets all chemical potentials to zero. Chemical
                potentials can also be supplied later in each analysis function, or set
                using ``DefectThermodynamics.chempots = ...`` (with the same input options).
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided in format generated by ``doped`` (see tutorials).

                If ``None`` (default), sets all elemental reference energies to zero. Reference
                energies can also be supplied later in each analysis function, or set
                using ``DefectThermodynamics.el_refs = ...`` (with the same input options).
            vbm (float):
                VBM eigenvalue to use as Fermi level reference point for analysis.
                If None (default), will use ``"vbm"`` from the ``calculation_metadata``
                dict attributes of the parsed ``DefectEntry`` objects, which by default
                is taken from the bulk supercell VBM (unless ``bulk_band_gap_vr`` is set
                during defect parsing).
                Note that ``vbm`` should only affect the reference for the Fermi level
                values output by ``doped`` (as this VBM eigenvalue is used as the zero
                reference), thus affecting the position of the band edges in the defect
                formation energy plots and doping window / dopability limit functions,
                and the reference of the reported Fermi levels.
            band_gap (float):
                Band gap of the host, to use for analysis.
                If None (default), will use "gap" from the calculation_metadata
                dict attributes of the DefectEntry objects in ``defect_entries``.
            dist_tol (float):
                Threshold for the closest distance (in Å) between equivalent
                defect sites, for different species of the same defect type,
                to be grouped together (for plotting and transition level
                analysis). If the minimum distance between equivalent defect
                sites is less than ``dist_tol``, then they will be grouped
                together, otherwise treated as separate defects.
                (Default: 1.5)
            check_compatibility (bool):
                Whether to check the compatibility of the bulk entry for each defect
                entry (i.e. that all reference bulk energies are the same).
                (Default: True)
            bulk_dos (FermiDos or Vasprun or PathLike):
                ``pymatgen`` ``FermiDos`` for the bulk electronic density of states (DOS),
                for calculating Fermi level positions and defect/carrier concentrations.
                Alternatively, can be a ``pymatgen`` ``Vasprun`` object or path to the
                ``vasprun.xml(.gz)`` output of a bulk DOS calculation in VASP.
                Can also be provided later when using ``get_equilibrium_fermi_level()``,
                ``get_quenched_fermi_level_and_concentrations`` etc, or set using
                ``DefectThermodynamics.bulk_dos = ...`` (with the same input options).

                Usually this is a static calculation with the `primitive` cell of the bulk
                material, with relatively dense `k`-point sampling (especially for materials
                with disperse band edges) to ensure an accurately-converged DOS and thus Fermi
                level. ``ISMEAR = -5`` (tetrahedron smearing) is usually recommended for best
                convergence wrt `k`-point sampling. Consistent functional settings should be
                used for the bulk DOS and defect supercell calculations.
                (Default: None)
            skip_check (bool):
                Whether to skip the warning about the DOS VBM differing from the defect
                entries VBM by >0.05 eV. Should only be used when the reason for this
                difference is known/acceptable. (Default: False)

        Key Attributes:
            defect_entries (dict[str, DefectEntry]):
                Dict of ``DefectEntry`` objects included in the ``DefectThermodynamics``
                set, with their names as keys.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and hence concentrations etc), in the ``doped``
                format.
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials.
            vbm (float):
                VBM energy to use as Fermi level reference point for analysis.
            band_gap (float):
                Band gap of the host, to use for analysis.
            dist_tol (float):
                Threshold for the closest distance (in Å) between equivalent
                defect sites, for different species of the same defect type,
                to be grouped together (for plotting and transition level
                analysis).
            transition_levels (dict):
                Dictionary of charge transition levels for each defect entry.
                (e.g. ``{defect_name: {charge: transition_level}}``).
            check_compatibility (bool):
                Whether to check the compatibility of the bulk entry for each defect
                entry (i.e. that all reference bulk energies are the same).
            bulk_formula (str):
                The reduced formula of the bulk structure (e.g. "CdTe").
            bulk_dos (FermiDos):
                ``pymatgen`` ``FermiDos`` for the bulk electronic density of states
                (DOS), used for calculating Fermi level positions and defect/carrier
                concentrations.
            skip_check (bool):
                Whether to skip the warning about the DOS VBM differing from the defect
                entries VBM by >0.05 eV. Should only be used when the reason for this
                difference is known/acceptable.
        """
        if not defect_entries:
            raise ValueError(
                "No defects found in `defect_entries`. Please check the supplied dictionary is in the "
                "correct format (i.e. {'defect_name': defect_entry}), or as a list: [defect_entry]."
            )
        if isinstance(defect_entries, list):
            defect_entries = {entry.name: entry for entry in defect_entries}

        self._defect_entries = defect_entries
        self._chempots, self._el_refs = _parse_chempots(chempots, el_refs, update_el_refs=True)
        self._dist_tol = dist_tol
        self.check_compatibility = check_compatibility
        self.skip_check = skip_check

        # get and check VBM/bandgap values:
        def _raise_VBM_band_gap_value_error(vals, type="VBM"):
            raise ValueError(
                f"{type} values for defects in `defect_dict` do not match within 0.05 eV of each other, "
                f"and so are incompatible for thermodynamic analysis with DefectThermodynamics. The "
                f"{type} values in the dictionary are: {vals}. You should recheck the correct/same bulk "
                f"files were used when parsing. If this is acceptable, you can instead manually specify "
                f"{type} in DefectThermodynamics initialisation."
            )

        self.vbm = vbm
        self.band_gap = band_gap
        if self.vbm is None or self.band_gap is None:
            vbm_vals = []
            band_gap_vals = []
            for defect_entry in self.defect_entries.values():
                if "vbm" in defect_entry.calculation_metadata:
                    vbm_vals.append(defect_entry.calculation_metadata["vbm"])
                if "gap" in defect_entry.calculation_metadata:
                    band_gap_vals.append(defect_entry.calculation_metadata["gap"])

            # get the max difference in VBM & band_gap vals:
            if vbm_vals and max(vbm_vals) - min(vbm_vals) > 0.05 and self.vbm is None:
                _raise_VBM_band_gap_value_error(vbm_vals, type="VBM")
            elif vbm_vals and self.vbm is None:
                self.vbm = vbm_vals[0]

            if band_gap_vals and max(band_gap_vals) - min(band_gap_vals) > 0.05 and self.band_gap is None:
                _raise_VBM_band_gap_value_error(band_gap_vals, type="band_gap")
            elif band_gap_vals and self.band_gap is None:
                self.band_gap = band_gap_vals[0]

        for i, name in [(self.vbm, "VBM eigenvalue"), (self.band_gap, "band gap value")]:
            if i is None:
                raise ValueError(
                    f"No {name} was supplied or able to be parsed from the defect entries "
                    f"(calculation_metadata attributes). Please specify the {name} in the function input."
                )

        self.bulk_dos = bulk_dos  # use setter method, needs to be after setting VBM

        # order entries for deterministic behaviour (particularly for plotting)
        self._sort_parse_and_check_entries()

        bulk_entry = next(iter(self.defect_entries.values())).bulk_entry
        if bulk_entry is not None:
            self.bulk_formula = bulk_entry.structure.composition.get_reduced_formula_and_factor(
                iupac_ordering=True
            )[0]
        else:
            self.bulk_formula = None

    def _sort_parse_and_check_entries(self):
        """
        Sort the defect entries, parse the transition levels, and check the
        compatibility of the bulk entries (if ``self.check_compatibility`` is
        ``True``).
        """
        defect_entries_dict: dict[str, DefectEntry] = {}
        for entry in self.defect_entries.values():
            # rename defect entry names in dict if necessary ("_a", "_b"...)
            entry_name, [
                defect_entries_dict,
            ] = _rename_key_and_dicts(
                entry.name,
                [
                    defect_entries_dict,
                ],
            )
            defect_entries_dict[entry_name] = entry

        sorted_defect_entries_dict = _sort_defect_entries(defect_entries_dict)
        self._defect_entries = sorted_defect_entries_dict
        with warnings.catch_warnings():  # ignore formation energies chempots warning when just parsing TLs
            warnings.filterwarnings("ignore", message="No chemical potentials")
            self._parse_transition_levels()
        if self.check_compatibility:
            self._check_bulk_compatibility()
            self._check_bulk_chempots_compatibility(self._chempots)

    def as_dict(self):
        """
        Returns:
            JSON-serializable dict representation of ``DefectThermodynamics``.
        """
        return {
            "@module": type(self).__module__,
            "@class": type(self).__name__,
            "defect_entries": self.defect_entries,
            "chempots": self.chempots,
            "el_refs": self.el_refs,
            "vbm": self.vbm,
            "band_gap": self.band_gap,
            "dist_tol": self.dist_tol,
            "check_compatibility": self.check_compatibility,
            "bulk_formula": self.bulk_formula,
            "bulk_dos": self.bulk_dos,
            "skip_check": self.skip_check,
        }

    @classmethod
    def from_dict(cls, d):
        """
        Reconstitute a ``DefectThermodynamics`` object from a dict
        representation created using ``as_dict()``.

        Args:
            d (dict): dict representation of ``DefectThermodynamics``.

        Returns:
            ``DefectThermodynamics`` object
        """
        warnings.filterwarnings(
            "ignore", "Use of properties is"
        )  # `message` only needs to match start of message

        def _get_defect_entry(entry_dict):
            if isinstance(entry_dict, DefectEntry):
                return entry_dict
            return DefectEntry.from_dict(entry_dict)

        if isinstance(d.get("defect_entries"), list):
            d["defect_entries"] = [_get_defect_entry(entry_dict) for entry_dict in d.get("defect_entries")]
        else:
            d["defect_entries"] = {
                name: _get_defect_entry(entry_dict) for name, entry_dict in d.get("defect_entries").items()
            }

        return cls(
            defect_entries=d.get("defect_entries"),
            chempots=d.get("chempots"),
            el_refs=d.get("el_refs"),
            vbm=d.get("vbm"),
            band_gap=d.get("band_gap"),
            dist_tol=d.get("dist_tol", 1.5),
            check_compatibility=d.get("check_compatibility", True),
            bulk_dos=FermiDos.from_dict(d.get("bulk_dos")) if d.get("bulk_dos") else None,
            skip_check=d.get("skip_check"),
        )

    def to_json(self, filename: Optional[PathLike] = None):
        """
        Save the ``DefectThermodynamics`` object as a json file, which can be
        reloaded with the ``DefectThermodynamics.from_json()`` class method.

        Note that file extensions with ".gz" will be automatically compressed
        (recommended to save space)!

        Args:
            filename (PathLike): Filename to save json file as. If None, the
                filename will be set as
                ``{Chemical Formula}_defect_thermodynamics.json.gz`` where
                {Chemical Formula} is the chemical formula of the host material.
        """
        if filename is None:
            if self.bulk_formula is not None:
                filename = f"{self.bulk_formula}_defect_thermodynamics.json.gz"
            else:
                filename = "defect_thermodynamics.json.gz"

        dumpfn(self, filename)

    @classmethod
    def from_json(cls, filename: PathLike):
        """
        Load a ``DefectThermodynamics`` object from a json(.gz) file.

        Note that ``.json.gz`` files can be loaded directly.

        Args:
            filename (PathLike):
                Filename of json file to load ``DefectThermodynamics``
                object from.

        Returns:
            ``DefectThermodynamics`` object
        """
        return loadfn(filename)

    def _get_chempots(self, chempots: Optional[dict] = None, el_refs: Optional[dict] = None):
        """
        Parse chemical potentials, either using input values (after formatting
        them in the doped format) or using the class attributes if set.
        """
        if isinstance(chempots, dict) and "elemental_refs" in chempots and el_refs is None:
            # doped chempot dict input, use its elemental refs
            chempots, el_refs = _parse_chempots(chempots, chempots.get("elemental_refs"))

        else:  # use stored or provided el_refs
            chempots, el_refs = _parse_chempots(
                chempots or self.chempots, el_refs or self.el_refs, update_el_refs=True
            )
        if self.check_compatibility:
            self._check_bulk_chempots_compatibility(chempots)

        return chempots, el_refs

    def _parse_transition_levels(self):
        r"""
        Parses the charge transition levels for defect entries in the
        DefectThermodynamics object, and stores information about the stable
        charge states, transition levels etc.

        Defect entries of the same type (e.g. ``Te_i``, ``v_Cd``) are grouped together
        (for plotting and transition level analysis) based on the minimum
        distance between (equivalent) defect sites, to distinguish between
        different inequivalent sites. ``DefectEntry``\s of the same type and with
        a closest distance between equivalent defect sites less than ``dist_tol``
        (1.5 Å by default) are grouped together. If a DefectEntry's site has a
        closest distance less than ``dist_tol`` to multiple sets of equivalent
        sites, then it is matched to the one with the lowest minimum distance.

        Code for parsing the transition levels was originally templated from
        the pyCDT (pymatgen<=2022.7.25) thermodynamics code (deleted in later
        versions).

        This function uses scipy's HalfspaceIntersection
        to construct the polygons corresponding to defect stability as
        a function of the Fermi-level. The Halfspace Intersection
        constructs N-dimensional hyperplanes, in this case N=2, based
        on the equation of defect formation energy with considering chemical
        potentials:
            E_form = E_0^{Corrected} + Q_{defect}*(E_{VBM} + E_{Fermi}).

        Extra hyperplanes are constructed to bound this space so that
        the algorithm can actually find enclosed region.

        This code was modeled after the Halfspace Intersection code for
        the Pourbaix Diagram.
        """
        # determine defect charge transition levels:
        with warnings.catch_warnings():  # ignore formation energies chempots warning when just parsing TLs
            warnings.filterwarnings("ignore", message="Chemical potentials not present for elements")
            midgap_formation_energies = [  # without chemical potentials
                entry.formation_energy(
                    fermi_level=0.5 * self.band_gap, vbm=entry.calculation_metadata.get("vbm", self.vbm)
                )
                for entry in self.defect_entries.values()
            ]
        # note that in general, we have chosen to favour ``entry.calculation_metadata.get("vbm")`` over
        # ``self.vbm`` for all calculations of formation energies / transition levels / concentrations.
        # These values should be the same, and we have warnings if they differ by too much,
        # but in general the ``DefectEntry`` value should be the most reliable, as this is tied to its
        # raw supercell energy difference from the chosen bulk cell for that ``DefectEntry``.
        # but we do use ``self.band_gap`` preferably as this only affects the plot ranges (rather than
        # formation energies)

        # set range to {min E_form - 30, max E_form +30} eV for y (formation energy), and
        # {VBM - 1, CBM + 1} eV for x (fermi level)
        min_y_lim = min(midgap_formation_energies) - 30
        max_y_lim = max(midgap_formation_energies) + 30
        limits = [[-1, self.band_gap + 1], [min_y_lim, max_y_lim]]  # type: ignore

        stable_entries: dict = {}
        defect_charge_map: dict = {}
        transition_level_map: dict = {}
        all_entries: dict = {}  # similar format to stable_entries, but with all (incl unstable) entries

        try:
            defect_site_dict = group_defects_by_distance(
                list(self.defect_entries.values()), dist_tol=self.dist_tol
            )
            grouped_entries_list = [
                entry_list for sub_dict in defect_site_dict.values() for entry_list in sub_dict.values()
            ]
        except Exception as e:
            grouped_entries = group_defects_by_name(list(self.defect_entries.values()))
            grouped_entries_list = list(grouped_entries.values())
            warnings.warn(
                f"Grouping (inequivalent) defects by distance failed with error: {e!r}"
                f"\nGrouping by defect names instead."
            )  # possibly different bulks (though this should be caught/warned about earlier), or not
            # parsed with recent doped versions etc

        for grouped_defect_entries in grouped_entries_list:
            sorted_defect_entries = sorted(
                grouped_defect_entries, key=lambda x: (abs(x.charge_state), x.get_ediff())
            )  # sort by charge, starting with closest to zero, and then formation energy for
            # deterministic behaviour

            # prepping coefficient matrix for half-space intersection
            # [-Q, 1, -1*(E_form+Q*VBM)] -> -Q*E_fermi+E+-1*(E_form+Q*VBM) <= 0 where E_fermi and E are
            # the variables in the hyperplanes
            hyperplanes = np.array(
                [
                    [
                        -1.0 * entry.charge_state,
                        1,
                        -1.0
                        * (
                            entry.get_ediff()
                            + entry.charge_state * entry.calculation_metadata.get("vbm", self.vbm)
                        ),  # type: ignore
                    ]
                    for entry in sorted_defect_entries
                ]
            )

            border_hyperplanes = [
                [-1, 0, limits[0][0]],
                [1, 0, -1 * limits[0][1]],
                [0, -1, limits[1][0]],
                [0, 1, -1 * limits[1][1]],
            ]
            hs_hyperplanes = np.vstack([hyperplanes, border_hyperplanes])
            interior_point = [self.band_gap / 2, min(midgap_formation_energies) - 1.0]  # type: ignore
            hs_ints = HalfspaceIntersection(hs_hyperplanes, np.array(interior_point))

            # Group the intersections and corresponding facets
            ints_and_facets_zip = zip(hs_ints.intersections, hs_ints.dual_facets)
            # Only include the facets corresponding to entries, not the boundaries
            total_entries = len(sorted_defect_entries)
            ints_and_facets_filter = filter(
                lambda int_and_facet: all(np.array(int_and_facet[1]) < total_entries),
                ints_and_facets_zip,
            )
            # sort based on transition level
            ints_and_facets_list = sorted(
                ints_and_facets_filter, key=lambda int_and_facet: int_and_facet[0][0]
            )

            # take simplest (shortest) possible defect name, with lowest energy, as the name for that group
            possible_defect_names_and_energies = [
                (defect_entry.name.rsplit("_", 1)[0], defect_entry.get_ediff())
                for defect_entry in sorted_defect_entries
            ]  # names without charge
            defect_name_wout_charge = min(
                possible_defect_names_and_energies, key=lambda x: (len(x[0]), x[1])
            )[0]

            defect_name_wout_charge, output_dicts = _rename_key_and_dicts(
                defect_name_wout_charge,
                [transition_level_map, all_entries, stable_entries, defect_charge_map],
            )
            transition_level_map, all_entries, stable_entries, defect_charge_map = output_dicts

            if len(ints_and_facets_list) > 0:  # unpack into lists
                _, facets = zip(*ints_and_facets_list)
                transition_level_map[defect_name_wout_charge] = {  # map of transition level: charge states
                    intersection[0]: sorted(
                        [sorted_defect_entries[i].charge_state for i in facet], reverse=True
                    )
                    for intersection, facet in ints_and_facets_list
                }
                stable_entries[defect_name_wout_charge] = sorted(
                    {sorted_defect_entries[i] for dual in facets for i in dual},
                    key=lambda x: (-x.charge_state, x.get_ediff(), len(x.name)),
                )
                defect_charge_map[defect_name_wout_charge] = sorted(
                    [entry.charge_state for entry in sorted_defect_entries], reverse=True
                )

            elif len(sorted_defect_entries) == 1:
                transition_level_map[defect_name_wout_charge] = {}
                stable_entries[defect_name_wout_charge] = [sorted_defect_entries[0]]
                defect_charge_map[defect_name_wout_charge] = [sorted_defect_entries[0].charge_state]

            else:  # if ints_and_facets is empty, then there is likely only one defect...
                # confirm formation energies dominant for one defect over other identical defects
                name_set = [entry.name for entry in sorted_defect_entries]
                with (
                    warnings.catch_warnings()
                ):  # ignore formation energies chempots warning when just parsing TLs
                    warnings.filterwarnings(
                        "ignore", message="Chemical potentials not present for elements"
                    )
                    vb_list = [
                        entry.formation_energy(
                            fermi_level=limits[0][0], vbm=entry.calculation_metadata.get("vbm", self.vbm)
                        )
                        for entry in sorted_defect_entries
                    ]
                    cb_list = [
                        entry.formation_energy(
                            fermi_level=limits[0][1], vbm=entry.calculation_metadata.get("vbm", self.vbm)
                        )
                        for entry in sorted_defect_entries
                    ]

                vbm_def_index = vb_list.index(min(vb_list))
                name_stable_below_vbm = name_set[vbm_def_index]
                cbm_def_index = cb_list.index(min(cb_list))
                name_stable_above_cbm = name_set[cbm_def_index]

                if name_stable_below_vbm != name_stable_above_cbm:
                    raise ValueError(
                        f"HalfSpace identified only one stable charge out of list: {name_set}\n"
                        f"But {name_stable_below_vbm} is stable below vbm and "
                        f"{name_stable_above_cbm} is stable above cbm.\nList of VBM formation "
                        f"energies: {vb_list}\nList of CBM formation energies: {cb_list}"
                    )

                transition_level_map[defect_name_wout_charge] = {}
                stable_entries[defect_name_wout_charge] = [sorted_defect_entries[vbm_def_index]]
                defect_charge_map[defect_name_wout_charge] = sorted(
                    [entry.charge_state for entry in sorted_defect_entries], reverse=True
                )

            all_entries[defect_name_wout_charge] = sorted_defect_entries

        self.transition_level_map = transition_level_map
        self.stable_entries = stable_entries
        self.all_entries = all_entries
        self.defect_charge_map = defect_charge_map

        # sort dictionaries deterministically:
        self.transition_level_map = dict(
            sorted(self.transition_level_map.items(), key=lambda item: self._map_sort_func(item[0]))
        )
        self.stable_entries = dict(
            sorted(self.stable_entries.items(), key=lambda item: self._map_sort_func(item[0]))
        )
        self.all_entries = dict(
            sorted(self.all_entries.items(), key=lambda item: self._map_sort_func(item[0]))
        )
        self.defect_charge_map = dict(
            sorted(self.defect_charge_map.items(), key=lambda item: self._map_sort_func(item[0]))
        )

        self.transition_levels = {
            defect_name: list(defect_tls.keys())
            for defect_name, defect_tls in transition_level_map.items()
        }
        self.stable_charges = {
            defect_name: [entry.charge_state for entry in entries]
            for defect_name, entries in stable_entries.items()
        }

    def _map_sort_func(self, name_wout_charge):
        """
        Convenience sorting function for dictionaries in and outputs from
        ``DefectThermodynamics``.
        """
        for i in range(name_wout_charge.count("_") + 1):  # number of underscores in name
            split_name = name_wout_charge.rsplit("_", i)[0]
            indices = [  # find first occurrence of name_wout_charge in defect_entries
                i for i, name in enumerate(self._defect_entries.keys()) if name.startswith(split_name)
            ]
            if indices:
                return min(indices), split_name

        return 100, split_name  # if name not in defect_entries, put at end

    def _check_bulk_compatibility(self):
        """
        Helper function to quickly check if all entries have compatible bulk
        calculation settings, by checking that the energy of
        ``defect_entry.bulk_entry`` is the same for all defect entries.

        By proxy checks that same bulk/defect calculation settings were used in
        all cases, from each bulk/defect combination already being checked when
        parsing. This is to catch any cases where defects may have been parsed
        separately and combined (rather than altogether with DefectsParser,
        which ensures the same bulk in each case), and where a different bulk
        reference calculation was (mistakenly) used.
        """
        bulk_energies = [entry.bulk_entry.energy for entry in self.defect_entries.values()]
        if max(bulk_energies) - min(bulk_energies) > 0.02:  # 0.02 eV tolerance
            warnings.warn(
                f"Note that not all defects in `defect_entries` have the same reference bulk energy (bulk "
                f"supercell calculation at `bulk_path` when parsing), with energies differing by >0.02 "
                f"eV. This can lead to inaccuracies in predicted formation energies! The bulk energies of "
                f"defect entries in `defect_entries` are:\n"
                f"{[(name, entry.bulk_entry.energy) for name, entry in self.defect_entries.items()]}\n"
                f"You can suppress this warning by setting `DefectThermodynamics.check_compatibility = "
                f"False`."
            )

    def _check_bulk_defects_compatibility(self):
        """
        Helper function to quickly check if all entries have compatible
        defect/bulk calculation settings.

        Currently not used, as the bulk/defect compatibility is checked when
        parsing, and the compatibility across bulk calculations is checked with
        ``_check_bulk_compatibility()``.
        """
        # check each defect entry against its own bulk, and also check each bulk against each other
        reference_defect_entry = next(iter(self.defect_entries.values()))
        reference_run_metadata = reference_defect_entry.calculation_metadata["run_metadata"]
        for defect_entry in self.defect_entries.values():
            with warnings.catch_warnings(record=True) as captured_warnings:
                run_metadata = defect_entry.calculation_metadata["run_metadata"]
                # compare defect and bulk:
                _compare_incar_tags(run_metadata["bulk_incar"], run_metadata["defect_incar"])
                _compare_potcar_symbols(
                    run_metadata["bulk_potcar_symbols"], run_metadata["defect_potcar_symbols"]
                )
                _compare_kpoints(
                    run_metadata["bulk_actual_kpoints"],
                    run_metadata["defect_actual_kpoints"],
                    run_metadata["bulk_kpoints"],
                    run_metadata["defect_kpoints"],
                )

                # compare bulk and reference bulk:
                _compare_incar_tags(
                    reference_run_metadata["bulk_incar"],
                    run_metadata["bulk_incar"],
                    defect_name=f"other bulk (for {reference_defect_entry.name})",
                )
                _compare_potcar_symbols(
                    reference_run_metadata["bulk_potcar_symbols"],
                    run_metadata["bulk_potcar_symbols"],
                    defect_name=f"other bulk (for {reference_defect_entry.name})",
                )
                _compare_kpoints(
                    reference_run_metadata["bulk_actual_kpoints"],
                    run_metadata["defect_actual_kpoints"],
                    reference_run_metadata["bulk_kpoints"],
                    run_metadata["defect_kpoints"],
                    defect_name=f"other bulk (for {reference_defect_entry.name})",
                )

            if captured_warnings:
                concatenated_warnings = "\n".join(str(warning.message) for warning in captured_warnings)
                warnings.warn(
                    f"Incompatible defect/bulk calculation settings detected for defect "
                    f"{defect_entry.name}: \n{concatenated_warnings}"
                )

    def _check_bulk_chempots_compatibility(self, chempots: Optional[dict] = None):
        r"""
        Helper function to quickly check if the supplied chemical potentials
        dictionary matches the bulk supercell used for the defect calculations,
        by comparing the raw energies (from the bulk supercell calculation, and
        that corresponding to the chemical potentials supplied).

        Args:
            chempots (dict, optional):
                Dictionary of chemical potentials to check compatibility with
                the bulk supercell calculations (``DefectEntry.bulk_entry``\s),
                in the ``doped`` format.

                If ``None`` (default), will use ``self.chempots`` (= 0 for all
                chemical potentials by default).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)), or alternatively a dictionary of chemical
                potentials for a single limit (``limit``), in the format:
                ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
        """
        if chempots is None and self.chempots is None:
            return

        bulk_entry = next(entry.bulk_entry for entry in self.defect_entries.values())
        bulk_supercell_energy_per_atom = bulk_entry.energy / bulk_entry.composition.num_atoms
        bulk_chempot_energy_per_atom = (
            raw_energy_from_chempots(bulk_entry.composition, chempots or self.chempots)
            / bulk_entry.composition.num_atoms
        )

        if abs(bulk_supercell_energy_per_atom - bulk_chempot_energy_per_atom) > 0.025:
            warnings.warn(  # 0.05 eV intrinsic defect formation energy error tolerance, taking per-atom
                # chempot error and multiplying by 2 to account for how this would affect antisite
                # formation energies (extreme case)
                f"Note that the raw (DFT) energy of the bulk supercell calculation ("
                f"{bulk_supercell_energy_per_atom:.2f} eV/atom) differs from that expected from the "
                f"supplied chemical potentials ({bulk_chempot_energy_per_atom:.2f} eV/atom) by >0.025 eV. "
                f"This will likely give inaccuracies of similar magnitude in the predicted formation "
                f"energies! \n"
                f"You can suppress this warning by setting `DefectThermodynamics.check_compatibility = "
                f"False`."
            )

    def add_entries(
        self,
        defect_entries: Union[dict[str, DefectEntry], list[DefectEntry]],
        check_compatibility: bool = True,
    ):
        """
        Add additional defect entries to the DefectThermodynamics object.

        Args:
            defect_entries ({str: DefectEntry} or [DefectEntry]):
                A dict or list of ``DefectEntry`` objects, to add to the
                ``DefectThermodynamics.defect_entries`` dict. Note that ``DefectEntry.name``
                attributes are used for grouping and plotting purposes! These should
                be in the format "{defect_name}_{optional_site_info}_{charge_state}".
                If the ``DefectEntry.name`` attribute is not defined or does not end with
                the charge state, then the entry will be renamed with the doped
                default name.
            check_compatibility (bool):
                Whether to check the compatibility of the bulk entry for each defect
                entry (i.e. that all reference bulk energies are the same).
                (Default: True)
        """
        self.check_compatibility = check_compatibility
        if not defect_entries:
            raise ValueError(
                "No defects found in `defect_entries`. Please check the supplied dictionary is in the "
                "correct format (i.e. {'defect_name': defect_entry}), or as a list: [defect_entry]."
            )
        if isinstance(defect_entries, list):  # append 'pre_formatting' so we don't overwrite any existing
            defect_entries = {f"{entry.name}_pre_formatting": entry for entry in defect_entries}

        self._defect_entries.update(defect_entries)  # add new entries and format names
        self._sort_parse_and_check_entries()

    @property
    def defect_entries(self):
        """
        Get the dict of parsed ``DefectEntry`` objects in the
        ``DefectThermodynamics`` analysis object.
        """
        return self._defect_entries

    @defect_entries.setter
    def defect_entries(self, input_defect_entries):
        r"""
        Set the dict of parsed ``DefectEntry``\s to include in the
        ``DefectThermodynamics`` object, and reparse the thermodynamic
        information (transition levels etc).
        """
        self._defect_entries = input_defect_entries
        self._sort_parse_and_check_entries()

    @property
    def chempots(self):
        r"""
        Get the chemical potentials dictionary used for calculating the defect
        formation energies.

        ``chempots`` is a dictionary of chemical potentials to use for calculating
        the defect formation energies, in the form of:
        ``{"limits": [{'limit': [chempot_dict]}]}`` (the format generated by
        ``doped``\'s chemical potential parsing functions (see tutorials)) which
        allows easy analysis over a range of chemical potentials - where limit(s)
        (chemical potential limit(s)) to analyse/plot can later be chosen using
        the ``limits`` argument.
        """
        return self._chempots

    @chempots.setter
    def chempots(self, input_chempots):
        r"""
        Set the chemical potentials dictionary (``chempots``), and reparse to
        have the required ``doped`` format.

        ``chempots`` is a dictionary of chemical potentials to use for calculating
        the defect formation energies, in the form of:
        ``{"limits": [{'limit': [chempot_dict]}]}`` (the format generated by
        ``doped``\'s chemical potential parsing functions (see tutorials)) which
        allows easy analysis over a range of chemical potentials - where limit(s)
        (chemical potential limit(s)) to analyse/plot can later be chosen using
        the ``limits`` argument.

        Alternatively this can be a dictionary of chemical potentials for a
        single limit (limit), in the format: ``{element symbol: chemical potential}``.
        If manually specifying chemical potentials this way, you can set the
        ``el_refs`` option with the DFT reference energies of the elemental phases
        in order to show the formal (relative) chemical potentials above the
        formation energy plot, in which case it is the formal chemical potentials
        (i.e. relative to the elemental references) that should be given here,
        otherwise the absolute (DFT) chemical potentials should be given.

        If None (default), sets all formal chemical potentials (i.e. relative
        to the elemental reference energies in ``el_refs``) to zero. Chemical
        potentials can also be supplied later in each analysis function.
        (Default: None)
        """
        if isinstance(input_chempots, dict) and "elemental_refs" in input_chempots:
            # doped chempot dict input, use its el_refs
            self._chempots, self._el_refs = _parse_chempots(
                input_chempots, input_chempots.get("elemental_refs")
            )

        else:
            self._chempots, self._el_refs = _parse_chempots(
                input_chempots, self._el_refs, update_el_refs=False
            )
        if self.check_compatibility:
            self._check_bulk_chempots_compatibility(self._chempots)

    @property
    def el_refs(self):
        """
        Get the elemental reference energies for the chemical potentials.

        This is in the form of a dictionary of elemental reference energies for
        the chemical potentials, in the format: ``{element symbol: reference
        energy}``.
        """
        return self._el_refs

    @el_refs.setter
    def el_refs(self, input_el_refs):
        """
        Set the elemental reference energies for the chemical potentials
        (``el_refs``), and reparse to have the required ``doped`` format.

        This is in the form of a dictionary of elemental reference energies for the
        chemical potentials, in the format: ``{element symbol: reference energy}``,
        and is used to determine the formal chemical potentials, when ``chempots``
        has been manually specified as ``{element symbol: chemical potential}``.
        Unnecessary if ``chempots`` is provided in format generated by ``doped``
        (see tutorials).
        """
        self._chempots, self._el_refs = _parse_chempots(self._chempots, input_el_refs, update_el_refs=True)

    @property
    def bulk_dos(self):
        """
        Get the ``pymatgen``  ``FermiDos`` for the bulk electronic density of
        states (DOS), for calculating Fermi level positions and defect/carrier
        concentrations, if set.

        Otherwise, returns None.
        """
        return self._bulk_dos

    @bulk_dos.setter
    def bulk_dos(self, input_bulk_dos: Union[FermiDos, Vasprun, PathLike]):
        r"""
        Set the ``pymatgen``  ``FermiDos`` for the bulk electronic density of
        states (DOS), for calculating Fermi level positions and defect/carrier
        concentrations.

        Should be a ``pymatgen`` ``FermiDos`` for the bulk electronic DOS, a
        ``pymatgen`` ``Vasprun`` object or path to the  ``vasprun.xml(.gz)``
        output of a bulk DOS calculation in VASP.
        Can also be provided later when using ``get_equilibrium_fermi_level()``,
        ``get_quenched_fermi_level_and_concentrations`` etc.

        Usually this is a static calculation with the `primitive` cell of the bulk
        material, with relatively dense `k`-point sampling (especially for materials
        with disperse band edges) to ensure an accurately-converged DOS and thus Fermi
        level. ``ISMEAR = -5`` (tetrahedron smearing) is usually recommended for best
        convergence wrt `k`-point sampling. Consistent functional settings should be
        used for the bulk DOS and defect supercell calculations.
        """
        self._bulk_dos = self._parse_fermi_dos(input_bulk_dos, skip_check=self.skip_check)

    @property
    def defect_names(self):
        """
        List of names of defects in the ``DefectThermodynamics`` set.
        """
        return list(self.defect_charge_map.keys())

    @property
    def all_stable_entries(self):
        """
        List all stable entries (defect + charge) in the
        ``DefectThermodynamics`` set.
        """
        return list(chain.from_iterable(self.stable_entries.values()))

    @property
    def all_unstable_entries(self):
        """
        List all unstable entries (defect + charge) in the
        ``DefectThermodynamics`` set.
        """
        return [e for e in self.defect_entries.values() if e not in self.all_stable_entries]

    @property
    def unstable_entries(self):
        """
        Dictionary of unstable entries (``{defect name without charge: [list of
        DefectEntry objects]}``) in the ``DefectThermodynamics`` set.
        """
        return {
            k: [entry for entry in v if entry not in self.stable_entries[k]]
            for k, v in self.all_entries.items()
        }

    @property
    def dist_tol(self):
        r"""
        Get the distance tolerance (in Å) used for grouping (equivalent)
        defects together (for plotting and transition level analysis).

        ``DefectEntry``\s of the same type and with a closest distance between
        equivalent defect sites less than ``dist_tol`` (1.5 Å by default) are
        grouped together. If a DefectEntry's site has a closest distance less
        than ``dist_tol`` to multiple sets of equivalent sites, then it is
        matched to the one with the lowest minimum distance.
        """
        return self._dist_tol

    @dist_tol.setter
    def dist_tol(self, input_dist_tol: float):
        r"""
        Set the distance tolerance (in Å) used for grouping (equivalent)
        defects together (for plotting and transition level analysis), and
        reparse the thermodynamic information (transition levels etc) with this
        tolerance.

        ``DefectEntry``\s of the same type and with a closest distance between
        equivalent defect sites less than ``dist_tol`` (1.5 Å by default) are
        grouped together. If a DefectEntry's site has a closest distance less
        than ``dist_tol`` to multiple sets of equivalent sites, then it is
        matched to the one with the lowest minimum distance.
        """
        self._dist_tol = input_dist_tol
        with warnings.catch_warnings():  # ignore formation energies chempots warning when just parsing TLs
            warnings.filterwarnings("ignore", message="No chemical potentials")
            self._parse_transition_levels()

    def _get_and_set_fermi_level(self, fermi_level: Optional[float] = None) -> float:
        """
        Handle the input Fermi level choice.

        If Fermi level not set, defaults to mid-gap Fermi level (E_g/2) and
        prints an info message to the user.
        """
        if fermi_level is None:
            fermi_level = 0.5 * self.band_gap  # type: ignore
            print(
                f"Fermi level was not set, so using mid-gap Fermi level (E_g/2 = {fermi_level:.2f} eV "
                f"relative to the VBM)."
            )
        return fermi_level

    def get_equilibrium_concentrations(
        self,
        chempots: Optional[dict] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict] = None,
        fermi_level: Optional[float] = None,
        temperature: float = 300,
        per_charge: bool = True,
        per_site: bool = False,
        skip_formatting: bool = False,
        lean: bool = False,
    ) -> pd.DataFrame:
        r"""
        Compute the `equilibrium` concentrations (in cm^-3) for all
        ``DefectEntry``\s in the ``DefectThermodynamics`` object, at a given
        chemical potential limit, fermi_level and temperature, assuming the
        dilute limit approximation.

        Note that these are the `equilibrium` defect concentrations!
        ``DefectThermodynamics.get_quenched_fermi_level_and_concentrations()``
        can instead be used to calculate the Fermi level and defect
        concentrations for a material grown/annealed at higher temperatures
        and then cooled (quenched) to room/operating temperature (where defect
        concentrations are assumed to remain fixed) - this is known as the
        frozen defect approach and is typically the most valid approximation
        (see its docstring for more information).

        The degeneracy/multiplicity factor "g" is an important parameter in the defect
        concentration equation (see discussion in https://doi.org/10.1039/D2FD00043A
        and https://doi.org/10.1039/D3CS00432E), affecting the final concentration by
        up to ~2 orders of magnitude. This factor is taken from the product of the
        ``defect_entry.defect.multiplicity`` and ``defect_entry.degeneracy_factors``
        attributes.

        Args:
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations). If ``None`` (default),
                will use ``self.chempots`` (= 0 for all chemical potentials by default).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (``limit``), in the format:
                ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                obtain the equilibrium concentrations. Can be either:

                - ``None``, if ``chempots`` corresponds to a single chemical potential
                  limit - otherwise will use the first chemical potential limit in the
                  ``chempots`` dict.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)
            fermi_level (float):
                Value corresponding to the electron chemical potential, referenced
                to the VBM (using ``self.vbm``, which is the VBM of the `bulk supercell`
                calculation by default). If None (default), set to the mid-gap Fermi
                level (E_g/2).
            temperature (float):
                Temperature in Kelvin at which to calculate the equilibrium concentrations.
                Default is 300 K.
            per_charge (bool):
                Whether to break down the defect concentrations into individual defect charge
                states (e.g. ``v_Cd_0``, ``v_Cd_-1``, ``v_Cd_-2`` instead of ``v_Cd``).
                (default: True)
            per_site (bool):
                Whether to return the concentrations as percent concentrations per site,
                rather than the default of per cm^3. (default: False)
            skip_formatting (bool):
                Whether to skip formatting the defect charge states and concentrations as
                strings (and keep as ``int``\s and ``float``\s instead). (default: False)
            lean (bool):
                Whether to return a leaner ``DataFrame`` with only the defect name, charge
                state, and concentration in cm^-3 (assumes ``skip_formatting=True``). Only
                really intended for internal ``doped`` usage, to reduce compute times when
                calculating defect concentrations repeatedly. (default: False)
                strings (and keep as ints and floats instead). (default: False)

        Returns:
            ``pandas`` ``DataFrame`` of defect concentrations (and formation energies)
            for each ``DefectEntry`` in the ``DefectThermodynamics`` object.
        """
        fermi_level = self._get_and_set_fermi_level(fermi_level)
        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots/self.el_refs if chempots is None
        skip_formatting = skip_formatting or lean

        energy_concentration_list = []

        if chempots is None:  # only warn once
            _no_chempots_warning()
            all_comps = [
                entry.sc_entry.composition if entry.sc_entry else entry.bulk_entry.composition
                for entry in self.defect_entries.values()
            ]
            empty_el_dict = {el: 0 for el in {el.symbol for comp in all_comps for el in comp}}
            chempots = {
                "limits": {"No User Chemical Potentials": empty_el_dict},
                "limits_wrt_el_refs": {"No User Chemical Potentials": empty_el_dict},
                "elemental_refs": el_refs or empty_el_dict,
            }

        for defect_entry in self.defect_entries.values():
            formation_energy = defect_entry.formation_energy(
                chempots=chempots,
                limit=limit,
                el_refs=el_refs,
                fermi_level=fermi_level,
                vbm=defect_entry.calculation_metadata.get("vbm", self.vbm),
            )
            raw_concentration = defect_entry.equilibrium_concentration(
                chempots=chempots,
                limit=limit,
                el_refs=el_refs,
                fermi_level=fermi_level,
                vbm=defect_entry.calculation_metadata.get("vbm", self.vbm),
                temperature=temperature,
                per_site=per_site,
                formation_energy=formation_energy,  # reduce compute times
            )

            defect_name = defect_entry.name.rsplit("_", 1)[0]  # name without charge
            charge = (
                defect_entry.charge_state
                if skip_formatting
                else f"{'+' if defect_entry.charge_state > 0 else ''}{defect_entry.charge_state}"
            )
            if lean:
                energy_concentration_list.append(
                    {
                        "Defect": defect_name,
                        "Charge": charge,
                        "Concentration (cm^-3)": raw_concentration,
                    }
                )
            else:
                energy_concentration_list.append(
                    {
                        "Defect": defect_name,
                        "Raw Charge": defect_entry.charge_state,  # for sorting
                        "Charge": charge,
                        "Formation Energy (eV)": round(formation_energy, 3),
                        "Raw Concentration": raw_concentration,
                        (
                            "Concentration (per site)" if per_site else "Concentration (cm^-3)"
                        ): _format_concentration(
                            raw_concentration, per_site=per_site, skip_formatting=skip_formatting
                        ),
                    }
                )

        conc_df = pd.DataFrame(energy_concentration_list)

        if per_charge:
            if lean:
                return conc_df

            conc_df["Charge State Population"] = conc_df["Raw Concentration"] / conc_df.groupby("Defect")[
                "Raw Concentration"
            ].transform("sum")
            conc_df["Charge State Population"] = conc_df["Charge State Population"].apply(
                lambda x: f"{x:.2%}"
            )
            conc_df = conc_df.drop(columns=["Raw Charge", "Raw Concentration"])
            return conc_df.reset_index(drop=True)

        # group by defect and sum concentrations:
        return _group_defect_charge_state_concentrations(
            conc_df, per_site=per_site, skip_formatting=skip_formatting
        )

    def _parse_fermi_dos(
        self, bulk_dos: Optional[Union[PathLike, Vasprun, FermiDos]] = None, skip_check: bool = False
    ) -> FermiDos:
        if bulk_dos is None:
            return None

        if isinstance(bulk_dos, FermiDos):
            fdos = bulk_dos
            # most similar settings to Vasprun.eigenvalue_band_properties:
            fdos_vbm = fdos.get_cbm_vbm(tol=1e-4, abs_tol=True)[1]  # tol 1e-4 is lowest possible, as VASP
            fdos_band_gap = fdos.get_gap(tol=1e-4, abs_tol=True)  # rounds the DOS outputs to 4 dp

        if isinstance(bulk_dos, PathLike):
            bulk_dos = get_vasprun(bulk_dos, parse_dos=True)  # converted to fdos in next block

        if isinstance(bulk_dos, Vasprun):  # either supplied Vasprun or parsed from string there
            fdos_band_gap, _cbm, fdos_vbm, _ = bulk_dos.eigenvalue_band_properties
            fdos = get_fermi_dos(bulk_dos)

        if abs(fdos_vbm - self.vbm) > 0.05 and not skip_check:
            warnings.warn(
                f"The VBM eigenvalue of the bulk DOS calculation ({fdos_vbm:.2f} eV, band gap = "
                f"{fdos_band_gap:.2f} eV) differs by >0.05 eV from `DefectThermodynamics.vbm/gap` "
                f"({self.vbm:.2f} eV, band gap = {self.band_gap:.2f} eV; which are taken from the bulk "
                f"supercell calculation by default, unless `bulk_band_gap_vr` is set during defect "
                f"parsing). If this is only due to differences in kpoint sampling for the bulk DOS vs "
                f"supercell calculations, then you should use the `bulk_band_gap_vr` option during "
                f"defect parsing to set the bulk band gap and VBM eigenvalue "
                f"(`DefectThermodynamics.gap/vbm`) to the correct values (though the absolute values of "
                f"predictions should not be affected as the eigenvalue references in the calculations "
                f"are consistent, just the reported Fermi levels will be referenced to "
                f"`DefectThermodynamics.vbm` which may not be the exact VBM position here).\n"
                f"Otherwise if this is due to changes in functional settings (LHFCALC, AEXX etc), then "
                f"the calculations should be redone with consistent settings to ensure accurate "
                f"predictions.\n"
                f"Note that the Fermi level will be always referenced to `DefectThermodynamics.vbm`!"
            )
        return fdos

    def get_equilibrium_fermi_level(
        self,
        bulk_dos: Optional[Union[FermiDos, Vasprun, PathLike]] = None,
        chempots: Optional[dict] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict] = None,
        temperature: float = 300,
        return_concs: bool = False,
        skip_check: bool = False,
        effective_dopant_concentration: Optional[float] = None,
    ) -> Union[float, tuple[float, float, float]]:
        r"""
        Calculate the self-consistent Fermi level, at a given chemical
        potential limit and temperature, assuming `equilibrium` defect
        concentrations (i.e. under annealing) and the dilute limit
        approximation, by self-consistently solving for the Fermi level which
        yields charge neutrality.

        Note that the returned Fermi level is given relative to ``self.vbm``,
        which is the VBM eigenvalue of the bulk supercell calculation by
        default, unless ``bulk_band_gap_vr`` is set during defect parsing.

        Note that this assumes `equilibrium` defect concentrations!
        ``DefectThermodynamics.get_quenched_fermi_level_and_concentrations()``
        can instead be used to calculate the Fermi level and defect
        concentrations for a material grown/annealed at higher temperatures and
        then cooled (quenched) to room/operating temperature (where defect
        concentrations are assumed to remain fixed) - this is known as the
        frozen defect approach and is typically the most valid approximation
        (see its docstring for more information).

        Note that the bulk DOS calculation should be well-converged with respect to
        k-points for accurate Fermi level predictions!

        The degeneracy/multiplicity factor "g" is an important parameter in the defect
        concentration equation and thus Fermi level calculation (see discussion in
        https://doi.org/10.1039/D2FD00043A and https://doi.org/10.1039/D3CS00432E),
        affecting the final concentration by up to ~2 orders of magnitude. This factor
        is taken from the product of the ``defect_entry.defect.multiplicity`` and
        ``defect_entry.degeneracy_factors`` attributes.

        Args:
            bulk_dos (FermiDos or Vasprun or PathLike):
                ``pymatgen`` ``FermiDos`` for the bulk electronic density of states (DOS),
                for calculating carrier concentrations. Alternatively, can be a ``pymatgen``
                ``Vasprun`` object or path to the ``vasprun.xml(.gz)`` output of a bulk DOS
                calculation in VASP -- however this will be much slower when looping over many
                conditions as it will re-parse the DOS each time! (So preferably use
                ``get_fermi_dos()`` as shown in the defect thermodynamics tutorial).

                Usually this is a static calculation with the `primitive` cell of the bulk
                material, with relatively dense `k`-point sampling (especially for materials
                with disperse band edges) to ensure an accurately-converged DOS and thus Fermi
                level. ``ISMEAR = -5`` (tetrahedron smearing) is usually recommended for best
                convergence wrt `k`-point sampling. Consistent functional settings should be
                used for the bulk DOS and defect supercell calculations.

                ``bulk_dos`` can also be left as ``None`` (default), if it has previously
                been provided and parsed, and thus is set as the ``self.bulk_dos`` attribute.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations and Fermi level).
                If ``None`` (default), will use ``self.chempots`` (= 0 for all chemical
                potentials by default).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                determine the equilibrium Fermi level. Can be either:

                - ``None``, if ``chempots`` corresponds to a single chemical potential
                  limit - otherwise will use the first chemical potential limit in the
                  ``chempots`` dict.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)
            temperature (float):
                Temperature in Kelvin at which to calculate the equilibrium Fermi level.
                Default is 300 K.
            return_concs (bool):
                Whether to return the corresponding electron and hole concentrations
                (in cm^-3) as well as the Fermi level. (default: False)
            skip_check (bool):
                Whether to skip the warning about the DOS VBM differing from ``self.vbm``
                by >0.05 eV. Should only be used when the reason for this difference is
                known/acceptable. (default: False)
            effective_dopant_concentration (float):
                Fixed concentration (in cm^-3) of an arbitrary dopant/impurity in the
                material to include in the charge neutrality condition, in order to
                analyse the Fermi level / doping response under hypothetical doping
                conditions. If a negative value is given, the dopant is assumed to be
                an acceptor dopant (i.e. negative defect charge state), while a positive
                value corresponds to donor doping.
                (Default: None; no extrinsic dopant)

        Returns:
            Self consistent Fermi level (in eV from the VBM (``self.vbm``)), and the
            corresponding electron and hole concentrations (in cm^-3) if ``return_concs=True``.
        """
        if bulk_dos is not None:
            self.bulk_dos = self._parse_fermi_dos(bulk_dos, skip_check=skip_check)

        if self.bulk_dos is None:  # none provided, and none previously set
            raise ValueError(
                "No bulk DOS calculation (`bulk_dos`) provided or previously parsed to "
                "`DefectThermodynamics.bulk_dos`, which is required for calculating carrier "
                "concentrations and solving for Fermi level position."
            )

        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots/self.el_refs if chempots is None
        if chempots is None:  # handle once here, as otherwise brentq function results in this being
            # called many times
            _no_chempots_warning()

        def _get_total_q(fermi_level):
            conc_df = self.get_equilibrium_concentrations(
                chempots=chempots,
                limit=limit,
                el_refs=el_refs,
                temperature=temperature,
                fermi_level=fermi_level,
                lean=True,
            )
            # add effective dopant concentration if supplied:
            conc_df = _add_effective_dopant_concentration(conc_df, effective_dopant_concentration)
            qd_tot = (conc_df["Charge"] * conc_df["Concentration (cm^-3)"]).sum()
            qd_tot += get_doping(
                fermi_dos=self.bulk_dos, fermi_level=fermi_level + self.vbm, temperature=temperature
            )
            return qd_tot

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", "No chemical potentials")  # ignore chempots warning,
            # as given once above

            eq_fermi_level: float = brentq(_get_total_q, -1.0, self.band_gap + 1.0)  # type: ignore
            if return_concs:
                e_conc, h_conc = get_e_h_concs(
                    self.bulk_dos, eq_fermi_level + self.vbm, temperature  # type: ignore
                )
                return eq_fermi_level, e_conc, h_conc

        return eq_fermi_level

    def get_quenched_fermi_level_and_concentrations(
        self,
        bulk_dos: Optional[Union[FermiDos, Vasprun, PathLike]] = None,
        chempots: Optional[dict] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict] = None,
        annealing_temperature: float = 1000,
        quenched_temperature: float = 300,
        delta_gap: float = 0,
        per_charge: bool = True,
        per_site: bool = False,
        skip_formatting: bool = False,
        return_annealing_values: bool = False,
        effective_dopant_concentration: Optional[float] = None,
        **kwargs,
    ) -> Union[
        tuple[float, float, float, pd.DataFrame],
        tuple[float, float, float, pd.DataFrame, float, float, float, pd.DataFrame],
    ]:
        r"""
        Calculate the self-consistent Fermi level and corresponding
        carrier/defect calculations, for a given chemical potential limit,
        annealing temperature and quenched/operating temperature, using the
        frozen defect and dilute limit approximations under the constraint of
        charge neutrality.

        According to the 'frozen defect' approximation, we typically expect defect
        concentrations to reach equilibrium during annealing/crystal growth
        (at elevated temperatures), but `not` upon quenching (i.e. at
        room/operating temperature) where we expect kinetic inhibition of defect
        annhiliation and hence non-equilibrium defect concentrations / Fermi level.
        Typically this is approximated by computing the equilibrium Fermi level and
        defect concentrations at the annealing temperature, and then assuming the
        total concentration of each defect is fixed to this value, but that the
        relative populations of defect charge states (and the Fermi level) can
        re-equilibrate at the lower (room) temperature. See discussion in
        https://doi.org/10.1039/D3CS00432E (brief),
        https://doi.org/10.1016/j.cpc.2019.06.017 (detailed) and
        ``doped``/``py-sc-fermi`` tutorials for more information.
        In certain cases (such as Li-ion battery materials or extremely slow charge
        capture/emission), these approximations may have to be adjusted such that some
        defects/charge states are considered fixed and some are allowed to
        re-equilibrate (e.g. highly mobile Li vacancies/interstitials). Modelling
        these specific cases is demonstrated in:
        https://doped.readthedocs.io/en/latest/fermisolver_tutorial.html

        This function works by calculating the self-consistent Fermi level and total
        concentration of each defect at the annealing temperature, then fixing the
        total concentrations to these values and re-calculating the self-consistent
        (constrained equilibrium) Fermi level and relative charge state concentrations
        under this constraint at the quenched/operating temperature.

        Note that the bulk DOS calculation should be well-converged with respect to
        k-points for accurate Fermi level predictions!

        The degeneracy/multiplicity factor "g" is an important parameter in the defect
        concentration equation and thus Fermi level calculation (see discussion in
        https://doi.org/10.1039/D2FD00043A and https://doi.org/10.1039/D3CS00432E),
        affecting the final concentration by up to 2 orders of magnitude. This factor
        is taken from the product of the ``defect_entry.defect.multiplicity`` and
        ``defect_entry.degeneracy_factors`` attributes.

        If you use this code in your work, please also cite:
        Squires et al., (2023). Journal of Open Source Software, 8(82), 4962
        https://doi.org/10.21105/joss.04962

        Args:
            bulk_dos (FermiDos or Vasprun or PathLike):
                ``pymatgen`` ``FermiDos`` for the bulk electronic density of states (DOS),
                for calculating carrier concentrations. Alternatively, can be a ``pymatgen``
                ``Vasprun`` object or path to the ``vasprun.xml(.gz)`` output of a bulk DOS
                calculation in VASP -- however this will be much slower when looping over many
                conditions as it will re-parse the DOS each time! (So preferably use
                ``get_fermi_dos()`` as shown in the defect thermodynamics tutorial).

                Usually this is a static calculation with the `primitive` cell of the bulk
                material, with relatively dense `k`-point sampling (especially for materials
                with disperse band edges) to ensure an accurately-converged DOS and thus Fermi
                level. ``ISMEAR = -5`` (tetrahedron smearing) is usually recommended for best
                convergence wrt `k`-point sampling. Consistent functional settings should be
                used for the bulk DOS and defect supercell calculations.

                ``bulk_dos`` can also be left as ``None`` (default), if it has previously
                been provided and parsed, and thus is set as the ``self.bulk_dos`` attribute.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations and Fermi level).
                If ``None`` (default), will use ``self.chempots`` (= 0 for all chemical
                potentials by default).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                determine the Fermi level and concentrations. Can be either:

                - ``None``, if ``chempots`` corresponds to a single chemical potential
                  limit - otherwise will use the first chemical potential limit in the
                  ``chempots`` dict.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)
            annealing_temperature (float):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to the
                highest temperature during annealing/synthesis of the material (at
                which we assume equilibrium defect concentrations) within the frozen
                defect approach. Default is 1000 K.
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            delta_gap (float):
                Change in band gap (in eV) of the host material at the annealing
                temperature (e.g. due to thermal renormalisation), relative to the
                original band gap of the ``FermiDos`` object (assumed to correspond to the
                quenched temperature). If set, applies a scissor correction to ``fermi_dos``
                which renormalises the band gap symmetrically about the VBM and CBM (i.e.
                assuming equal up/downshifts of the band-edges around their original
                eigenvalues) while the defect levels remain fixed.
                (Default: 0)
            per_charge (bool):
                Whether to break down the defect concentrations into individual defect charge
                states (e.g. ``v_Cd_0``, ``v_Cd_-1``, ``v_Cd_-2`` instead of ``v_Cd``).
                (default: True)
            per_site (bool):
                Whether to return the concentrations as percent concentrations per site,
                rather than the default of per cm^3. (default: False)
            skip_formatting (bool):
                Whether to skip formatting the defect charge states and concentrations as
                strings (and keep as ``int``\s and ``float``\s instead). (default: False)
            return_annealing_values (bool):
                If True, also returns the Fermi level, electron and hole concentrations and
                defect concentrations at the annealing temperature. (default: False)
            effective_dopant_concentration (float):
                Fixed concentration (in cm^-3) of an arbitrary dopant/impurity in the
                material to include in the charge neutrality condition, in order to
                analyse the Fermi level / doping response under hypothetical doping
                conditions. If a negative value is given, the dopant is assumed to be
                an acceptor dopant (i.e. negative defect charge state), while a positive
                value corresponds to donor doping.
                (Default: None; no extrinsic dopant)
            **kwargs:
                Additional keyword arguments to pass to ``scissor_dos`` (if ``delta_gap``
                is not 0) or ``_parse_fermi_dos`` (``skip_check``; to skip the warning about
                the DOS VBM differing from ``self.vbm`` by >0.05 eV; default is False).

        Returns:
            Predicted quenched Fermi level (in eV from the VBM (``self.vbm``)), the
            corresponding electron and hole concentrations (in cm^-3) and a dataframe of the
            quenched defect concentrations (in cm^-3); ``(fermi_level, e_conc, h_conc, conc_df)``.
            If ``return_annealing_values=True``, also returns the annealing Fermi level, electron
            and hole concentrations and a dataframe of the annealing defect concentrations (in cm^-3);
            ``(fermi_level, e_conc, h_conc, conc_df, annealing_fermi_level, annealing_e_conc,
            annealing_h_conc, annealing_conc_df)``.
        """
        if kwargs and any(i not in ["verbose", "tol", "skip_check"] for i in kwargs):
            raise ValueError(f"Invalid keyword arguments: {', '.join(kwargs.keys())}")

        if bulk_dos is not None:
            self.bulk_dos = self._parse_fermi_dos(bulk_dos, skip_check=kwargs.get("skip_check", False))

        if self.bulk_dos is None:  # none provided, and none previously set
            raise ValueError(
                "No bulk DOS calculation (`bulk_dos`) provided or previously parsed to "
                "`DefectThermodynamics.bulk_dos`, which is required for calculating carrier "
                "concentrations and solving for Fermi level position."
            )
        orig_fermi_dos = deepcopy(self.bulk_dos)  # can get modified during annealing loops

        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots/self.el_refs if chempots is None
        if chempots is None:  # handle once here, as otherwise brentq function results in this being
            # called many times
            _no_chempots_warning()

        annealing_dos = (
            self.bulk_dos
            if delta_gap == 0
            else scissor_dos(
                delta_gap,
                self.bulk_dos,
                verbose=kwargs.get("verbose", False),
                tol=kwargs.get("tol", 1e-8),
            )
        )

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", "No chemical potentials")  # ignore chempots warning,
            # as given once above

            annealing_fermi_level = self.get_equilibrium_fermi_level(
                annealing_dos,
                chempots=chempots,
                limit=limit,
                el_refs=el_refs,
                temperature=annealing_temperature,
                return_concs=False,
                effective_dopant_concentration=effective_dopant_concentration,
                skip_check=kwargs.get("skip_check", delta_gap != 0),  # skip check by default if delta
                # gap not 0
            )
            assert not isinstance(annealing_fermi_level, tuple)  # float w/ return_concs=False, for typing
            self.bulk_dos = orig_fermi_dos  # reset to original DOS for quenched calculations

            annealing_defect_concentrations = self.get_equilibrium_concentrations(
                chempots=chempots,
                limit=limit,
                el_refs=el_refs,
                fermi_level=annealing_fermi_level,  # type: ignore
                temperature=annealing_temperature,
                per_charge=False,  # give total concentrations for each defect
                lean=True,
            )
            annealing_defect_concentrations = _add_effective_dopant_concentration(
                annealing_defect_concentrations, effective_dopant_concentration
            )  # add effective dopant concentration if supplied
            total_concentrations = dict(  # {Defect: Total Concentration (cm^-3)}
                zip(
                    annealing_defect_concentrations.index,  # index is Defect name, when per_charge=False
                    annealing_defect_concentrations["Concentration (cm^-3)"],
                )
            )

            def _get_constrained_concentrations(
                fermi_level, per_charge=True, per_site=False, skip_formatting=False, lean=True
            ):
                conc_df = self.get_equilibrium_concentrations(
                    chempots=chempots,
                    limit=limit,
                    el_refs=el_refs,
                    temperature=quenched_temperature,
                    fermi_level=fermi_level,
                    skip_formatting=True,
                    lean=lean,
                )
                conc_df = _add_effective_dopant_concentration(conc_df, effective_dopant_concentration)
                conc_df["Total Concentration (cm^-3)"] = conc_df["Defect"].map(total_concentrations)
                conc_df["Concentration (cm^-3)"] = (  # set total concentration to match annealing conc
                    conc_df["Concentration (cm^-3)"]  # but with same relative concentrations
                    / conc_df.groupby("Defect")["Concentration (cm^-3)"].transform("sum")
                ) * conc_df["Total Concentration (cm^-3)"]

                if not per_charge:
                    conc_df = _group_defect_charge_state_concentrations(
                        conc_df, per_site, skip_formatting=True
                    )

                if per_site:
                    cm3_conc_df = self.get_equilibrium_concentrations(
                        chempots=chempots,
                        limit=limit,
                        el_refs=el_refs,
                        temperature=quenched_temperature,
                        fermi_level=fermi_level,
                        skip_formatting=True,
                        per_charge=per_charge,
                    )
                    per_site_conc_df = self.get_equilibrium_concentrations(
                        chempots=chempots,
                        limit=limit,
                        el_refs=el_refs,
                        temperature=quenched_temperature,
                        fermi_level=fermi_level,
                        skip_formatting=True,
                        per_site=True,
                        per_charge=per_charge,
                    )
                    per_site_factors = (
                        per_site_conc_df["Concentration (per site)"] / cm3_conc_df["Concentration (cm^-3)"]
                    )
                    conc_df["Concentration (per site)"] = (
                        conc_df["Concentration (cm^-3)"] * per_site_factors
                    )
                    conc_df = conc_df.drop(columns=["Concentration (cm^-3)"])

                    if not skip_formatting:
                        conc_df["Concentration (per site)"] = conc_df["Concentration (per site)"].apply(
                            _format_per_site_concentration
                        )

                elif not skip_formatting:
                    conc_df["Concentration (cm^-3)"] = conc_df["Concentration (cm^-3)"].apply(
                        lambda x: f"{x:.3e}"
                    )

                return conc_df

            def _get_constrained_total_q(fermi_level):
                conc_df = _get_constrained_concentrations(fermi_level, skip_formatting=True)
                qd_tot = (conc_df["Charge"] * conc_df["Concentration (cm^-3)"]).sum()
                qd_tot += get_doping(  # use orig fermi dos for quenched temperature
                    fermi_dos=orig_fermi_dos,
                    fermi_level=fermi_level + self.vbm,
                    temperature=quenched_temperature,
                )
                return qd_tot

            eq_fermi_level: float = brentq(
                _get_constrained_total_q, -1.0, self.band_gap + 1.0  # type: ignore
            )
            e_conc, h_conc = get_e_h_concs(
                orig_fermi_dos, eq_fermi_level + self.vbm, quenched_temperature  # type: ignore
            )
            conc_df = _get_constrained_concentrations(
                eq_fermi_level, per_charge, per_site, skip_formatting, lean=False
            )  # not lean for final output

            if not return_annealing_values:
                return (eq_fermi_level, e_conc, h_conc, conc_df)

            annealing_e_conc, annealing_h_conc = get_e_h_concs(
                annealing_dos, annealing_fermi_level + self.vbm, annealing_temperature  # type: ignore
            )
            annealing_defect_concentrations = self.get_equilibrium_concentrations(
                chempots=chempots,
                limit=limit,
                el_refs=el_refs,
                fermi_level=annealing_fermi_level,  # type: ignore
                temperature=annealing_temperature,
                per_charge=per_charge,
                per_site=per_site,
                skip_formatting=skip_formatting,
            )
            return (
                eq_fermi_level,
                e_conc,
                h_conc,
                conc_df,
                annealing_fermi_level,
                annealing_e_conc,
                annealing_h_conc,
                annealing_defect_concentrations,
            )

    def get_formation_energy(
        self,
        defect_entry: Union[str, DefectEntry],
        chempots: Optional[dict] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict] = None,
        fermi_level: Optional[float] = None,
    ) -> float:
        r"""
        Compute the formation energy for a ``DefectEntry`` at a given chemical
        potential limit and fermi_level. ``defect_entry`` can be a string of
        the defect name, of the ``DefectEntry`` object itself.

        Args:
            defect_entry (str or DefectEntry):
                Either a string of the defect entry name (in
                ``DefectThermodynamics.defect_entries``), or a ``DefectEntry`` object.
                If the defect name is given without the charge state, then the
                formation energy of the lowest energy (stable) charge state
                at the chosen Fermi level will be given.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energy. If None (default), will use ``self.chempots``.
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                If None (default), sets all chemical potentials to zero.
                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                calculate the formation energy. Can be either:

                - None (default), if ``chempots`` corresponds to a single chemical
                  potential limit - otherwise will use the first chemical potential
                  limit in the ``chempots`` dict.
                - "X-rich"/"X-poor" where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)
            fermi_level (float):
                Value corresponding to the electron chemical potential, referenced
                to the VBM (using ``self.vbm``, which is the VBM of the `bulk supercell`
                calculation by default). If None (default), set to the mid-gap Fermi
                level (E_g/2).

        Returns:
            Formation energy value (float)
        """
        fermi_level = self._get_and_set_fermi_level(fermi_level)

        if isinstance(defect_entry, DefectEntry):
            return defect_entry.formation_energy(
                chempots=chempots or self.chempots,
                limit=limit,
                el_refs=el_refs,
                vbm=defect_entry.calculation_metadata.get("vbm", self.vbm),
                fermi_level=fermi_level,
            )

        # otherwise is string:
        possible_defect_names = [
            defect_entry,
        ]
        with contextlib.suppress(ValueError):  # in case formatted/not charge state:
            possible_defect_names.append(
                f"{defect_entry.rsplit('_', 1)[0]}_{int(defect_entry.rsplit('_', 1)[1])}"
            )
            possible_defect_names.append(
                f"{defect_entry.rsplit('_', 1)[0]}_+{int(defect_entry.rsplit('_', 1)[1])}"
            )

        exact_match_defect_entries = [
            entry
            for entry in self.defect_entries.values()
            if any(entry.name == possible_defect_name for possible_defect_name in possible_defect_names)
        ]
        if len(exact_match_defect_entries) == 1:
            return exact_match_defect_entries[0].formation_energy(
                chempots=chempots or self.chempots,
                limit=limit,
                el_refs=el_refs,
                vbm=exact_match_defect_entries[0].calculation_metadata.get("vbm", self.vbm),
                fermi_level=fermi_level,
            )

        if matching_defect_entries := [
            entry
            for entry in self.defect_entries.values()
            if any(possible_defect_name in entry.name for possible_defect_name in possible_defect_names)
        ]:
            return min(
                entry.formation_energy(
                    chempots=chempots or self.chempots,
                    limit=limit,
                    el_refs=el_refs,
                    vbm=entry.calculation_metadata.get("vbm", self.vbm),
                    fermi_level=fermi_level,
                )
                for entry in matching_defect_entries
            )

        raise ValueError(
            f"No matching DefectEntry with {defect_entry} in name found in "
            f"DefectThermodynamics.defect_entries, which have names:\n{list(self.defect_entries.keys())}"
        )

    def get_dopability_limits(
        self, chempots: Optional[dict] = None, limit: Optional[str] = None, el_refs: Optional[dict] = None
    ) -> pd.DataFrame:
        r"""
        Find the dopability limits of the defect system, searching over all
        limits (chemical potential limits) in ``chempots`` and returning the
        most p/n-type conditions, or for a given chemical potential limit (if
        ``limit`` is set or ``chempots`` corresponds to a single chemical
        potential limit; i.e. {element symbol: chemical potential}).

        The dopability limites are defined by the (first) Fermi level positions at
        which defect formation energies become negative as the Fermi level moves
        towards/beyond the band edges, thus determining the maximum possible Fermi
        level range upon doping for this chemical potential limit.

        Note that the Fermi level positions are given relative to ``self.vbm``,
        which is the VBM eigenvalue of the bulk supercell calculation by
        default, unless ``bulk_band_gap_vr`` is set during defect parsing.

        This is computed by obtaining the formation energy for every stable defect
        with non-zero charge, and then finding the highest Fermi level position at
        which a donor defect (positive charge) has zero formation energy (crosses
        the x-axis) - giving the lower dopability limit, and the lowest Fermi level
        position at which an acceptor defect (negative charge) has zero formation
        energy - giving the upper dopability limit.

        Args:
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus dopability limits).
                If ``None`` (default), will use ``self.chempots``.
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                calculate formation energies (and thus dopability limits). Can be either:

                - ``None``, in which case we search over all limits (chemical potential
                  limits) in ``chempots`` and return the most n/p-type conditions,
                  unless ``chempots`` corresponds to a single chemical potential limit.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)

        Returns:
            ``pandas`` ``DataFrame`` of dopability limits, with columns:
            "limit", "Compensating Defect", "Dopability Limit" for both p/n-type
            where 'Dopability limit' are the corresponding Fermi level positions in
            eV, relative to the VBM (``self.vbm``).
        """
        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots if chempots is None
        if chempots is None:
            raise ValueError(
                "No chemical potentials supplied or present in "
                "DefectThermodynamics.chempots, so dopability limits cannot be calculated."
            )

        limit = _parse_limit(chempots, limit)
        limits = [limit] if limit is not None else list(chempots["limits"].keys())

        donor_intercepts: list[tuple] = []
        acceptor_intercepts: list[tuple] = []

        for entry in self.all_stable_entries:
            if entry.charge_state > 0:  # donor
                # formation energy is y = mx + c where m = charge_state, c = vbm_formation_energy
                # so x-intercept is -c/m:
                donor_intercepts.extend(
                    (
                        limit,
                        entry.name,
                        -self.get_formation_energy(
                            entry,
                            chempots=chempots,
                            limit=limit,
                            el_refs=el_refs,
                            fermi_level=0,
                        )
                        / entry.charge_state,
                    )
                    for limit in limits
                )
            elif entry.charge_state < 0:  # acceptor
                acceptor_intercepts.extend(
                    (
                        limit,
                        entry.name,
                        -self.get_formation_energy(
                            entry,
                            chempots=chempots,
                            limit=limit,
                            el_refs=el_refs,
                            fermi_level=0,
                        )
                        / entry.charge_state,
                    )
                    for limit in limits
                )

        if not donor_intercepts:
            donor_intercepts = [("N/A", "N/A", -np.inf)]
        if not acceptor_intercepts:
            acceptor_intercepts = [("N/A", "N/A", np.inf)]

        donor_intercepts_df = pd.DataFrame(donor_intercepts, columns=["limit", "name", "intercept"])
        acceptor_intercepts_df = pd.DataFrame(acceptor_intercepts, columns=["limit", "name", "intercept"])

        # get the most p/n-type limit, by getting the limit with the minimum/maximum max/min-intercept,
        # where max/min-intercept is the max/min intercept for that limit (i.e. the compensating intercept)
        idx = (
            donor_intercepts_df.groupby("limit")["intercept"].transform("max")
            == donor_intercepts_df["intercept"]
        )
        limiting_donor_intercept_row = donor_intercepts_df.iloc[
            donor_intercepts_df[idx]["intercept"].idxmin()
        ]
        idx = (
            acceptor_intercepts_df.groupby("limit")["intercept"].transform("min")
            == acceptor_intercepts_df["intercept"]
        )
        limiting_acceptor_intercept_row = acceptor_intercepts_df.iloc[
            acceptor_intercepts_df[idx]["intercept"].idxmax()
        ]

        if limiting_donor_intercept_row["intercept"] > limiting_acceptor_intercept_row["intercept"]:
            warnings.warn(
                "Donor and acceptor doping limits intersect at negative defect formation energies "
                "(unphysical)!"
            )

        try:
            limit_dict = get_rich_poor_limit_dict(chempots)
        except ValueError:
            limit_dict = {}

        return pd.DataFrame(
            [
                [
                    _get_limit_name_from_dict(
                        limiting_donor_intercept_row["limit"], limit_dict, bracket=True
                    ),
                    limiting_donor_intercept_row["name"],
                    round(limiting_donor_intercept_row["intercept"], 3),
                ],
                [
                    _get_limit_name_from_dict(
                        limiting_acceptor_intercept_row["limit"], limit_dict, bracket=True
                    ),
                    limiting_acceptor_intercept_row["name"],
                    round(limiting_acceptor_intercept_row["intercept"], 3),
                ],
            ],
            columns=["limit", "Compensating Defect", "Dopability Limit (eV from VBM/CBM)"],
            index=["p-type", "n-type"],
        )

    def get_doping_windows(
        self, chempots: Optional[dict] = None, limit: Optional[str] = None, el_refs: Optional[dict] = None
    ) -> pd.DataFrame:
        r"""
        Find the doping windows of the defect system, searching over all limits
        (chemical potential limits) in ``chempots`` and returning the most
        p/n-type conditions, or for a given chemical potential limit (if
        ``limit`` is set or ``chempots`` corresponds to a single chemical
        potential limit; i.e. {element symbol: chemical potential}).

        Doping window is defined by the formation energy of the lowest energy
        compensating defect species at the corresponding band edge (i.e. VBM for
        hole doping and CBM for electron doping), as these set the upper limit to
        the formation energy of dopants which could push the Fermi level close to
        the band edge without being negated by defect charge compensation.

        Note that the band edge positions are taken from ``self.vbm`` and
        ``self.band_gap``, which are parsed from the `bulk supercell calculation` by
        default, unless ``bulk_band_gap_vr`` is set during defect parsing.

        If a dopant has a higher formation energy than the doping window at the
        band edge, then its charge will be compensated by formation of the
        corresponding limiting defect species (rather than free carrier populations).

        Args:
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus doping windows).
                If ``None`` (default), will use ``self.chempots``.
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                calculate formation energies (and thus doping windows). Can be either:

                - ``None``, in which case we search over all limits (chemical potential
                  limits) in ``chempots`` and return the most n/p-type conditions,
                  unless ``chempots`` corresponds to a single chemical potential limit.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)

        Returns:
            ``pandas`` ``DataFrame`` of doping windows, with columns:
            "limit", "Compensating Defect", "Doping Window" for both p/n-type
            where 'Doping Window' are the corresponding doping windows in eV.
        """
        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots if chempots is None
        if chempots is None:
            raise ValueError(
                "No chemical potentials supplied or present in "
                "DefectThermodynamics.chempots, so doping windows cannot be calculated."
            )

        limit = _parse_limit(chempots, limit)
        limits = [limit] if limit is not None else list(chempots["limits"].keys())

        vbm_donor_intercepts: list[tuple] = []
        cbm_acceptor_intercepts: list[tuple] = []

        for entry in self.all_stable_entries:
            if entry.charge_state > 0:  # donor
                vbm_donor_intercepts.extend(
                    (
                        limit,
                        entry.name,
                        self.get_formation_energy(
                            entry,
                            chempots=chempots,
                            limit=limit,
                            el_refs=el_refs,
                            fermi_level=0,
                        ),
                    )
                    for limit in limits
                )
            elif entry.charge_state < 0:  # acceptor
                cbm_acceptor_intercepts.extend(
                    (
                        limit,
                        entry.name,
                        self.get_formation_energy(
                            entry,
                            chempots=chempots,
                            limit=limit,
                            el_refs=el_refs,
                            fermi_level=self.band_gap,  # type: ignore[arg-type]
                        ),
                    )
                    for limit in limits
                )
        if not vbm_donor_intercepts:
            vbm_donor_intercepts = [("N/A", "N/A", np.inf)]
        if not cbm_acceptor_intercepts:
            cbm_acceptor_intercepts = [("N/A", "N/A", -np.inf)]

        vbm_donor_intercepts_df = pd.DataFrame(
            vbm_donor_intercepts, columns=["limit", "name", "intercept"]
        )
        cbm_acceptor_intercepts_df = pd.DataFrame(
            cbm_acceptor_intercepts, columns=["limit", "name", "intercept"]
        )

        try:
            limit_dict = get_rich_poor_limit_dict(chempots)
        except ValueError:
            limit_dict = {}

        # get the most p/n-type limit, by getting the limit with the maximum min-intercept, where
        # min-intercept is the min intercept for that limit (i.e. the compensating intercept)
        limiting_intercept_rows = []
        for intercepts_df in [vbm_donor_intercepts_df, cbm_acceptor_intercepts_df]:
            idx = (
                intercepts_df.groupby("limit")["intercept"].transform("min") == intercepts_df["intercept"]
            )
            limiting_intercept_row = intercepts_df.iloc[intercepts_df[idx]["intercept"].idxmax()]
            limiting_intercept_rows.append(
                [
                    _get_limit_name_from_dict(limiting_intercept_row["limit"], limit_dict, bracket=True),
                    limiting_intercept_row["name"],
                    round(limiting_intercept_row["intercept"], 3),
                ]
            )

        return pd.DataFrame(
            limiting_intercept_rows,
            columns=["limit", "Compensating Defect", "Doping Window (eV at VBM/CBM)"],
            index=["p-type", "n-type"],
        )

    # TODO: Don't show chempot table by default? At least is limit explicitly chosen?
    # TODO: Add option to only plot defect states that are stable at some point in the bandgap
    # TODO: Add option to plot formation energies at the centroid of the chemical stability region? And
    #  make this the default if no chempots are specified? Or better default to plot both the most (
    #  most-electronegative-)anion-rich and the (most-electropositive-)cation-rich chempot limits?
    # TODO: Likewise, add example showing how to plot a metastable state (above the ground state)
    # TODO: Should have similar colours for similar defect types, an option to just show amalgamated
    #  lowest energy charge states for each _defect type_) - equivalent to setting the dist_tol to
    #  infinity (but should be easier to just do here by taking the short defect name). NaP is an example
    #  for this - should have a test built for however we want to handle cases like this. See Ke's example
    #  case too with different interstitial sites.
    #   Related: Currently updating `dist_tol` to change the number of defects being plotted,
    #   can also change the colours of the different defect lines (e.g. for CdTe_wout_meta increasing
    #   `dist_tol` to 2 to merge all Te interstitials, results in the colours of other defect lines (
    #   e.g. Cd_Te) changing at the same time - ideally this wouldn't happen!
    #  TODO: optionally retain/remove unstable (in the gap) charge states (rather than current
    #  default range of (VBM - 1eV, CBM + 1eV))... depends on if shallow defect tagging with pydefect is
    #  implemented or not really, what would be best to do by default

    def plot(
        self,
        chempots: Optional[dict] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict] = None,
        chempot_table: bool = True,
        all_entries: Union[bool, str] = False,
        style_file: Optional[PathLike] = None,
        xlim: Optional[tuple] = None,
        ylim: Optional[tuple] = None,
        fermi_level: Optional[float] = None,
        include_site_info: bool = False,
        colormap: Optional[Union[str, colors.Colormap]] = None,
        linestyles: Union[str, list[str]] = "-",
        auto_labels: bool = False,
        filename: Optional[PathLike] = None,
    ) -> Union[Figure, list[Figure]]:
        r"""
        Produce a defect formation energy vs Fermi level plot (a.k.a. a defect
        formation energy / transition level diagram), returning the
        ``matplotlib`` ``Figure`` object to allow further plot customisation.

        Note that the band edge positions are taken from ``self.vbm`` and
        ``self.band_gap``, which are parsed from the `bulk supercell calculation` by
        default, unless ``bulk_band_gap_vr`` is set during defect parsing.

        Args:
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies. If None (default), will use ``self.chempots``.
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases
                in order to show the formal (relative) chemical potentials above the
                formation energy plot, in which case it is the formal chemical potentials
                (i.e. relative to the elemental references) that should be given here,
                otherwise the absolute (DFT) chemical potentials should be given.

                If None (default), sets all chemical potentials to zero.
                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                plot formation energies. Can be either:

                - None, in which case plots are generated for all limits in ``chempots``.
                - "X-rich"/"X-poor" where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)
            chempot_table (bool):
                Whether to print the chemical potential table above the plot.
                (Default: True)
            all_entries (bool, str):
                Whether to plot the formation energy lines of `all` defect entries,
                rather than the default of showing only the equilibrium states at each
                Fermi level position (traditional). If instead set to "faded", will plot
                the equilibrium states in bold, and all unstable states in faded grey
                (Default: False)
            style_file (PathLike):
                Path to a mplstyle file to use for the plot. If None (default), uses
                the default doped style (from ``doped/utils/doped.mplstyle``).
            xlim:
                Tuple (min,max) giving the range of the x-axis (Fermi level). May want
                to set manually when including transition level labels, to avoid crossing
                the axes. Default is to plot from -0.3 to +0.3 eV above the band gap.
            ylim:
                Tuple (min,max) giving the range for the y-axis (formation energy). May
                want to set manually when including transition level labels, to avoid
                crossing the axes. Default is from 0 to just above the maximum formation
                energy value in the band gap.
            fermi_level (float):
                If set, plots a dashed vertical line at this Fermi level value, typically
                used to indicate the equilibrium Fermi level position (e.g. calculated
                with py-sc-fermi). (Default: None)
            include_site_info (bool):
                Whether to include site info in defect names in the plot legend (e.g.
                $Cd_{i_{C3v}}^{0}$ rather than $Cd_{i}^{0}$). Default is ``False``, where
                site info is not included unless we have inequivalent sites for the same
                defect type. If, even with site info added, there are duplicate defect
                names, then "-a", "-b", "-c" etc are appended to the names to differentiate.
            colormap (str, matplotlib.colors.Colormap):
                Colormap to use for the formation energy lines, either as a string
                (which can be a colormap name from
                https://matplotlib.org/stable/users/explain/colors/colormaps or from
                https://www.fabiocrameri.ch/colourmaps -- append 'S' if using a sequential
                colormap from the latter) or a ``Colormap`` / ``ListedColormap`` object.
                If ``None`` (default), uses ``tab10`` with ``alpha=0.75`` (if 10 or fewer
                lines to plot), ``tab20`` (if 20 or fewer lines) or ``batlow`` (if more
                than 20 lines; citation: https://zenodo.org/records/8409685).
            linestyles (list):
                Linestyles to use for the formation energy lines, either as a single
                linestyle (``str``) or list of linestyles (``list[str]``) in the order of
                appearance of lines in the plot legend. Default is ``"-"``; i.e. solid
                linestyle for all entries.
            auto_labels (bool):
                Whether to automatically label the transition levels with their charge
                states. If there are many transition levels, this can be quite ugly.
                (Default: False)
            filename (PathLike): Filename to save the plot to. (Default: None (not saved))

        Returns:
            ``matplotlib`` ``Figure`` object, or list of ``Figure`` objects if multiple
            limits chosen.
        """
        from shakenbreak.plotting import _install_custom_font

        _install_custom_font()
        # check input options:
        if all_entries not in [False, True, "faded"]:
            raise ValueError(
                f"`all_entries` option must be either False, True, or 'faded', not {all_entries}"
            )

        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots/self.el_refs if chempots is None
        if chempots is None:
            chempots = {
                "limits": {"No User Chemical Potentials": None}
            }  # empty chempots dict to allow plotting, user will be warned

        limit = _parse_limit(chempots, limit)
        limits = [limit] if limit is not None else list(chempots["limits"].keys())

        if (
            chempots
            and limit is None
            and el_refs is None
            and "limits" not in chempots
            and any(np.isclose(chempot, 0, atol=0.1) for chempot in chempots.values())
        ):
            # if any chempot is close to zero, this is likely a formal chemical potential and so inaccurate
            # here (trying to make this as idiotproof as possible to reduce unnecessary user queries...)
            warnings.warn(
                "At least one of your manually-specified chemical potentials is close to zero, "
                "which is likely a _formal_ chemical potential (i.e. relative to the elemental "
                "reference energies), but you have not specified the elemental reference "
                "energies with `el_refs`. This will give large errors in the absolute values "
                "of formation energies, but the transition level positions will be unaffected."
            )

        style_file = style_file or f"{os.path.dirname(__file__)}/utils/doped.mplstyle"
        plt.style.use(style_file)  # enforce style, as style.context currently doesn't work with jupyter
        with plt.style.context(style_file):
            figs = []
            for limit in limits:
                dft_chempots = chempots["limits"][limit]
                plot_title = limit if len(limits) > 1 else None
                plot_filename = (
                    f"{filename.rsplit('.', 1)[0]}_{limit}.{filename.rsplit('.', 1)[1]}"
                    if filename
                    else None
                )

                with warnings.catch_warnings():  # avoid double warning about no chempots supplied
                    warnings.filterwarnings("ignore", "No chemical potentials")
                    fig = _TLD_plot(
                        self,
                        dft_chempots=dft_chempots,
                        el_refs=el_refs,
                        chempot_table=chempot_table,
                        all_entries=all_entries,
                        xlim=xlim,
                        ylim=ylim,
                        fermi_level=fermi_level,
                        include_site_info=include_site_info,
                        title=plot_title,
                        colormap=colormap,
                        linestyles=linestyles,
                        auto_labels=auto_labels,
                        filename=plot_filename,
                    )
                figs.append(fig)

            return figs[0] if len(figs) == 1 else figs

    def get_transition_levels(
        self,
        all: bool = False,
        format_charges: bool = True,
    ) -> Union[pd.DataFrame, None]:
        """
        Return a ``DataFrame`` of the charge transition levels for the defects
        in the ``DefectThermodynamics`` object (stored in the
        ``transition_level_map`` attribute).

        Note that the transition level (and Fermi level) positions are given
        relative to ``self.vbm``, which is the VBM eigenvalue of the bulk
        supercell calculation by default, unless ``bulk_band_gap_vr`` is set
        during defect parsing.

        By default, only returns the thermodynamic ground-state transition
        levels (i.e. those visible on the defect formation energy diagram),
        not including metastable defect states (which can be important for
        recombination, migration, degeneracy/concentrations etc, see e.g.
        https://doi.org/10.1039/D2FD00043A & https://doi.org/10.1039/D3CS00432E).
        e.g. negative-U defects will show the 2-electron transition level
        (N+1/N-1) rather than (N+1/N) and (N/N-1).
        If instead all single-electron transition levels are desired, set
        ``all = True``.

        Returns a ``DataFrame`` with columns:

        - "Defect": Defect name
        - "Charges": Defect charge states which make up the transition level
            (as a string if ``format_charges=True``, otherwise as a list of integers)
        - "eV from VBM": Transition level position in eV from the VBM (``self.vbm``)
        - "In Band Gap?": Whether the transition level is within the host band gap
        - "N(Metastable)": Number of metastable states involved in the transition level
            (0, 1 or 2). Only included if all = True.

        Args:
              all (bool):
                    Whether to print all single-electron transition levels (i.e.
                    including metastable defect states), or just the thermodynamic
                    ground-state transition levels (default).
              format_charges (bool):
                    Whether to format the transition level charge states as strings
                    (e.g. "ε(+1/+2)") or keep in list format (e.g. [1,2]).
                    (Default: True)
        """
        # create a dataframe from the transition level map, with defect name, transition level charges and
        # TL position in eV from the VBM:
        transition_level_map_list = []

        def _TL_naming_func(TL_charges, i_meta=False, j_meta=False):
            if not format_charges:
                return TL_charges
            i, j = TL_charges
            return (
                f"ε({'+' if i > 0 else ''}{i}{'*' if i_meta else ''}/"
                f"{'+' if j > 0 else ''}{j}{'*' if j_meta else ''})"
            )

        for defect_name, transition_level_dict in self.transition_level_map.items():
            if not transition_level_dict:
                transition_level_map_list.append(  # add defects with no TL to dataframe as "None"
                    {
                        "Defect": defect_name,
                        "Charges": "None",
                        "eV from VBM": np.inf,
                        "In Band Gap?": False,
                        "-q_i": np.inf,  # for sorting
                        "-q_j": np.inf,  # for sorting
                    }
                )
                if all:
                    transition_level_map_list[-1]["N(Metastable)"] = 0

            if not all:
                transition_level_map_list.extend(
                    {
                        "Defect": defect_name,
                        "Charges": _TL_naming_func(transition_level_charges),
                        "eV from VBM": round(TL, 3),
                        "In Band Gap?": (TL > 0) and (self.band_gap > TL),
                        "-q_i": -transition_level_charges[0],  # for sorting
                        "-q_j": -transition_level_charges[1],  # for sorting
                    }
                    for TL, transition_level_charges in transition_level_dict.items()
                )

        # now get metastable TLs
        if all:
            for defect_name_wout_charge, grouped_defect_entries in self.all_entries.items():
                sorted_defect_entries = sorted(
                    grouped_defect_entries, key=lambda x: x.charge_state
                )  # sort by charge
                for i, j in product(sorted_defect_entries, repeat=2):
                    if i.charge_state - j.charge_state == 1:
                        # take mean VBM, ofc should be the same, but allow for small differences
                        mean_VBM = np.mean([x.calculation_metadata.get("vbm", self.vbm) for x in [i, j]])
                        TL = j.get_ediff() - i.get_ediff() - mean_VBM
                        i_meta = not any(i == y for y in self.all_stable_entries)
                        j_meta = not any(j == y for y in self.all_stable_entries)
                        transition_level_map_list.append(
                            {
                                "Defect": defect_name_wout_charge,
                                "Charges": _TL_naming_func(
                                    [i.charge_state, j.charge_state], i_meta=i_meta, j_meta=j_meta
                                ),
                                "eV from VBM": round(TL, 3),
                                "In Band Gap?": (TL > 0) and (self.band_gap > TL),
                                "N(Metastable)": [i_meta, j_meta].count(True),
                                "-q_i": -i.charge_state,  # for sorting
                                "-q_j": -j.charge_state,  # for sorting
                            }
                        )

        if not transition_level_map_list:
            warnings.warn("No transition levels found for chosen parameters!")
            return None
        tl_df = pd.DataFrame(transition_level_map_list)

        if "N(Metastable)" not in tl_df.columns:  # add, because we use it for sorting in case all=True
            tl_df["N(Metastable)"] = 0

        # sort df by Defect appearance order in defect_entries, Defect, then by TL position:
        tl_df["Defect Appearance Order"] = tl_df["Defect"].map(self._map_sort_func)
        tl_df = tl_df.sort_values(
            by=["Defect Appearance Order", "Defect", "-q_i", "-q_j", "N(Metastable)", "eV from VBM"]
        )
        tl_df = tl_df.drop(columns=["Defect Appearance Order", "-q_i", "-q_j"])
        if not all:
            tl_df = tl_df.drop(columns="N(Metastable)")
        return tl_df.set_index(["Defect", "Charges"])

    def print_transition_levels(self, all: bool = False):
        """
        Iteratively prints the charge transition levels for the defects in the
        DefectThermodynamics object (stored in the transition_level_map
        attribute).

        By default, only returns the thermodynamic ground-state transition
        levels (i.e. those visible on the defect formation energy diagram),
        not including metastable defect states (which can be important for
        recombination, migration, degeneracy/concentrations etc, see e.g.
        https://doi.org/10.1039/D2FD00043A & https://doi.org/10.1039/D3CS00432E).
        e.g. negative-U defects will show the 2-electron transition level
        (N+1/N-1) rather than (N+1/N) and (N/N-1).
        If instead all single-electron transition levels are desired, set
        ``all = True``.

        Args:
              all (bool):
                    Whether to print all single-electron transition levels (i.e.
                    including metastable defect states), or just the thermodynamic
                    ground-state transition levels (default).
        """
        if not all:
            for defect_name, tl_info in self.transition_level_map.items():
                bold_print(f"Defect: {defect_name}")
                for tl_efermi, chargeset in tl_info.items():
                    print(
                        f"Transition level ε({max(chargeset):{'+' if max(chargeset) else ''}}/"
                        f"{min(chargeset):{'+' if min(chargeset) else ''}}) at {tl_efermi:.3f} eV above "
                        f"the VBM"
                    )
                print("")  # add space

        else:
            all_TLs_df = self.get_transition_levels(all=True)
            if all_TLs_df is None:
                return
            for defect_name, tl_df in all_TLs_df.groupby("Defect", sort=False):
                bold_print(f"Defect: {defect_name}")
                for index, row in tl_df.iterrows():
                    if index[1] != "None":  # charges
                        print(f"Transition level {index[1]} at {row['eV from VBM']:.3f} eV above the VBM")
                print("")  # add space

    def get_formation_energies(
        self,
        chempots: Optional[dict] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict] = None,
        fermi_level: Optional[float] = None,
        skip_formatting: bool = False,
    ) -> Union[pd.DataFrame, list[pd.DataFrame]]:
        r"""
        Generates defect formation energy tables (DataFrames) for either a
        single chemical potential limit (i.e. phase diagram ``limit``) or each
        limit in the phase diagram (chempots dict), depending on input
        ``limit`` and ``chempots``.

        Table Key: (all energies in eV):

        - 'Defect':
            Defect name (without charge)
        - 'q':
            Defect charge state.
        - 'ΔEʳᵃʷ':
            Raw DFT energy difference between defect and host supercell (E_defect - E_host).
        - 'qE_VBM':
            Defect charge times the VBM eigenvalue (to reference the Fermi level to the VBM)
        - 'qE_F':
            Defect charge times the Fermi level (referenced to the VBM if qE_VBM is not 0
            (if "vbm" in ``DefectEntry.calculation_metadata``)
        - 'Σμ_ref':
            Sum of reference energies of the elemental phases in the chemical potentials sum.
        - 'Σμ_formal':
            Sum of `formal` atomic chemical potential terms (Σμ_DFT = Σμ_ref + Σμ_formal).
        - 'E_corr':
            Finite-size supercell charge correction.
        - 'Eᶠᵒʳᵐ':
            Defect formation energy, with the specified chemical potentials and Fermi level.
            Equals the sum of all other terms.
        - 'Path':
            Path to the defect calculation folder.
        - 'Δ[E_corr]':
            Estimated error in the charge correction, from the variance of the potential in
            the sampling region.

        Args:
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies. If None (default), will use ``self.chempots``.
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}]}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                If None (default), sets all chemical potentials to zero.
                Note that you can also set ``DefectThermodynamics.chempots = ...``
                (with the same input options) to set the default chemical potentials
                for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to
                tabulate formation energies. Can be either:

                - None, in which case tables are generated for all limits in ``chempots``.
                - "X-rich"/"X-poor" where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.)chempots["limits"]`` dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``DefectThermodynamics.el_refs = ...``
                (with the same input options) to set the default elemental reference
                energies for all calculations.
                (Default: None)
            fermi_level (float):
                Value corresponding to the electron chemical potential, referenced
                to the VBM eigenvalue, which is taken from the ``calculation_metadata``
                dict attributes of ``DefectEntry``\s in ``self.defect_entries`` if present,
                otherwise ``self.vbm`` -- which correspond to the VBM of the `bulk supercell`
                calculation by default, unless ``bulk_band_gap_vr`` is set during defect
                parsing).  ``None`` (default), set to the mid-gap Fermi level (E_g/2).
            skip_formatting (bool):
                Whether to skip formatting the defect charge states as
                strings (and keep as ``int``\s and ``float``\s instead).
                (default: False)

        Returns:
            ``pandas`` ``DataFrame`` or list of ``DataFrame``\s
        """
        fermi_level = self._get_and_set_fermi_level(fermi_level)
        chempots, el_refs = self._get_chempots(
            chempots, el_refs
        )  # returns self.chempots/self.el_refs if chempots is None

        if chempots is None:
            _no_chempots_warning()
            chempots = {  # empty sub-dicts so they're iterable (for following code)
                "limits": {"No User Chemical Potentials": {}},
                "limits_wrt_el_refs": {"No User Chemical Potentials": {}},
            }

        limit = _parse_limit(chempots, limit)
        limits = [limit] if limit is not None else list(chempots["limits"].keys())

        list_of_dfs = []
        with warnings.catch_warnings():  # avoid double warning, already warned above
            warnings.filterwarnings("ignore", "Chemical potentials not present")
            for limit in limits:
                limits_wrt_el_refs = chempots.get("limits_wrt_el_refs") or chempots.get(
                    "limits_wrt_elt_refs"
                )
                if limits_wrt_el_refs is None:
                    raise ValueError("Supplied chempots are not in a recognised format (see docstring)!")
                relative_chempots = limits_wrt_el_refs[limit]
                if el_refs is None:
                    el_refs = (
                        {el: 0 for el in relative_chempots}
                        if chempots.get("elemental_refs") is None
                        else chempots["elemental_refs"]
                    )

                single_formation_energy_df = self._single_formation_energy_table(
                    relative_chempots, el_refs, fermi_level, skip_formatting
                )
                list_of_dfs.append(single_formation_energy_df)

        return list_of_dfs[0] if len(list_of_dfs) == 1 else list_of_dfs

    def _single_formation_energy_table(
        self,
        relative_chempots: dict,
        el_refs: dict,
        fermi_level: float = 0,
        skip_formatting: bool = False,
    ) -> pd.DataFrame:
        r"""
        Returns a defect formation energy table for a single chemical potential
        limit as a pandas ``DataFrame``.

        Table Key: (all energies in eV):

        - 'Defect':
            Defect name (without charge)
        - 'q':
            Defect charge state.
        - 'ΔEʳᵃʷ':
            Raw DFT energy difference between defect and host supercell (E_defect - E_host).
        - 'qE_VBM':
            Defect charge times the VBM eigenvalue (to reference the Fermi level to the VBM)
        - 'qE_F':
            Defect charge times the Fermi level (referenced to the VBM if qE_VBM is not 0
            (if "vbm" in ``DefectEntry.calculation_metadata``)
        - 'Σμ_ref':
            Sum of reference energies of the elemental phases in the chemical potentials sum.
        - 'Σμ_formal':
            Sum of `formal` atomic chemical potential terms (Σμ_DFT = Σμ_ref + Σμ_formal).
        - 'E_corr':
            Finite-size supercell charge correction.
        - 'Eᶠᵒʳᵐ':
            Defect formation energy, with the specified chemical potentials and Fermi level.
            Equals the sum of all other terms.
        - 'Path':
            Path to the defect calculation folder.
        - 'Δ[E_corr]':
            Estimated error in the charge correction, from the variance of the potential in
            the sampling region.

        Args:
            relative_chempots (dict):
                Dictionary of formal (i.e. relative to elemental reference energies)
                chemical potentials in the form ``{element symbol: energy}``.
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format: ``{element symbol: reference energy}``
                (Default: None)
            fermi_level (float):
                Value corresponding to the electron chemical potential, referenced
                to the VBM eigenvalue, which is taken from the ``calculation_metadata``
                dict attributes of ``DefectEntry``\s in ``self.defect_entries`` if present,
                otherwise ``self.vbm`` -- which correspond to the VBM of the `bulk supercell`
                calculation by default, unless ``bulk_band_gap_vr`` is set during defect
                parsing). If ``None`` (default), set to the mid-gap Fermi level (E_g/2).
            skip_formatting (bool):
                Whether to skip formatting the defect charge states as
                strings (and keep as ``int``\s and ``float``\s instead).
                (default: False)

        Returns:
            ``pandas`` ``DataFrame`` sorted by formation energy
        """
        table = []

        for name, defect_entry in self.defect_entries.items():
            row = [
                name.rsplit("_", 1)[0],  # name without charge,
                (
                    defect_entry.charge_state
                    if skip_formatting
                    else f"{'+' if defect_entry.charge_state > 0 else ''}{defect_entry.charge_state}"
                ),
            ]
            row += [defect_entry.get_ediff() - sum(defect_entry.corrections.values())]
            row += [defect_entry.charge_state * defect_entry.calculation_metadata.get("vbm", self.vbm)]
            row += [defect_entry.charge_state * fermi_level]
            row += [defect_entry._get_chempot_term(el_refs) if any(el_refs.values()) else "N/A"]
            row += [defect_entry._get_chempot_term(relative_chempots)]
            row += [sum(defect_entry.corrections.values())]
            dft_chempots = {el: energy + el_refs[el] for el, energy in relative_chempots.items()}
            formation_energy = defect_entry.formation_energy(
                chempots=dft_chempots,
                fermi_level=fermi_level,
                vbm=defect_entry.calculation_metadata.get("vbm", self.vbm),
            )
            row += [formation_energy]
            row += [defect_entry.calculation_metadata.get("defect_path", "N/A")]
            row += [
                sum(
                    [
                        val
                        for key, val in defect_entry.corrections_metadata.items()
                        if "charge_correction_error" in key
                    ]
                )
            ]

            table.append(row)

        formation_energy_df = pd.DataFrame(
            table,
            columns=[
                "Defect",
                "q",
                "ΔEʳᵃʷ",
                "qE_VBM",
                "qE_F",
                "Σμ_ref",
                "Σμ_formal",
                "E_corr",
                "Eᶠᵒʳᵐ",
                "Path",
                "Δ[E_corr]",
            ],
        )

        # round all floats to 3dp:
        formation_energy_df = formation_energy_df.round(3)
        return formation_energy_df.set_index(["Defect", "q"])

    def get_symmetries_and_degeneracies(
        self,
        skip_formatting: bool = False,
        symprec: Optional[float] = None,
    ) -> pd.DataFrame:
        r"""
        Generates a table of the bulk-site & relaxed defect point group
        symmetries, spin/orientational/total degeneracies and (bulk-)site
        multiplicities for each defect in the ``DefectThermodynamics`` object.

        Table Key:

        - 'Defect': Defect name (without charge)
        - 'q': Defect charge state.
        - 'Site_Symm': Point group symmetry of the defect site in the bulk cell.
        - 'Defect_Symm': Point group symmetry of the relaxed defect.
        - 'g_Orient': Orientational degeneracy of the defect.
        - 'g_Spin': Spin degeneracy of the defect.
        - 'g_Total': Total degeneracy of the defect.
        - 'Mult': Multiplicity of the defect site in the bulk cell, per primitive unit cell.

        For interstitials, the bulk site symmetry corresponds to the
        point symmetry of the interstitial site with `no relaxation
        of the host structure`, while for vacancies/substitutions it is
        simply the symmetry of their corresponding bulk site.
        This corresponds to the point symmetry of ``DefectEntry.defect``,
        or ``calculation_metadata["bulk_site"]/["unrelaxed_defect_structure"]``.

        Point group symmetries are taken from the calculation_metadata
        ("relaxed point symmetry" and "bulk site symmetry") if
        present (should be, if parsed with doped and defect supercell
        doesn't break host periodicity), otherwise are attempted to be
        recalculated.

        Note: doped tries to use the defect_entry.defect_supercell to determine
        the `relaxed` site symmetry. However, it should be noted that this is not
        guaranteed to work in all cases; namely for non-diagonal supercell
        expansions, or sometimes for non-scalar supercell expansion matrices
        (e.g. a 2x1x2 expansion)(particularly with high-symmetry materials)
        which can mess up the periodicity of the cell. doped tries to automatically
        check if this is the case, and will warn you if so.

        This can also be checked by using this function on your doped `generated` defects:

        .. code-block:: python

            from doped.generation import get_defect_name_from_entry
            for defect_name, defect_entry in defect_gen.items():
                print(defect_name, get_defect_name_from_entry(defect_entry, relaxed=False),
                      get_defect_name_from_entry(defect_entry), "\n")

        And if the point symmetries match in each case, then doped should be able to
        correctly determine the final relaxed defect symmetry (and orientational degeneracy)
        - otherwise periodicity-breaking prevents this.

        If periodicity-breaking prevents auto-symmetry determination, you can manually
        determine the relaxed defect and bulk-site point symmetries, and/or orientational
        degeneracy, from visualising the structures (e.g. using VESTA)(can use
        ``get_orientational_degeneracy`` to obtain the corresponding orientational
        degeneracy factor for given defect/bulk-site point symmetries) and setting the
        corresponding values in the
        ``calculation_metadata['relaxed point symmetry']/['bulk site symmetry']`` and/or
        ``degeneracy_factors['orientational degeneracy']`` attributes.
        Note that the bulk-site point symmetry corresponds to that of ``DefectEntry.defect``,
        or equivalently ``calculation_metadata["bulk_site"]/["unrelaxed_defect_structure"]``,
        which for vacancies/substitutions is the symmetry of the corresponding bulk site,
        while for interstitials it is the point symmetry of the `final relaxed` interstitial
        site when placed in the (unrelaxed) bulk structure.
        The degeneracy factor is used in the calculation of defect/carrier concentrations
        and Fermi level behaviour (see e.g. https://doi.org/10.1039/D2FD00043A &
        https://doi.org/10.1039/D3CS00432E).

        Args:
            skip_formatting (bool):
                Whether to skip formatting the defect charge states as
                strings (and keep as ``int``\s and ``float``\s instead).
                (default: False)
            symprec (float):
                Symmetry tolerance for ``spglib`` to use when determining
                relaxed defect point symmetries and thus orientational
                degeneracies. Default is ``0.1`` which matches that used by
                the ``Materials Project`` and is larger than the ``pymatgen``
                default of ``0.01`` (which is used by ``doped`` for
                unrelaxed/bulk structures) to account for residual structural
                noise in relaxed defect supercells.
                You may want to adjust for your system (e.g. if there are
                very slight octahedral distortions etc.). If ``symprec`` is
                set, then the point symmetries and corresponding orientational
                degeneracy will be re-parsed/computed even if already present
                in the ``DefectEntry`` object ``calculation_metadata``.

        Returns:
            ``pandas`` ``DataFrame``
        """
        table_list = []

        for name, defect_entry in self.defect_entries.items():
            defect_entry._parse_and_set_degeneracies(symprec=symprec)
            try:
                multiplicity_per_unit_cell = defect_entry.defect.multiplicity * (
                    len(defect_entry.defect.structure.to_primitive())  # spglib primitive
                    / len(defect_entry.defect.structure)
                )

            except Exception:
                multiplicity_per_unit_cell = "N/A"

            total_degeneracy = (
                reduce(lambda x, y: x * y, defect_entry.degeneracy_factors.values())
                if defect_entry.degeneracy_factors
                else "N/A"
            )

            table_list.append(
                {
                    "Defect": name.rsplit("_", 1)[0],  # name without charge
                    "q": defect_entry.charge_state,
                    "Site_Symm": defect_entry.calculation_metadata.get("bulk site symmetry", "N/A"),
                    "Defect_Symm": defect_entry.calculation_metadata.get("relaxed point symmetry", "N/A"),
                    "g_Orient": defect_entry.degeneracy_factors.get("orientational degeneracy", "N/A"),
                    "g_Spin": defect_entry.degeneracy_factors.get("spin degeneracy", "N/A"),
                    "g_Total": total_degeneracy,
                    "Mult": multiplicity_per_unit_cell,
                }
            )

        if any(
            defect_entry.calculation_metadata.get("periodicity_breaking_supercell", False)
            for defect_entry in self.defect_entries.values()
        ):
            warnings.warn(_orientational_degeneracy_warning)

        symmetry_df = pd.DataFrame(table_list)

        if not skip_formatting:
            symmetry_df["q"] = symmetry_df["q"].apply(lambda x: f"{'+' if x > 0 else ''}{x}")

        return symmetry_df.set_index(["Defect", "q"])

    def __repr__(self):
        """
        Returns a string representation of the ``DefectThermodynamics`` object.
        """
        formula = _get_bulk_supercell(
            next(iter(self.defect_entries.values()))
        ).composition.get_reduced_formula_and_factor(iupac_ordering=True)[0]
        properties, methods = _doped_obj_properties_methods(self)
        return (
            f"doped DefectThermodynamics for bulk composition {formula} with {len(self.defect_entries)} "
            f"defect entries (in self.defect_entries). Available attributes:\n"
            f"{properties}\n\nAvailable methods:\n{methods}"
        )

    def __getattr__(self, attr):
        """
        Redirects an unknown attribute/method call to the ``defect_entries``
        dictionary attribute, if the attribute doesn't exist in
        ``DefectThermodynamics``.
        """
        try:
            super().__getattribute__(attr)
        except AttributeError as exc:
            if attr == "_defect_entries":
                raise exc
            return getattr(self._defect_entries, attr)

    def __getitem__(self, key):
        """
        Makes ``DefectThermodynamics`` object subscriptable, so that it can be
        indexed like a dictionary, using the ``defect_entries`` dictionary
        attribute.
        """
        return self.defect_entries[key]

    def __setitem__(self, key, value):
        """
        Set the value of a specific key (defect name) in the ``defect_entries``
        dictionary, using ``add_entries`` (to check compatibility).
        """
        self.add_entries(
            [
                value,
            ]
        )

    def __delitem__(self, key):
        """
        Deletes the specified defect entry from the ``defect_entries``
        dictionary.
        """
        del self.defect_entries[key]

    def __contains__(self, key):
        """
        Returns ``True`` if the ``defect_entries`` dictionary contains the
        specified defect name.
        """
        return key in self.defect_entries

    def __len__(self):
        """
        Returns the number of entries in the ``defect_entries`` dictionary.
        """
        return len(self.defect_entries)

    def __iter__(self):
        """
        Returns an iterator over the ``defect_entries`` dictionary.
        """
        return iter(self.defect_entries)


def _add_effective_dopant_concentration(
    conc_df: pd.DataFrame, effective_dopant_concentration: Optional[float] = None
):
    """
    Add the effective dopant concentration to the concentration ``DataFrame``.

    Args:
        conc_df (pd.DataFrame):
            DataFrame of defect concentrations.
        effective_dopant_concentration (float):
            The effective dopant concentration to add to the DataFrame.

    Returns:
        pd.DataFrame: DataFrame of defect concentrations with the effective
            dopant concentration
    """
    if effective_dopant_concentration is None:
        return conc_df

    eff_dopant_df = pd.DataFrame(
        {
            "Defect": "Effective Dopant",
            "Charge": np.sign(effective_dopant_concentration),
            "Formation Energy (eV)": "N/A",
            "Concentration (cm^-3)": np.abs(effective_dopant_concentration),
            "Charge State Population": "100.0%",
        },
        index=[0],
    )
    for col in conc_df.columns:
        if col not in eff_dopant_df.columns:
            eff_dopant_df[col] = "N/A"  # e.g. concentration per site, if per_site=True

    if "Charge" not in conc_df.columns:
        eff_dopant_df = eff_dopant_df.drop(
            columns=["Charge", "Formation Energy (eV)", "Charge State Population"]
        )
        eff_dopant_df = eff_dopant_df.set_index("Defect")  # Defect is index with summed conc df
        return pd.concat([conc_df, eff_dopant_df])

    return pd.concat([conc_df, eff_dopant_df], ignore_index=True)


def _group_defect_charge_state_concentrations(
    conc_df: pd.DataFrame, per_site: bool = False, skip_formatting: bool = False
):
    summed_df = conc_df.groupby("Defect").sum(numeric_only=True)
    raw_concentrations = (
        summed_df["Raw Concentration"]
        if "Raw Concentration" in summed_df.columns
        else summed_df[next(k for k in conc_df.columns if k.startswith("Concentration"))]
    )
    summed_df[next(k for k in conc_df.columns if k.startswith("Concentration"))] = (
        raw_concentrations.apply(
            lambda x: _format_concentration(x, per_site=per_site, skip_formatting=skip_formatting)
        )
    )
    return summed_df.drop(
        columns=[
            i
            for i in [
                "Charge",
                "Formation Energy (eV)",
                "Raw Charge",
                "Raw Concentration",
                "Charge State Population",
            ]
            if i in summed_df.columns
        ]
    )


def _format_concentration(raw_concentration: float, per_site: bool = False, skip_formatting: bool = False):
    """
    Format concentration values for ``DataFrame`` outputs.
    """
    if skip_formatting:
        return raw_concentration
    if per_site:
        return _format_per_site_concentration(raw_concentration)

    return f"{raw_concentration:.3e}"


def _format_per_site_concentration(raw_concentration: float):
    """
    Format per-site concentrations for ``DataFrame`` outputs.
    """
    if raw_concentration > 1e-5:
        return f"{raw_concentration:.3%}"
    return f"{raw_concentration * 100:.3e} %"


def get_fermi_dos(dos_vr: Union[PathLike, Vasprun]):
    """
    Create a ``FermiDos`` object from the provided ``dos_vr``, which can be
    either a path to a ``vasprun.xml(.gz)`` file, or a ``pymatgen`` ``Vasprun``
    object (parsed with ``parse_dos = True``).

    Args:
        dos_vr (Union[PathLike, Vasprun]):
            Path to a ``vasprun.xml(.gz)`` file, or a ``Vasprun`` object.

    Returns:
        FermiDos: The ``FermiDos`` object.
    """
    if not isinstance(dos_vr, Vasprun):
        dos_vr = get_vasprun(dos_vr, parse_dos=True)

    return FermiDos(dos_vr.complete_dos, nelecs=get_nelect_from_vasprun(dos_vr))


def get_e_h_concs(fermi_dos: FermiDos, fermi_level: float, temperature: float) -> tuple[float, float]:
    """
    Get the corresponding electron and hole concentrations (in cm^-3) for a
    given Fermi level (in eV) and temperature (in K), for a ``FermiDos``
    object.

    Note that the Fermi level here is NOT referenced to the VBM! So the Fermi
    level should be the corresponding eigenvalue within the calculation (or in
    other words, the Fermi level relative to the VBM plus the VBM eigenvalue).

    Args:
        fermi_dos (FermiDos):
            ``pymatgen`` ``FermiDos`` for the bulk electronic density of states (DOS),
            for calculating carrier concentrations.

            Usually this is a static calculation with the `primitive` cell of the bulk
            material, with relatively dense `k`-point sampling (especially for materials
            with disperse band edges) to ensure an accurately-converged DOS and thus Fermi
            level. ``ISMEAR = -5`` (tetrahedron smearing) is usually recommended for best
            convergence wrt `k`-point sampling. Consistent functional settings should be
            used for the bulk DOS and defect supercell calculations.
        fermi_level (float):
            Value corresponding to the electron chemical potential, **not** referenced
            to the VBM! (i.e. same eigenvalue reference as the raw calculation)
        temperature (float):
            Temperature in Kelvin at which to calculate the equilibrium concentrations.

    Returns:
        tuple[float, float]: The electron and hole concentrations in cm^-3.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "overflow")  # ignore overflow warnings from f0, can remove in
        # future versions following SK's fix in https://github.com/materialsproject/pymatgen/pull/3879
        # code for obtaining the electron and hole concentrations here is taken from
        # FermiDos.get_doping(), and updated by SK to be independent of estimated VBM/CBM positions (using
        # correct DOS integral) and better handle exponential overflows (by editing `f0` in `pymatgen`)
        idx_mid_gap = int(fermi_dos.idx_vbm + (fermi_dos.idx_cbm - fermi_dos.idx_vbm) / 2)
        e_conc: float = np.sum(
            fermi_dos.tdos[idx_mid_gap:]
            * f0(
                fermi_dos.energies[idx_mid_gap:],
                fermi_level,  # type: ignore
                temperature,
            )
            * fermi_dos.de[idx_mid_gap:],
            axis=0,
        ) / (fermi_dos.volume * fermi_dos.A_to_cm**3)
        h_conc: float = np.sum(
            fermi_dos.tdos[: idx_mid_gap + 1]
            * f0(
                -fermi_dos.energies[: idx_mid_gap + 1],
                -fermi_level,  # type: ignore
                temperature,
            )
            * fermi_dos.de[: idx_mid_gap + 1],
            axis=0,
        ) / (fermi_dos.volume * fermi_dos.A_to_cm**3)

    return e_conc, h_conc


def get_doping(fermi_dos: FermiDos, fermi_level: float, temperature: float) -> float:
    """
    Get the doping concentration (majority carrier - minority carrier
    concentration) in cm^-3 for a given Fermi level (in eV) and temperature
    (in K), for a ``FermiDos`` object.

    Note that the Fermi level here is NOT referenced to the VBM! So the Fermi
    level should be the corresponding eigenvalue within the calculation (or in
    other words, the Fermi level relative to the VBM plus the VBM eigenvalue).

    Refactored from ``FermiDos.get_doping()`` to be more accurate/robust
    (independent of estimated VBM/CBM positions, avoiding overflow warnings).

    Args:
        fermi_dos (FermiDos):
            ``pymatgen`` ``FermiDos`` for the bulk electronic density of states (DOS),
            for calculating carrier concentrations.

            Usually this is a static calculation with the `primitive` cell of the bulk
            material, with relatively dense `k`-point sampling (especially for materials
            with disperse band edges) to ensure an accurately-converged DOS and thus Fermi
            level. ``ISMEAR = -5`` (tetrahedron smearing) is usually recommended for best
            convergence wrt `k`-point sampling. Consistent functional settings should be
            used for the bulk DOS and defect supercell calculations.
        fermi_level (float):
            Value corresponding to the electron chemical potential, **not** referenced
            to the VBM! (i.e. same eigenvalue reference as the raw calculation)
        temperature (float):
            Temperature in Kelvin at which to calculate the equilibrium concentrations.

    Returns:
        tuple[float, float]: The electron and hole concentrations in cm^-3.
    """
    # can replace this function with the ``FermiDos.get_doping()`` method in future versions following SK's
    # fix in https://github.com/materialsproject/pymatgen/pull/3879, whenever pymatgen>2024.6.10 becomes
    # a ``doped`` requirement (same for overflow catches in ``get_e_h_concs`` etc)
    e_conc, h_conc = get_e_h_concs(fermi_dos, fermi_level, temperature)
    return h_conc - e_conc


def scissor_dos(delta_gap: float, dos: Union[Dos, FermiDos], tol: float = 1e-8, verbose: bool = True):
    """
    Given an input Dos/FermiDos object, rigidly shifts the valence and
    conduction bands of the DOS object to give a band gap that is now
    increased/decreased by ``delta_gap`` eV, where this rigid scissor shift is
    applied symmetrically around the original gap (i.e. the VBM is downshifted
    by ``delta_gap/2`` and the CBM is upshifted by ``delta_gap/2``).

    Note this assumes a non-spin-polarised (i.e. non-magnetic) density
    of states!

    Args:
        delta_gap (float):
            The amount by which to increase/decrease the band gap (in eV).
        dos (Dos/FermiDos):
            The input DOS object to scissor.
        tol (float):
            The tolerance to use for determining the VBM and CBM (used in
            ``Dos.get_gap(tol=tol)``). Default: 1e-8.
        verbose (bool):
            Whether to print information about the original and new band gaps.

    Returns:
        FermiDos: The scissored DOS object.
    """
    dos = deepcopy(dos)  # don't overwrite object
    # shift just CBM upwards first, then shift all rigidly down by Eg/2 (simpler code with this approach)
    cbm_index = np.where(
        (dos.densities[Spin.up] > tol) & (dos.energies - dos.efermi > dos.get_gap(tol=tol) / 2)
    )[0][0]
    cbm_energy = dos.energies[cbm_index]
    # get closest index with energy near cbm_energy + delta_gap:
    new_cbm_index = np.argmin(np.abs(dos.energies - (cbm_energy + delta_gap)))
    new_cbm_energy = dos.energies[new_cbm_index]
    vbm_index = np.where(
        (dos.densities[Spin.up] > tol) & (dos.energies - dos.efermi < dos.get_gap(tol=tol) / 2)
    )[0][-1]
    vbm_energy = dos.energies[vbm_index]

    if not np.isclose(cbm_energy - vbm_energy, dos.get_gap(tol=tol), atol=1e-1) and np.isclose(
        new_cbm_energy - cbm_energy, delta_gap, atol=1e-2
    ):
        warnings.warn(
            "The new band gap does not appear to be equal to the original band gap plus the scissor "
            "shift, suggesting an error in `scissor_dos`, beware!\n"
            f"Got original gap (from manual indexing): {cbm_energy - vbm_energy}, {dos.get_gap(tol=tol)} "
            f"from dos.get_gap(tol=tol) and new gap: {dos.get_gap(tol=tol) + delta_gap}"
        )

    # Determine the number of values in energies/densities to remove/add to avoid duplication
    values_to_remove_or_add = int(new_cbm_index - cbm_index)
    scissored_dos_dict = dos.as_dict()

    # Shift the CBM and remove/add values:
    if values_to_remove_or_add < 0:  # remove values
        scissored_dos_dict["energies"] = np.concatenate(
            (dos.energies[: cbm_index + values_to_remove_or_add], dos.energies[cbm_index:] + delta_gap)
        )
        scissored_dos_dict["densities"][Spin.up] = np.concatenate(
            (
                dos.densities[Spin.up][: cbm_index + values_to_remove_or_add],
                dos.densities[Spin.up][cbm_index:],
            )
        )
        # Assuming non-spin-polarised bulk here:
        scissored_dos_dict["densities"][Spin.down] = scissored_dos_dict["densities"][Spin.up]
    elif values_to_remove_or_add > 0:
        # add more zero DOS values:
        scissored_dos_dict["energies"] = np.concatenate(
            (
                dos.energies[:cbm_index],
                dos.energies[cbm_index:new_cbm_index],
                dos.energies[cbm_index:] + delta_gap,
            )
        )
        scissored_dos_dict["densities"][Spin.up] = np.concatenate(
            (
                dos.densities[Spin.up][:cbm_index],
                np.zeros(values_to_remove_or_add),
                dos.densities[Spin.up][cbm_index:],
            )
        )
        scissored_dos_dict["densities"][Spin.down] = scissored_dos_dict["densities"][Spin.up]

    # now shift all energies rigidly, so we've shifted symmetrically around the original gap (eigenvalues)
    # ensure 'energies' is array (should be if function used correctly, not if band gap change is zero...):
    scissored_dos_dict["energies"] = np.array(scissored_dos_dict["energies"])
    scissored_dos_dict["energies"] -= np.float64(delta_gap / 2)
    scissored_dos_dict["efermi"] -= np.float64(delta_gap / 2)

    if verbose:
        print(f"Orig gap: {dos.get_gap(tol=tol)}, new gap:{dos.get_gap(tol=tol) + delta_gap}")
    scissored_dos_dict["structure"] = dos.structure.as_dict()
    if isinstance(dos, FermiDos):
        return FermiDos.from_dict(scissored_dos_dict)
    return Dos.from_dict(scissored_dos_dict)


def _get_label_and_charge(name: str) -> tuple[str, int]:
    """
    Extracts the label and charge from a defect name string.

    Args:
        name (str): Name of the defect.

    Returns:
        tuple: A tuple containing the label and charge.
    """
    last_underscore = name.rfind("_")
    label = name[:last_underscore] if last_underscore != -1 else name
    charge_str = name[last_underscore + 1 :] if last_underscore != -1 else None

    charge = 0  # Initialize charge with a default value
    if charge_str is not None:
        with contextlib.suppress(ValueError):
            charge = int(charge_str)

    return label, charge


# TODO: Update DefectThermodynamics docstrings after `py-sc-fermi` interface written, to point toward it
#  for more advanced analysis


def get_py_sc_fermi_dos_from_fermi_dos(
    fermi_dos: FermiDos,
    vbm: Optional[float] = None,
    nelect: Optional[int] = None,
    bandgap: Optional[float] = None,
) -> "DOS":
    """
    Given an input ``pymatgen`` ``FermiDos`` object, return a corresponding
    ``py-sc-fermi`` ``DOS`` object (which can then be used with the ``py-sc-
    fermi`` ``FermiSolver`` backend).

    Args:
        fermi_dos (FermiDos):
            ``pymatgen`` ``FermiDos`` object to convert to ``py-sc-fermi``
            ``DOS``.
        vbm (float):
            The valence band maximum (VBM) eigenvalue in eV. If not provided,
            the VBM will be taken from the FermiDos object. When this function
            is used internally in ``doped``, the ``DefectThermodynamics.vbm``
            attribute is used.
        nelect (int):
            The total number of electrons in the system. If not provided, the
            number of electrons will be taken from the FermiDos object (which
            usually takes this value from the ``vasprun.xml(.gz)`` when parsing).
        bandgap (float):
            Band gap of the system in eV. If not provided, the band gap will be
            taken from the FermiDos object. When this function is used internally
            in ``doped``, the ``DefectThermodynamics.band_gap`` attribute is used.

    Returns:
        DOS: A ``py-sc-fermi`` ``DOS`` object.
    """
    try:
        from py_sc_fermi.dos import DOS
    except ImportError as exc:
        raise ImportError("py-sc-fermi must be installed to use this function!") from exc

    densities = fermi_dos.densities
    if vbm is None:  # tol 1e-4 is lowest possible, as VASP rounds to 4 dp:
        vbm = fermi_dos.get_cbm_vbm(tol=1e-4, abs_tol=True)[1]

    edos = fermi_dos.energies - vbm
    if len(densities) == 2:
        dos = np.array([densities[Spin.up], densities[Spin.down]])
        spin_pol = True
    else:
        dos = np.array(densities[Spin.up])
        spin_pol = False

    if nelect is None:
        # this requires the input dos to be a FermiDos. NELECT could be calculated alternatively
        # by integrating the tdos of a ``pymatgen`` ``Dos`` object, but this isn't expected to be
        # a common use case and using parsed NELECT from vasprun.xml(.gz) is more reliable
        nelect = fermi_dos.nelecs
    if bandgap is None:
        bandgap = fermi_dos.get_gap(tol=1e-4, abs_tol=True)

    return DOS(dos=dos, edos=edos, nelect=nelect, bandgap=bandgap, spin_polarised=spin_pol)


class FermiSolver(MSONable):
    def __init__(
        self,
        defect_thermodynamics: "DefectThermodynamics",
        bulk_dos: Optional[Union[FermiDos, Vasprun, "PathLike"]] = None,
        chempots: Optional[dict] = None,
        el_refs: Optional[dict] = None,
        backend: str = "doped",
        skip_check: bool = False,
    ):
        r"""
        Class to calculate the Fermi level, defect and carrier concentrations
        under various conditions, using the input ``DefectThermodynamics``
        object.

        This constructor initializes a ``FermiSolver`` object, setting up the
        necessary attributes, which includes loading the bulk density of states
        (DOS) data from the provided VASP output.

        If using the ``py-sc-fermi`` backend (currently required for the
        ``free_defects`` and ``fix_charge_states`` options in the scanning
        functions), please cite the code paper:
        Squires et al., (2023). Journal of Open Source Software, 8(82), 4962
        https://doi.org/10.21105/joss.04962

        Args:
            defect_thermodynamics (DefectThermodynamics):
                A ``DefectThermodynamics`` object, providing access to defect
                formation energies and other related thermodynamic properties.
            bulk_dos (FermiDos or Vasprun or PathLike):
                Either a path to the ``vasprun.xml(.gz)`` output of a bulk DOS
                calculation in VASP, a ``pymatgen`` ``Vasprun`` object or a
                ``pymatgen`` ``FermiDos`` for the bulk electronic DOS, for
                calculating carrier concentrations.
                If not provided, uses ``DefectThermodynamics.bulk_dos`` if present.

                Usually this is a static calculation with the `primitive` cell of
                the bulk material, with relatively dense `k`-point sampling
                (especially for materials with disperse band edges) to ensure an
                accurately-converged DOS and thus Fermi level. ``ISMEAR = -5``
                (tetrahedron smearing) is usually recommended for best convergence
                wrt `k`-point sampling. Consistent functional settings should be
                used for the bulk DOS and defect supercell calculations.

                Note that the ``DefectThermodynamics.bulk_dos`` will be set to match
                this input, if provided.
            chempots (Optional[dict]):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations and Fermi level), under
                different chemical environments.

                If ``None`` (default), will use ``DefectThermodynamics.chempots``
                (= 0 for all chemical potentials by default, if not set).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}], ...}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (limit), in the format: ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                If provided here, then ``defect_thermodynamics.chempots`` is set to this
                input.

                Chemical potentials can also be supplied later in each analysis function, or
                set using ``FermiSolver.defect_thermodynamics.chempots = ...`` or
                ``DefectThermodynamics.chempots = ...`` (with the same input options).
            el_refs (Optional[dict]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in ``DefectThermodynamics`` in the format generated by
                ``doped`` (see tutorials), or if ``DefectThermodynamics.el_refs`` has been
                set.

                If provided here, then ``defect_thermodynamics.el_refs`` is set to this
                input.

                Elemental reference energies can also be supplied later in each analysis
                function, or set using ``FermiSolver.defect_thermodynamics.el_refs = ...``
                or ``DefectThermodynamics.el_refs = ...`` (with the same input options).
                Default is ``None``.
            backend (Optional[str]):
                The code backend to use for the thermodynamic calculations, which
                can be either ``"doped"`` or ``"py-sc-fermi"``. ``"py-sc-fermi"``
                allows the use of ``free_defects`` for advanced constrained defect
                equilibria (i.e. mobile defects, see advanced thermodynamics tutorial),
                while ``"doped"`` is usually (but not always) quicker. Default is
                ``doped``, but will attempt to switch to ``py-sc-fermi`` if required
                (and installed).
            skip_check (bool):
                Whether to skip the warning about the DOS VBM differing from
                ``DefectThermodynamics.vbm`` by >0.05 eV. Should only be used when the
                reason for this difference is known/acceptable. Default is ``False``.

        Key attributes:
            defect_thermodynamics (DefectThermodynamics):
                The ``DefectThermodynamics`` object used for the thermodynamic
                calculations.
            backend (str):
                The code backend used for the thermodynamic calculations (``"doped"``
                or ``"py-sc-fermi"``).
            volume (float):
                Volume of the unit cell in the bulk DOS calculation (stored in
                ``self.defect_thermodynamics.bulk_dos``).
            skip_check (bool):
                Whether to skip the warning about the DOS VBM differing from the defect
                entries VBM by >0.05 eV. Should only be used when the reason for this
                difference is known/acceptable.
            multiplicity_scaling (int):
                Scaling factor to account for the difference in volume between the
                defect supercells and the bulk DOS calculation cell, when using the
                ``py-sc-fermi`` backend.
            py_sc_fermi_dos (DOS):
                A ``py-sc-fermi`` ``DOS`` object, generated from the input
                ``FermiDos`` object, for use with the ``py-sc-fermi`` backend.
        """
        # Note: In theory multiprocessing could be introduced to make thermodynamic calculations
        # over large grids faster, but with the current code there seems to be issues with thread
        # locking / synchronisation (which shouldn't be necessary...). Worth keeping in mind if
        # needed in future.
        self.defect_thermodynamics = defect_thermodynamics
        self.skip_check = skip_check
        if bulk_dos is not None:
            self.defect_thermodynamics.bulk_dos = self.defect_thermodynamics._parse_fermi_dos(
                bulk_dos, skip_check=self.skip_check
            )
        if self.defect_thermodynamics.bulk_dos is None:
            raise ValueError(
                "No bulk DOS calculation (`bulk_dos`) provided or previously parsed to "
                "`DefectThermodynamics.bulk_dos`, which is required for calculating carrier "
                "concentrations and solving for Fermi level position."
            )
        self.volume = self.defect_thermodynamics.bulk_dos.volume

        if "fermi" in backend.lower():
            if bool(importlib.util.find_spec("py_sc_fermi")):
                self.backend = "py-sc-fermi"
            else:  # py-sc-fermi explicitly chosen but not installed
                raise ImportError(
                    "py-sc-fermi is not installed, so only the doped FermiSolver backend is available."
                )
        elif backend.lower() == "doped":
            self.backend = "doped"
        else:
            raise ValueError(f"Unrecognised `backend`: {backend}")

        self.multiplicity_scaling = 1
        self.py_sc_fermi_dos = None
        self._DefectSystem = self._DefectSpecies = self._DefectChargeState = self._DOS = None

        if self.backend == "py-sc-fermi":
            self._activate_py_sc_fermi_backend()

        # Parse chemical potentials, either using input values (after formatting them in the doped format)
        # or using the class attributes if set:
        self.defect_thermodynamics._chempots, self.defect_thermodynamics._el_refs = _parse_chempots(
            chempots or self.defect_thermodynamics.chempots, el_refs or self.defect_thermodynamics.el_refs
        )

        if self.defect_thermodynamics.chempots is None:
            raise ValueError(
                "You must supply a chemical potentials dictionary or have them present in the "
                "DefectThermodynamics object."
            )

    def _activate_py_sc_fermi_backend(self, error_message: Optional[str] = None):
        try:
            from py_sc_fermi.defect_charge_state import DefectChargeState
            from py_sc_fermi.defect_species import DefectSpecies
            from py_sc_fermi.defect_system import DefectSystem
            from py_sc_fermi.dos import DOS
        except ImportError as exc:  # py-sc-fermi activation attempted but not installed
            finishing_message = (
                ", but py-sc-fermi is not installed, so only the doped FermiSolver "
                "backend is available!"
            )
            message = (
                error_message + finishing_message
                if error_message
                else "The py-sc-fermi backend was attempted to be activated" + finishing_message
            )
            raise ImportError(message) from exc

        self._DefectSystem = DefectSystem
        self._DefectSpecies = DefectSpecies
        self._DefectChargeState = DefectChargeState
        self._DOS = DOS

        if isinstance(self.defect_thermodynamics.bulk_dos, FermiDos):
            self.py_sc_fermi_dos = get_py_sc_fermi_dos_from_fermi_dos(
                self.defect_thermodynamics.bulk_dos,
                vbm=self.defect_thermodynamics.vbm,
                bandgap=self.defect_thermodynamics.band_gap,
            )

        ms = (
            next(iter(self.defect_thermodynamics.defect_entries.values())).sc_entry.structure.volume
            / self.volume
        )
        if not np.isclose(ms, round(ms), atol=3e-2):  # check multiplicity scaling is almost an integer
            warnings.warn(
                f"The detected volume ratio ({ms:.3f}) between the defect supercells and the DOS "
                f"calculation cell is non-integer, indicating that they correspond to (slightly) "
                f"different unit cell volumes. This can cause quantitative errors of a similar "
                f"relative magnitude in the predicted defect/carrier concentrations!"
            )
        self.multiplicity_scaling = round(ms)

    def _check_required_backend_and_error(self, required_backend: str):
        """
        Check if the attributes needed for the ``required_backend`` backend are
        set, and throw an error message if not.

        Args:
            required_backend (str):
                Backend choice ("doped" or "py-sc-fermi") required.
        """
        raise_error = False
        if required_backend.lower() == "doped":
            if not isinstance(self.defect_thermodynamics.bulk_dos, FermiDos):
                raise_error = True
        elif self._DOS is None:
            raise_error = True

        if raise_error:
            raise RuntimeError(
                f"This function is only supported for the {required_backend} backend, but you are "
                f"using the {self.backend} backend!"
            )

    def _get_fermi_level_and_carriers(
        self,
        single_chempot_dict: dict[str, float],
        el_refs: Optional[dict[str, float]] = None,
        temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
    ) -> tuple[float, float, float]:
        """
        Calculate the equilibrium Fermi level and carrier concentrations under
        a given chemical potential regime and temperature.

        Args:
            single_chempot_dict (dict[str, float]):
                Dictionary of chemical potentials to use for calculating the equilibrium
                Fermi level position. Here, this should be a dictionary of chemical
                potentials for a single limit (``limit``), in the format:
                ``{element symbol: chemical potential}``.
                If ``el_refs`` is provided or set in ``self.defect_thermodynamics.el_refs``
                then it is the formal chemical potentials (i.e. relative to the elemental
                reference energies) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
            el_refs (dict[str, float]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}``. Unnecessary if
                ``self.defect_thermodynamics.el_refs`` is set (i.e. if ``chempots`` was
                provided to ``self.defect_thermodynamics`` in the format generated by
                ``doped``).
                (Default: None)
            temperature (float):
                The temperature at which to solve for the Fermi level
                and carrier concentrations, in Kelvin.
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.

        Returns:
            tuple[float, float, float]: A tuple containing:
                - The Fermi level (float) in eV.
                - The electron concentration (float) in cm^-3.
                - The hole concentration (float) in cm^-3.
        """
        self._check_required_backend_and_error("doped")
        fermi_level, electrons, holes = self.defect_thermodynamics.get_equilibrium_fermi_level(  # type: ignore
            chempots=single_chempot_dict,
            el_refs=el_refs,
            temperature=temperature,
            return_concs=True,
            effective_dopant_concentration=effective_dopant_concentration,
        )  # use already-set bulk dos
        return fermi_level, electrons, holes

    def _get_single_chempot_dict(
        self, limit: Optional[str] = None, chempots: Optional[dict] = None, el_refs: Optional[dict] = None
    ) -> tuple[dict[str, float], Any]:
        """
        Get the chemical potentials for a single limit (``limit``) from the
        ``chempots`` (or ``self.defect_thermodynamics.chempots``) dictionary.

        Returns a `single` chemical potential dictionary for the specified limit.
        """
        chempots, el_refs = self.defect_thermodynamics._get_chempots(
            chempots, el_refs
        )  # returns self.defect_thermodynamics.chempots if chempots is None
        if chempots is None:
            raise ValueError(
                "No chemical potentials supplied or present in self.defect_thermodynamics.chempots!"
            )
        limit = _parse_limit(chempots, limit)

        if (
            limit not in chempots["limits_wrt_el_refs"]
            and "User Chemical Potentials" not in chempots["limits_wrt_el_refs"]
        ):
            raise ValueError(
                f"Limit '{limit}' not found in the chemical potentials dictionary! You must specify "
                f"an appropriate limit or provide an appropriate chempots dictionary."
            )

        return chempots["limits_wrt_el_refs"][limit or "User Chemical Potentials"], el_refs

    def equilibrium_solve(
        self,
        single_chempot_dict: dict[str, float],
        el_refs: Optional[dict[str, float]] = None,
        temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
        append_chempots: bool = True,
        fixed_defects: Optional[dict[str, float]] = None,
    ) -> pd.DataFrame:
        """
        Calculate the Fermi level and defect/carrier concentrations under
        `thermodynamic equilibrium`, given a set of chemical potentials and a
        temperature.

        Typically not intended for direct usage, as the same functionality
        is provided by
        ``DefectThermodynamics.get_equilibrium_concentrations/fermi_level()``.
        Rather this is used internally to provide a unified interface for both
        backends, within the ``scan_...`` functions.

        Args:
            single_chempot_dict (dict[str, float]):
                Dictionary of chemical potentials to use for calculating the equilibrium
                Fermi level position and defect/carrier concentrations. Here, this
                should be a dictionary of chemical potentials for a single limit
                (``limit``), in the format: ``{element symbol: chemical potential}``.
                If ``el_refs`` is provided or set in ``self.defect_thermodynamics.el_refs``
                then it is the formal chemical potentials (i.e. relative to the elemental
                reference energies) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
            el_refs (dict[str, float]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}``. Unnecessary if
                ``self.defect_thermodynamics.el_refs`` is set (i.e. if ``chempots`` was
                provided to ``self.defect_thermodynamics`` in the format generated by
                ``doped``).
                (Default: None)
            temperature (float):
                The temperature at which to solve for defect concentrations, in Kelvin.
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            append_chempots (bool):
                Whether to append the chemical potentials (and effective dopant
                concentration, if provided) to the results ``DataFrame``.
                Default is ``True``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.

        Returns:
        # TODO: Check this output format matches for both backends!
            pd.DataFrame:
                A DataFrame containing the defect and carrier concentrations, as well
                as the self-consistent Fermi level and additional properties such as
                electron and hole concentrations. The columns include:
                    - "Fermi Level": The self-consistent Fermi level in eV.
                    - "Electrons (cm^-3)": The electron concentration.
                    - "Holes (cm^-3)": The hole concentration.
                    - "Temperature": The temperature at which the calculation was performed.
                    - "Dopant (cm^-3)": The dopant concentration, if applicable.
                    - "Defect": The defect type.
                    - "Concentration (cm^-3)": The concentration of the defect in cm^-3.
                Additional columns may include concentrations for specific defects and carriers.

        Returns:
                    pd.DataFrame: A DataFrame containing the calculated defect and carrier concentrations,
                    along with the self-consistent Fermi level. The DataFrame also includes the provided
                    chemical potentials as additional columns.
        """
        py_sc_fermi_required = fixed_defects is not None
        if py_sc_fermi_required and self._DOS is None:
            self._activate_py_sc_fermi_backend(
                error_message="The `fix_charge_states` and `free_defects` options are only supported "
                "for the py-sc-fermi backend"
            )

        if self.backend == "doped" and not py_sc_fermi_required:
            fermi_level, electrons, holes = self._get_fermi_level_and_carriers(
                single_chempot_dict=single_chempot_dict,
                el_refs=el_refs,
                temperature=temperature,
                effective_dopant_concentration=effective_dopant_concentration,
            )
            concentrations = self.defect_thermodynamics.get_equilibrium_concentrations(
                chempots=single_chempot_dict,
                el_refs=el_refs,
                fermi_level=fermi_level,
                temperature=temperature,
                per_charge=False,
                per_site=False,
                skip_formatting=False,
            )
            concentrations = _add_effective_dopant_concentration(
                concentrations, effective_dopant_concentration
            )
            new_columns = {
                "Fermi Level": fermi_level,
                "Electrons (cm^-3)": electrons,
                "Holes (cm^-3)": holes,
                "Temperature": temperature,
            }
            for column, value in new_columns.items():
                concentrations[column] = value
            excluded_columns = ["Defect", "Charge", "Charge State Population"]
            for column in concentrations.columns.difference(excluded_columns):
                concentrations[column] = concentrations[column].astype(float)

        else:  # py-sc-fermi backend:
            defect_system = self._generate_defect_system(
                single_chempot_dict=single_chempot_dict,
                el_refs=el_refs,
                temperature=temperature,
                effective_dopant_concentration=effective_dopant_concentration,
                fixed_defects=fixed_defects,
            )

            with np.errstate(all="ignore"):
                conc_dict = defect_system.concentration_dict()
            data = []

            for k, v in conc_dict.items():
                if k not in ["Fermi Energy", "n0", "p0", "Dopant"]:
                    row = {
                        "Temperature": defect_system.temperature,
                        "Fermi Level": conc_dict["Fermi Energy"],
                        "Holes (cm^-3)": conc_dict["p0"],
                        "Electrons (cm^-3)": conc_dict["n0"],
                    }
                    if "Dopant" in conc_dict:
                        row["Dopant (cm^-3)"] = conc_dict["Dopant"]
                    row.update({"Defect": k, "Concentration (cm^-3)": v})
                    data.append(row)

            concentrations = pd.DataFrame(data)
            concentrations = concentrations.set_index("Defect", drop=True)

        if append_chempots:
            for key, value in single_chempot_dict.items():
                concentrations[f"μ_{key}"] = value
            if effective_dopant_concentration:
                concentrations["Dopant (cm^-3)"] = effective_dopant_concentration

        return concentrations

    def pseudo_equilibrium_solve(
        self,
        annealing_temperature: float,
        single_chempot_dict: dict[str, float],
        el_refs: Optional[dict[str, float]] = None,
        quenched_temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
        append_chempots: bool = True,
    ) -> pd.DataFrame:
        """
        Calculate the self-consistent Fermi level and corresponding
        carrier/defect calculations under pseudo-equilibrium conditions given a
        set of elemental chemical potentials, a quenched temperature, and an
        annealing temperature.

        Typically not intended for direct usage, as the same functionality
        is provided by
        ``DefectThermodynamics.get_quenched_fermi_level_and_concentrations()``.
        Rather this is used internally to provide a unified interface for both
        backends, within the ``scan_...`` functions.

        'Pseudo-equilibrium' here refers to the use of frozen defect and dilute
        limit approximations under the constraint of charge neutrality (i.e.
        constrained equilibrium). According to the 'frozen defect' approximation,
        we typically expect defect concentrations to reach equilibrium during
        annealing/crystal growth (at elevated temperatures), but `not` upon
        quenching (i.e. at room/operating temperature) where we expect kinetic
        inhibition of defect annhiliation and hence non-equilibrium defect
        concentrations / Fermi level.
        Typically this is approximated by computing the equilibrium Fermi level and
        defect concentrations at the annealing temperature, and then assuming the
        total concentration of each defect is fixed to this value, but that the
        relative populations of defect charge states (and the Fermi level) can
        re-equilibrate at the lower (room) temperature. See discussion in
        https://doi.org/10.1039/D3CS00432E (brief),
        https://doi.org/10.1016/j.cpc.2019.06.017 (detailed) and
        ``doped``/``py-sc-fermi`` tutorials for more information.
        In certain cases (such as Li-ion battery materials or extremely slow charge
        capture/emission), these approximations may have to be adjusted such that some
        defects/charge states are considered fixed and some are allowed to
        re-equilibrate (e.g. highly mobile Li vacancies/interstitials). Modelling
        these specific cases can be achieved using the ``free_defects`` and/or
        ``fix_charge_states`` options, as demonstrated in:
        https://doped.readthedocs.io/en/latest/fermisolver_tutorial.html

        This function works by calculating the self-consistent Fermi level and total
        concentration of each defect at the annealing temperature, then fixing the
        total concentrations to these values and re-calculating the self-consistent
        (constrained equilibrium) Fermi level and relative charge state concentrations
        under this constraint at the quenched/operating temperature. If using the
        ``"py-sc-fermi"`` backend, then you can optionally fix the concentrations of
        individual defect `charge states` (rather than fixing total defect concentrations)
        using ``fix_charge_states=True``, and/or optionally specify ``free_defects`` to
        exclude from high-temperature concentration fixing (e.g. highly mobile defects).

        Note that the bulk DOS calculation should be well-converged with respect to
        k-points for accurate Fermi level predictions!

        The degeneracy/multiplicity factor "g" is an important parameter in the defect
        concentration equation and thus Fermi level calculation (see discussion in
        https://doi.org/10.1039/D2FD00043A and https://doi.org/10.1039/D3CS00432E),
        affecting the final concentration by up to 2 orders of magnitude. This factor
        is taken from the product of the ``defect_entry.defect.multiplicity`` and
        ``defect_entry.degeneracy_factors`` attributes.

        Args:
            annealing_temperature (float):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to the
                highest temperature during annealing/synthesis of the material (at
                which we assume equilibrium defect concentrations) within the frozen
                defect approach.
            single_chempot_dict (dict[str, float]):
                Dictionary of chemical potentials to use for calculating the
                pseudo-equilibrium Fermi level position and defect/carrier
                concentrations. Here, this should be a dictionary of chemical
                potentials for a single limit (``limit``), in the format:
                ``{element symbol: chemical potential}``.
                If ``el_refs`` is provided or set in ``self.defect_thermodynamics.el_refs``
                then it is the formal chemical potentials (i.e. relative to the elemental
                reference energies) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
            el_refs (dict[str, float]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}``. Unnecessary if
                ``self.defect_thermodynamics.el_refs`` is set (i.e. if ``chempots`` was
                provided to ``self.defect_thermodynamics`` in the format generated by
                ``doped``).
                (Default: None)
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.
                # TODO: Allow matching of substring
            append_chempots (bool):
                Whether to append the chemical potentials (and effective dopant
                concentration, if provided) to the results ``DataFrame``.
                Default is ``True``.

        Returns: # TODO: Check output format for both backends!
            pd.DataFrame:
                A ``DataFrame`` containing the defect and carrier concentrations
                under pseudo-equilibrium conditions, along with the self-consistent
                Fermi level.
                The columns include:
                    - "Fermi Level": The self-consistent Fermi level.
                    - "Electrons (cm^-3)": The electron concentration.
                    - "Holes (cm^-3)": The hole concentration.
                    - "Annealing Temperature": The annealing temperature.
                    - "Quenched Temperature": The quenched temperature.
                    - "Dopant (cm^-3)": The dopant concentration, if applicable.
                    - "Defect": The defect type.
                    - "Concentration (cm^-3)": The concentration of the defect in cm^-3.

                Additional columns may include concentrations for specific defects
                and other relevant data.

        Returns:
            pd.DataFrame: A DataFrame containing the calculated defect and carrier concentrations
            using the pseudo-equilibrium approach, along with the provided chemical potentials
            as additional columns.
        """
        py_sc_fermi_required = fix_charge_states or free_defects or fixed_defects is not None
        if py_sc_fermi_required and self._DOS is None:
            self._activate_py_sc_fermi_backend(
                error_message="The `fix_charge_states` and `free_defects` options are only supported "
                "for the py-sc-fermi backend"
            )

        if self.backend == "doped" and not py_sc_fermi_required:
            (
                fermi_level,
                electrons,
                holes,
                concentrations,
            ) = self.defect_thermodynamics.get_quenched_fermi_level_and_concentrations(  # type: ignore
                chempots=single_chempot_dict,
                el_refs=el_refs,
                annealing_temperature=annealing_temperature,
                quenched_temperature=quenched_temperature,
                effective_dopant_concentration=effective_dopant_concentration,
            )  # use already-set bulk dos
            concentrations = concentrations.drop(
                columns=[
                    "Charge",
                    "Charge State Population",
                    "Concentration (cm^-3)",
                    "Formation Energy (eV)",
                ],
            )

            new_columns = {
                "Fermi Level": fermi_level,
                "Electrons (cm^-3)": electrons,
                "Holes (cm^-3)": holes,
                "Annealing Temperature": annealing_temperature,
                "Quenched Temperature": quenched_temperature,
            }

            for column, value in new_columns.items():
                concentrations[column] = value

            trimmed_concentrations_sub_duplicates = concentrations.drop_duplicates()
            excluded_columns = ["Defect"]
            for column in concentrations.columns.difference(excluded_columns):
                concentrations[column] = concentrations[column].astype(float)

            concentrations = trimmed_concentrations_sub_duplicates.rename(
                columns={"Total Concentration (cm^-3)": "Concentration (cm^-3)"},
            )
            concentrations = concentrations.set_index("Defect", drop=True)

        else:  # py-sc-fermi
            defect_system = self._generate_annealed_defect_system(
                annealing_temperature=annealing_temperature,
                single_chempot_dict=single_chempot_dict,
                el_refs=el_refs,
                quenched_temperature=quenched_temperature,
                effective_dopant_concentration=effective_dopant_concentration,
                free_defects=free_defects,
                fixed_defects=fixed_defects,
                fix_charge_states=fix_charge_states,
            )

            with np.errstate(all="ignore"):
                conc_dict = defect_system.concentration_dict()

            data = []
            for k, v in conc_dict.items():
                if k not in ["Fermi Energy", "n0", "p0"]:
                    row = {
                        "Annealing Temperature": annealing_temperature,
                        "Quenched Temperature": quenched_temperature,
                        "Fermi Level": conc_dict["Fermi Energy"],
                        "Holes (cm^-3)": conc_dict["p0"],
                        "Electrons (cm^-3)": conc_dict["n0"],
                    }
                    row.update({"Defect": k, "Concentration (cm^-3)": v})
                    if "Dopant" in conc_dict:
                        row["Dopant (cm^-3)"] = conc_dict["Dopant"]
                    row.update({"Defect": k, "Concentration (cm^-3)": v})
                    data.append(row)

            concentrations = pd.DataFrame(data)
            concentrations = concentrations.set_index("Defect", drop=True)

        if append_chempots:
            for key, value in single_chempot_dict.items():
                concentrations[f"μ_{key}"] = value
            if effective_dopant_concentration:
                concentrations["Dopant (cm^-3)"] = effective_dopant_concentration

        return concentrations

    def scan_temperature(
        self,
        annealing_temperature_range: Optional[Union[float, list[float]]] = None,
        quenched_temperature_range: Union[float, list[float]] = 300,
        temperature_range: Union[float, list[float]] = 300,
        chempots: Optional[dict[str, float]] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict[str, float]] = None,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        r"""
        Scan over a range of temperatures and solve for the defect
        concentrations, carrier concentrations, and Fermi level at each
        temperature / annealing & quenched temperature pair.

        If ``annealing_temperature_range`` (and ``quenched_temperature_range``;
        just 300 K by default) are specified, then the frozen defect approximation
        is employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature_range`` is
        specified, then the Fermi level and defect/carrier concentrations are
        calculated assuming thermodynamic equilibrium at each temperature.

        Args:
            annealing_temperature_range (Optional[Union[float, list[float]]]):
                Temperature range in Kelvin at which to calculate the high
                temperature (fixed) total defect concentrations, which should
                correspond to the highest temperature during annealing/synthesis
                of the material (at which we assume equilibrium defect
                concentrations) within the frozen defect approach. Default is ``None``
                (uses ``temperature_range`` under thermodynamic equilibrium).
            quenched_temperature_range (Union[float, list[float]]):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is just 300 K.
            temperature_range (Union[float, list[float]]):
                Temperature range to solve over, under thermodynamic equilibrium
                (if ``annealing_temperature_range`` is not specified).
                Defaults to just 300 K.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations). If ``None`` (default),
                will use ``self.defect_thermodynamics.chempots`` (= 0 for all
                chemical potentials by default).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}], ...}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (``limit``), in the format:
                ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``FermiSolver.defect_thermodynamics.chempots = ...``
                or ``DefectThermodynamics.chempots = ...`` (with the same input options)
                to set the default chemical potentials for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to calculate the Fermi level
                positions and defect/carrier concentrations. Can be either:

                - ``None``, if ``chempots`` corresponds to a single chemical potential
                  limit - otherwise will use the first chemical potential limit in the
                  ``chempots`` dict.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.defect_thermodynamics.)chempots["limits"]``
                  dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``FermiSolver.defect_thermodynamics.el_refs = ...``
                or ``DefectThermodynamics.el_refs = ...`` (with the same input options)
                to set the default elemental reference energies for all calculations.
                (Default: None)
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame: DataFrame containing defect and carrier concentrations.
        """
        # Ensure temperature ranges are lists:
        if isinstance(temperature_range, float):
            temperature_range = [temperature_range]
        if annealing_temperature_range is not None and isinstance(annealing_temperature_range, float):
            annealing_temperature_range = [annealing_temperature_range]
        if isinstance(quenched_temperature_range, float):
            quenched_temperature_range = [quenched_temperature_range]

        single_chempot_dict, el_refs = self._get_single_chempot_dict(limit, chempots, el_refs)

        if annealing_temperature_range is not None:
            return pd.concat(
                [
                    self.pseudo_equilibrium_solve(
                        single_chempot_dict=single_chempot_dict,
                        el_refs=el_refs,
                        quenched_temperature=quench_temp,
                        annealing_temperature=anneal_temp,
                        effective_dopant_concentration=effective_dopant_concentration,
                        fix_charge_states=fix_charge_states,
                        free_defects=free_defects,
                        fixed_defects=fixed_defects,
                    )
                    for quench_temp, anneal_temp in tqdm(
                        product(quenched_temperature_range, annealing_temperature_range)
                    )
                ]
            )

        return pd.concat(
            [
                self.equilibrium_solve(
                    single_chempot_dict=single_chempot_dict,
                    el_refs=el_refs,
                    temperature=temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                    fixed_defects=fixed_defects,
                )
                for temperature in tqdm(temperature_range)
            ]
        )

    def scan_dopant_concentration(
        self,
        effective_dopant_concentration_range: Union[float, list[float]],
        chempots: Optional[dict[str, float]] = None,
        limit: Optional[str] = None,
        el_refs: Optional[dict[str, float]] = None,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        r"""
        Calculate the defect concentrations under a range of effective dopant
        concentrations.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            effective_dopant_concentration_range (Union[float, list[float]]):
                The range of effective dopant concentrations to solver over.
                This can be a single value or a list of values representing
                different concentrations.
            chempots (dict):
                Dictionary of chemical potentials to use for calculating the defect
                formation energies (and thus concentrations). If ``None`` (default),
                will use ``self.defect_thermodynamics.chempots`` (= 0 for all
                chemical potentials by default).
                This can have the form of ``{"limits": [{'limit': [chempot_dict]}], ...}``
                (the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials)) and specific limits (chemical potential
                limits) can then be chosen using ``limit``.

                Alternatively this can be a dictionary of chemical potentials for a
                single limit (``limit``), in the format:
                ``{element symbol: chemical potential}``.
                If manually specifying chemical potentials this way, you can set the
                ``el_refs`` option with the DFT reference energies of the elemental phases,
                in which case it is the formal chemical potentials (i.e. relative to the
                elemental references) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.

                Note that you can also set ``FermiSolver.defect_thermodynamics.chempots = ...``
                or ``DefectThermodynamics.chempots = ...`` (with the same input options)
                to set the default chemical potentials for all calculations.
                (Default: None)
            limit (str):
                The chemical potential limit for which to calculate the Fermi level
                positions and defect/carrier concentrations. Can be either:

                - ``None``, if ``chempots`` corresponds to a single chemical potential
                  limit - otherwise will use the first chemical potential limit in the
                  ``chempots`` dict.
                - ``"X-rich"/"X-poor"`` where X is an element in the system, in which
                  case the most X-rich/poor limit will be used (e.g. "Li-rich").
                - A key in the ``(self.defect_thermodynamics.)chempots["limits"]``
                  dictionary.

                The latter two options can only be used if ``chempots`` is in the
                ``doped`` format (see chemical potentials tutorial).
                (Default: None)
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``{element symbol: chemical potential}``). Unnecessary if ``chempots`` is
                provided/present in format generated by ``doped`` (see tutorials).

                Note that you can also set ``FermiSolver.defect_thermodynamics.el_refs = ...``
                or ``DefectThermodynamics.el_refs = ...`` (with the same input options)
                to set the default elemental reference energies for all calculations.
                (Default: None)
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame:
                A ``DataFrame`` containing the defect and carrier concentrations for each
                effective dopant concentration. Each row represents the concentrations
                for a different dopant concentration.
        """
        if isinstance(effective_dopant_concentration_range, float):
            effective_dopant_concentration_range = [effective_dopant_concentration_range]

        single_chempot_dict, el_refs = self._get_single_chempot_dict(limit, chempots, el_refs)

        if annealing_temperature is not None:
            return pd.concat(
                [
                    self.pseudo_equilibrium_solve(
                        single_chempot_dict=single_chempot_dict,
                        el_refs=el_refs,
                        quenched_temperature=quenched_temperature,
                        annealing_temperature=annealing_temperature,
                        effective_dopant_concentration=effective_dopant_concentration,
                        fix_charge_states=fix_charge_states,
                        free_defects=free_defects,
                        fixed_defects=fixed_defects,
                    )
                    for effective_dopant_concentration in tqdm(effective_dopant_concentration_range)
                ]
            )

        return pd.concat(
            [
                self.equilibrium_solve(
                    single_chempot_dict=single_chempot_dict,
                    el_refs=el_refs,
                    temperature=temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                    fixed_defects=fixed_defects,
                )
                for effective_dopant_concentration in tqdm(effective_dopant_concentration_range)
            ]
        )

    def interpolate_chempots(
        self,
        n_points: int,
        chempots: Optional[Union[list[dict], dict]] = None,
        limits: Optional[list[str]] = None,
        el_refs: Optional[dict[str, float]] = None,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        Interpolate between two sets of chemical potentials and solve for the
        defect concentrations and Fermi level at each interpolated point.
        Chemical potentials can be interpolated between two sets of chemical
        potentials or between two limits.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            n_points (int):
                The number of points to generate between chemical potential
                end points.
            chempots (Optional[list[dict]]):
                The chemical potentials to interpolate between. This can be
                either a list containing two dictionaries, each representing
                a set of chemical potentials for a single limit (in the format:
                ``{element symbol: chemical potential}``) to interpolate between,
                or can be a single chemical potentials dictionary in the
                ``doped`` format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- in which case ``limits`` must be specified to pick the
                end-points to interpolate between.

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.
                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...``
                or ``DefectThermodynamics.chempots = ...`` (with the same input
                options) to set the default chemical potentials for all calculations.

                If manually specifying chemical potentials with a list of two
                dictionaries, you can also set the ``el_refs`` option with the DFT
                reference energies of the elemental phases if desired, in which case
                it is the formal chemical potentials (i.e. relative to the elemental
                references) that should be given here, otherwise the absolute (DFT)
                chemical potentials should be given.
            limits (Optional[list[str]]):
                The chemical potential limits to interpolate between, as a list
                containing two strings. Each string should be in the format
                ``"X-rich"/"X-poor"``, where X is an element in the system, or
                be a key in the ``(self.defect_thermodynamics.)chempots["limits"]``
                dictionary.

                If not provided, ``chempots`` must be specified as a list of two
                single chemical potential dictionaries for single limits.
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``[{element symbol: chemical potential}, ...]``). Unnecessary if
                ``chempots`` is provided/present in format generated by ``doped``
                (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``).

                Note that you can also set ``FermiSolver.defect_thermodynamics.el_refs = ...``
                or ``DefectThermodynamics.el_refs = ...`` (with the same input options)
                to set the default elemental reference energies for all calculations.
                (Default: None)
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame:
                A ``DataFrame`` containing the defect and carrier concentrations for
                each interpolated set of chemical potentials. Each row represents the
                concentrations for a different interpolated point.
        """
        if isinstance(chempots, list):  # should be two single chempot dictionaries
            if len(chempots) != 2:
                raise ValueError(
                    f"If `chempots` is a list, it must contain two dictionaries representing the starting "
                    f"and ending chemical potentials. The provided list has {len(chempots)} entries!"
                )
            single_chempot_dict_1, single_chempot_dict_2 = chempots

        else:  # should be a dictionary in the ``doped`` format or ``None``:
            chempots, el_refs = self.defect_thermodynamics._get_chempots(
                chempots, el_refs
            )  # returns self.defect_thermodynamics.chempots if chempots is None
            if chempots is None:
                raise ValueError(
                    "No chemical potentials supplied or present in self.defect_thermodynamics.chempots!"
                )

            if limits is None or len(limits) != 2:
                raise ValueError(
                    f"If `chempots` is not provided as a list, then `limits` must be a list containing "
                    f"two strings representing the chemical potential limits to interpolate between. "
                    f"The provided `limits` is: {limits}."
                )

            assert isinstance(chempots, dict)  # typing
            single_chempot_dict_1, el_refs = self._get_single_chempot_dict(limits[0], chempots, el_refs)
            single_chempot_dict_2, el_refs = self._get_single_chempot_dict(limits[1], chempots, el_refs)

        interpolated_chempots = self._get_interpolated_chempots(
            single_chempot_dict_1, single_chempot_dict_2, n_points
        )

        if annealing_temperature is not None:
            return pd.concat(
                [
                    self.pseudo_equilibrium_solve(
                        single_chempot_dict=single_chempot_dict,
                        el_refs=el_refs,
                        quenched_temperature=quenched_temperature,
                        annealing_temperature=annealing_temperature,
                        effective_dopant_concentration=effective_dopant_concentration,
                        fix_charge_states=fix_charge_states,
                        free_defects=free_defects,
                        fixed_defects=fixed_defects,
                    )
                    for single_chempot_dict in tqdm(interpolated_chempots)
                ]
            )

        return pd.concat(
            [
                self.equilibrium_solve(
                    single_chempot_dict=single_chempot_dict,
                    el_refs=el_refs,
                    temperature=temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                )
                for single_chempot_dict in tqdm(interpolated_chempots)
            ]
        )

    def _get_interpolated_chempots(
        self,
        chempot_start: dict,
        chempot_end: dict,
        n_points: int,
    ) -> list:
        """
        Generate a set of interpolated chemical potentials between two points.

        Here, these should be dictionaries of chemical potentials for `single`
        limits, in the format: ``{element symbol: chemical potential}``.
        If ``el_refs`` is provided (to the parent function) or set in
        ``self.defect_thermodynamics.el_refs``, then it is the formal chemical
        potentials (i.e. relative to the elemental reference energies) that
        should be used here, otherwise the absolute (DFT) chemical potentials
        should be used.

        Args:
            chempot_start (dict):
                A dictionary representing the starting chemical potentials.
            chempot_end (dict):
                A dictionary representing the ending chemical potentials.
            n_points (int):
                The number of interpolated points to generate, `including`
                the start and end points.

        Returns:
            list:
                A list of dictionaries, where each dictionary contains a `single`
                set of interpolated chemical potentials. The length of the list
                corresponds to `n_points`, and each dictionary corresponds to an
                interpolated state between the starting and ending chemical potentials.
        """
        return [
            {
                key: chempot_start[key] + (chempot_end[key] - chempot_start[key]) * i / (n_points - 1)
                for key in chempot_start
            }
            for i in range(n_points)
        ]

    def scan_chempots(
        self,
        chempots: Union[list[dict[str, float]], dict[str, dict]],
        limits: Optional[list[str]] = None,
        el_refs: Optional[dict[str, float]] = None,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        Scan over a range of chemical potentials and solve for the defect
        concentrations and Fermi level at each set of chemical potentials.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            chempots (Optional[Union[list[dict], dict]]):
                The chemical potentials to scan over. This can be either a list
                containing dictionaries of a set of chemical potentials for a
                `single` limit (in the format: ``{element symbol: chemical potential}``),
                or can be a single chemical potentials dictionary in the
                ``doped`` format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- in which case ``limits`` can be specified to pick the chemical
                potential limits to scan over (otherwise scans over all limits in the
                ``chempots`` dictionary).

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.
                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...``
                or ``DefectThermodynamics.chempots = ...`` (with the same input
                options) to set the default chemical potentials for all calculations.

                If manually specifying chemical potentials with a list of dictionaries,
                you can also set the ``el_refs`` option with the DFT reference energies
                of the elemental phases if desired, in which case it is the formal
                chemical potentials (i.e. relative to the elemental references) that
                should be given here, otherwise the absolute (DFT) chemical potentials
                should be given.
            limits (Optional[list[str]]):
                The chemical potential limits to scan over, as a list of strings, if
                ``chempots`` was provided / is present in the ``doped`` format. Each
                string should be in the format ``"X-rich"/"X-poor"``, where X is an
                element in the system, or be a key in the
                ``(self.defect_thermodynamics.)chempots["limits"]`` dictionary.

                If ``None`` (default) and ``chempots`` is in the ``doped`` format
                (rather than a list of single chemical potential limits), will scan
                over all limits in the ``chempots`` dictionary.
            el_refs (dict):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}`` (to determine the formal chemical
                potentials, when ``chempots`` has been manually specified as
                ``[{element symbol: chemical potential}, ...]``). Unnecessary if
                ``chempots`` is provided/present in format generated by ``doped``
                (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``).

                Note that you can also set ``FermiSolver.defect_thermodynamics.el_refs = ...``
                or ``DefectThermodynamics.el_refs = ...`` (with the same input options)
                to set the default elemental reference energies for all calculations.
                (Default: None)
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame:
                A ``DataFrame`` containing defect and carrier concentrations for each
                set of chemical potentials. Each row corresponds to a different set
                of chemical potentials.
        """
        if isinstance(chempots, dict):  # should be a dictionary in the ``doped`` format or ``None``:
            chempots, el_refs = self.defect_thermodynamics._get_chempots(
                chempots, el_refs
            )  # returns self.defect_thermodynamics.chempots if chempots is None
            if chempots is None:
                raise ValueError(
                    "No chemical potentials supplied or present in self.defect_thermodynamics.chempots!"
                )
            assert isinstance(chempots, dict)  # typing
            chempots, el_refs = self._parse_and_check_grid_like_chempots(chempots)

            if limits is None:
                limits = list(chempots["limits_wrt_el_refs"].keys())
            elif not isinstance(limits, list):
                raise ValueError(
                    "`limits` must be either a list of limits (as strings) or `None` for `scan_chempots`!"
                )

            chempots = [self._get_single_chempot_dict(limit, chempots, el_refs)[0] for limit in limits]

        if annealing_temperature is not None:
            return pd.concat(
                [
                    self.pseudo_equilibrium_solve(
                        single_chempot_dict=single_chempot_dict,
                        quenched_temperature=quenched_temperature,
                        annealing_temperature=annealing_temperature,
                        effective_dopant_concentration=effective_dopant_concentration,
                        fix_charge_states=fix_charge_states,
                        free_defects=free_defects,
                        fixed_defects=fixed_defects,
                    )
                    for single_chempot_dict in chempots
                ]
            )

        return pd.concat(
            [
                self.equilibrium_solve(
                    single_chempot_dict=single_chempot_dict,
                    temperature=temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                    fixed_defects=fixed_defects,
                )
                for single_chempot_dict in chempots
            ]
        )

    def scan_chemical_potential_grid(
        self,
        chempots: Optional[dict] = None,
        n_points: int = 10,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        r"""
        Given a ``doped``-formatted chemical potential dictionary, generate a
        ``ChemicalPotentialGrid`` object and calculate the Fermi level
        positions and defect/carrier concentrations at the grid points.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            chempots (Optional[dict]):
                Dictionary of chemical potentials to scan over, in the ``doped``
                format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials).

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.

                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...`` or
                ``DefectThermodynamics.chempots = ...`` to set the default chemical
                potentials for all calculations, and you can set
                ``FermiSolver.defect_thermodynamics.el_refs = ...`` or
                ``DefectThermodynamics.el_refs = ...`` if you want to update the
                elemental reference energies for any reason.
            n_points (int):
                The number of points to generate along each axis of the grid.
                The actual number of grid points may be less, as points outside
                the convex hull are excluded. Default is 10.
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame: A DataFrame containing the Fermi level solutions at the grid
            points, based on the provided chemical potentials and conditions.
        """
        chempots, el_refs = self._parse_and_check_grid_like_chempots(chempots)
        grid = ChemicalPotentialGrid(chempots).get_grid(n_points)

        if annealing_temperature is not None:
            return pd.concat(
                [
                    self.pseudo_equilibrium_solve(
                        single_chempot_dict={
                            k.replace("μ_", ""): v for k, v in chempot_series.to_dict().items()
                        },
                        el_refs=el_refs,
                        annealing_temperature=annealing_temperature,
                        quenched_temperature=quenched_temperature,
                        effective_dopant_concentration=effective_dopant_concentration,
                        fix_charge_states=fix_charge_states,
                        free_defects=free_defects,
                        fixed_defects=fixed_defects,
                    )
                    for _idx, chempot_series in tqdm(grid.iterrows())
                ]
            )

        return pd.concat(
            [
                self.equilibrium_solve(
                    single_chempot_dict={
                        k.replace("μ_", ""): v for k, v in chempot_series.to_dict().items()
                    },
                    el_refs=el_refs,
                    temperature=temperature,
                    effective_dopant_concentration=effective_dopant_concentration,
                )
                for _idx, chempot_series in tqdm(grid.iterrows())
            ]
        )

    def _parse_and_check_grid_like_chempots(self, chempots: Optional[dict] = None) -> tuple[dict, dict]:
        r"""
        Parse a dictionary of chemical potentials for the chemical potential
        scanning functions, checking that it is in the correct format.

        Args:
            chempots (Optional[dict]):
                Dictionary of chemical potentials to scan over, in the ``doped``
                format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials).

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.

                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...`` or
                ``DefectThermodynamics.chempots = ...`` to set the default chemical
                potentials for all calculations, and you can set
                ``FermiSolver.defect_thermodynamics.el_refs = ...`` or
                ``DefectThermodynamics.el_refs = ...`` if you want to update the
                elemental reference energies for any reason.

        Returns:
            dict:
                The chemical potentials in the correct format, along with the
                elemental reference energies.
        """
        # returns self.defect_thermodynamics.chempots if chempots is None:
        chempots, el_refs = self.defect_thermodynamics._get_chempots(chempots)

        if chempots is None:
            raise ValueError(
                "No chemical potentials supplied or present in `self.defect_thermodynamics.chempots`!"
            )
        if len(chempots["limits"]) == 1:
            raise ValueError(
                "Only one chemical potential limit is present in "
                "`chempots`/`self.defect_thermodynamics.chempots`, which makes no sense for a chemical "
                "potential grid scan (with `scan_chemical_potential_grid`/`min_max_X`/`scan_chempots`)!"
            )

        return chempots, el_refs

    def min_max_X(
        self,
        target: str,
        min_or_max: str = "max",
        chempots: Optional[dict] = None,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        tolerance: float = 0.01,
        n_points: int = 10,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        r"""
        Search for the chemical potentials that minimize or maximize a target
        variable, such as electron concentration, within a specified tolerance.

        This function iterates over a grid of chemical potentials and "zooms in" on
        the chemical potential that either minimizes or maximizes the target variable.
        The process continues until the change in the target variable is less than
        the specified tolerance.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            target (str):
                The target variable to minimize or maximize, e.g., "Electrons (cm^-3)".
                # TODO: Allow this to match a substring of the column name.
            min_or_max (str):
                Specify whether to "minimize" ("min") or "maximize" ("max"; default)
                the target variable.
            chempots (Optional[dict]):
                Dictionary of chemical potentials to scan over, in the ``doped``
                format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials).

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.

                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...`` or
                ``DefectThermodynamics.chempots = ...`` to set the default chemical
                potentials for all calculations, and you can set
                ``FermiSolver.defect_thermodynamics.el_refs = ...`` or
                ``DefectThermodynamics.el_refs = ...`` if you want to update the
                elemental reference energies for any reason.
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            tolerance (float):
                The convergence criterion for the target variable. The search
                stops when the target value change is less than this value.
                Defaults to ``0.01``.
            n_points (int):
                The number of points to generate along each axis of the grid for
                the initial search. Defaults to ``10``.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame:
                A ``DataFrame`` containing the results of the minimization or
                maximization process, including the optimal chemical potentials and
                the corresponding values of the target variable.

        Raises:
            ValueError:
                If neither ``chempots`` nor ``self.chempots`` is provided, or if
                ``min_or_max`` is not ``"minimize"/"min"`` or ``"maximize"/"max"``.
        """
        # Determine the dimensionality of the chemical potential space
        chempots, el_refs = self._parse_and_check_grid_like_chempots(chempots)
        n_chempots = len(el_refs)

        # Call the appropriate method based on dimensionality
        if n_chempots == 2:
            return self._min_max_X_line(
                target,
                min_or_max,
                chempots,
                annealing_temperature,
                quenched_temperature,
                temperature,
                tolerance,
                n_points,
                effective_dopant_concentration,
                fix_charge_states,
                fixed_defects,
                free_defects,
            )
        return self._min_max_X_grid(
            target,
            min_or_max,
            chempots,
            annealing_temperature,
            quenched_temperature,
            temperature,
            tolerance,
            n_points,
            effective_dopant_concentration,
            fix_charge_states,
            fixed_defects,
            free_defects,
        )

    def _min_max_X_line(
        self,
        target: str,
        min_or_max: str,
        chempots: dict,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        tolerance: float = 0.01,
        n_points: int = 100,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        r"""
        Search for the chemical potentials that minimize or maximize a target
        variable, such as electron concentration, within a specified tolerance.

        This function iterates over a grid of chemical potentials and "zooms in" on
        the chemical potential that either minimizes or maximizes the target variable.
        The process continues until the change in the target variable is less than
        the specified tolerance.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            target (str):
                The target variable to minimize or maximize, e.g., "Electrons (cm^-3)".
                # TODO: Allow this to match a substring of the column name.
            min_or_max (str):
                Specify whether to "minimize" ("min") or "maximize" ("max"; default)
                the target variable.
            chempots (dict):
                Dictionary of chemical potentials to scan over, in the ``doped``
                format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials).

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.

                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...`` or
                ``DefectThermodynamics.chempots = ...`` to set the default chemical
                potentials for all calculations, and you can set
                ``FermiSolver.defect_thermodynamics.el_refs = ...`` or
                ``DefectThermodynamics.el_refs = ...`` if you want to update the
                elemental reference energies for any reason.
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            tolerance (float):
                The convergence criterion for the target variable. The search
                stops when the target value change is less than this value.
                Defaults to ``0.01``.
            n_points (int):
                The number of points to generate along each axis of the grid for
                the initial search. Defaults to ``10``.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame:
                A ``DataFrame`` containing the results of the minimization or
                maximization process, including the optimal chemical potentials and
                the corresponding values of the target variable.

        Raises:
            ValueError:
                If neither ``chempots`` nor ``self.chempots`` is provided, or if
                ``min_or_max`` is not ``"minimize"/"min"`` or ``"maximize"/"max"``.
        """
        el_refs = chempots["elemental_refs"]
        chempots_label = list(el_refs.keys())  # Assuming 1D space, focus on one label.
        chempots_labels = [f"μ_{label}" for label in chempots_label]

        # Initial line scan: Rich to Poor limit
        rich = self._get_single_chempot_dict(f"{chempots_label[0]}-rich")
        poor = self._get_single_chempot_dict(f"{chempots_label[0]}-poor")
        starting_line = self._get_interpolated_chempots(rich[0], poor[0], n_points)

        previous_value = None

        while True:
            # Calculate results based on the given temperature conditions
            if annealing_temperature is not None:
                results_df = pd.concat(
                    [
                        self.pseudo_equilibrium_solve(
                            single_chempot_dict={
                                k.replace("μ_", ""): v for k, v in chempot_series.items()
                            },
                            el_refs=el_refs,
                            annealing_temperature=annealing_temperature,
                            quenched_temperature=quenched_temperature,
                            effective_dopant_concentration=effective_dopant_concentration,
                            fix_charge_states=fix_charge_states,
                            free_defects=free_defects,
                            fixed_defects=fixed_defects,
                        )
                        for chempot_series in tqdm(starting_line)
                    ]
                )
            else:
                results_df = pd.concat(
                    [
                        self.equilibrium_solve(
                            single_chempot_dict={
                                k.replace("μ_", ""): v for k, v in chempot_series.items()
                            },
                            el_refs=el_refs,
                            temperature=temperature,
                            effective_dopant_concentration=effective_dopant_concentration,
                        )
                        for chempot_series in tqdm(starting_line)
                    ]
                )

            if target in results_df.columns:
                if "min" in min_or_max:
                    target_chempot = results_df[results_df[target] == results_df[target].min()][
                        chempots_labels
                    ]
                    target_dataframe = results_df[results_df[target] == results_df[target].min()]
                elif "max" in min_or_max:
                    target_chempot = results_df[results_df[target] == results_df[target].max()][
                        chempots_labels
                    ]
                    target_dataframe = results_df[results_df[target] == results_df[target].max()]
                current_value = (
                    results_df[target].min() if "min" in min_or_max else results_df[target].max()
                )

            else:
                # Filter the DataFrame for the specific defect
                filtered_df = results_df[results_df.index == target]
                # Find the row where "Concentration (cm^-3)" is at its minimum or maximum
                if "min" in min_or_max:
                    min_value = filtered_df["Concentration (cm^-3)"].min()
                    target_chempot = results_df.loc[results_df["Concentration (cm^-3)"] == min_value][
                        chempots_labels
                    ]
                    target_dataframe = results_df[
                        results_df[chempots_labels].eq(target_chempot.iloc[0]).all(axis=1)
                    ]

                elif "max" in min_or_max:
                    max_value = filtered_df["Concentration (cm^-3)"].max()
                    target_chempot = results_df.loc[results_df["Concentration (cm^-3)"] == max_value][
                        chempots_labels
                    ]
                    target_dataframe = results_df[
                        results_df[chempots_labels].eq(target_chempot.iloc[0]).all(axis=1)
                    ]
                current_value = (
                    filtered_df["Concentration (cm^-3)"].min()
                    if "min" in min_or_max
                    else filtered_df["Concentration (cm^-3)"].max()
                )

            # Check if the change in the target value is less than the tolerance
            if (
                previous_value is not None
                and abs((current_value - previous_value) / previous_value) < tolerance
            ):
                break
            previous_value = current_value
            target_chempot = target_chempot.drop_duplicates(ignore_index=True)

            starting_line = self._get_interpolated_chempots(
                chempot_start={
                    k: v - 0.1 for k, v in target_chempot.iloc[0].items() if k in chempots_labels
                },
                chempot_end={
                    k: v + 0.1 for k, v in target_chempot.iloc[0].items() if k in chempots_labels
                },
                n_points=100,
            )

            return target_dataframe

    def _min_max_X_grid(
        self,
        target: str,
        min_or_max: str = "max",
        chempots: Optional[dict] = None,
        annealing_temperature: Optional[float] = None,
        quenched_temperature: float = 300,
        temperature: float = 300,
        tolerance: float = 0.01,
        n_points: int = 10,
        effective_dopant_concentration: Optional[float] = None,
        fix_charge_states: bool = False,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        r"""
        Search for the chemical potentials that minimize or maximize a target
        variable, such as electron concentration, within a specified tolerance.

        This function iterates over a grid of chemical potentials and "zooms in" on
        the chemical potential that either minimizes or maximizes the target variable.
        The process continues until the change in the target variable is less than
        the specified tolerance.

        If ``annealing_temperature`` (and ``quenched_temperature``; 300 K by
        default) are specified, then the frozen defect approximation is
        employed, whereby total defect concentrations are calculated at the
        elevated annealing temperature, then fixed at these values (unless
        ``free_defects`` or ``fix_charge_states`` are specified) and the Fermi
        level and relative charge state populations are recalculated at the
        quenched temperature. Otherwise, if only ``temperature`` is specified,
        then the Fermi level and defect/carrier concentrations are calculated
        assuming thermodynamic equilibrium at that temperature.

        Args:
            target (str):
                The target variable to minimize or maximize, e.g., "Electrons (cm^-3)".
                # TODO: Allow this to match a substring of the column name.
            min_or_max (str):
                Specify whether to "minimize" ("min") or "maximize" ("max"; default)
                the target variable.
            chempots (Optional[dict]):
                Dictionary of chemical potentials to scan over, in the ``doped``
                format (i.e. ``{"limits": [{'limit': [chempot_dict]}], ...}``)
                -- the format generated by ``doped``\'s chemical potential parsing
                functions (see tutorials).

                If ``None`` (default), will use ``self.defect_thermodynamics.chempots``.

                Note that you can also set
                ``FermiSolver.defect_thermodynamics.chempots = ...`` or
                ``DefectThermodynamics.chempots = ...`` to set the default chemical
                potentials for all calculations, and you can set
                ``FermiSolver.defect_thermodynamics.el_refs = ...`` or
                ``DefectThermodynamics.el_refs = ...`` if you want to update the
                elemental reference energies for any reason.
            annealing_temperature (Optional[float]):
                Temperature in Kelvin at which to calculate the high temperature
                (fixed) total defect concentrations, which should correspond to
                the highest temperature during annealing/synthesis of the material
                (at which we assume equilibrium defect concentrations) within the
                frozen defect approach. Default is ``None`` (uses ``temperature``
                under thermodynamic equilibrium).
            quenched_temperature (float):
                Temperature in Kelvin at which to calculate the self-consistent
                (constrained equilibrium) Fermi level and carrier concentrations,
                given the fixed total concentrations, which should correspond to
                operating temperature of the material (typically room temperature).
                Default is 300 K.
            temperature (float):
                The temperature at which to solve for defect concentrations
                and Fermi level, under thermodynamic equilibrium (if
                ``annealing_temperature`` is not specified).
                Defaults to 300 K.
            tolerance (float):
                The convergence criterion for the target variable. The search
                stops when the target value change is less than this value.
                Defaults to ``0.01``.
            n_points (int):
                The number of points to generate along each axis of the grid for
                the initial search. Defaults to ``10``.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or
                impurity in the material. This value is included in the charge
                neutrality condition to analyze the Fermi level and doping
                response under hypothetical doping conditions.
                A positive value corresponds to donor doping, while a negative
                value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no additional extrinsic
                dopant.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``) upon quenching. Not expected to be
                physically sensible in most cases. Defaults to ``False``.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected
                to be "frozen-in" upon quenching. Defaults to ``None``.

        Returns:
            pd.DataFrame:
                A ``DataFrame`` containing the results of the minimization or
                maximization process, including the optimal chemical potentials and
                the corresponding values of the target variable.

        Raises:
            ValueError:
                If neither ``chempots`` nor ``self.chempots`` is provided, or if
                ``min_or_max`` is not ``"minimize"/"min"`` or ``"maximize"/"max"``.
        """
        chempots, el_refs = self._parse_and_check_grid_like_chempots(chempots)
        starting_grid = ChemicalPotentialGrid(chempots)
        current_vertices = starting_grid.vertices
        chempots_labels = list(current_vertices.columns)
        previous_value = None

        while True:
            if annealing_temperature is not None:
                results_df = pd.concat(
                    [
                        self.pseudo_equilibrium_solve(
                            single_chempot_dict={
                                k.replace("μ_", ""): v for k, v in chempot_series.to_dict().items()
                            },
                            el_refs=el_refs,
                            annealing_temperature=annealing_temperature,
                            quenched_temperature=quenched_temperature,
                            effective_dopant_concentration=effective_dopant_concentration,
                            fix_charge_states=fix_charge_states,
                            free_defects=free_defects,
                            fixed_defects=fixed_defects,
                        )
                        for _idx, chempot_series in tqdm(starting_grid.get_grid(n_points).iterrows())
                    ]
                )
            else:
                results_df = pd.concat(
                    [
                        self.equilibrium_solve(
                            single_chempot_dict={
                                k.replace("μ_", ""): v for k, v in chempot_series.to_dict().items()
                            },
                            el_refs=el_refs,
                            temperature=temperature,
                            effective_dopant_concentration=effective_dopant_concentration,
                        )
                        for _idx, chempot_series in tqdm(starting_grid.get_grid(n_points).iterrows())
                    ]
                )

            # Find chemical potentials value where target is lowest or highest
            if target in results_df.columns:
                if "min" in min_or_max:
                    target_chempot = results_df[results_df[target] == results_df[target].min()][
                        chempots_labels
                    ]
                    target_dataframe = results_df[results_df[target] == results_df[target].min()]
                elif "max" in min_or_max:
                    target_chempot = results_df[results_df[target] == results_df[target].max()][
                        chempots_labels
                    ]
                    target_dataframe = results_df[results_df[target] == results_df[target].max()]
                current_value = (
                    results_df[target].min() if "min" in min_or_max else results_df[target].max()
                )

            else:
                # Filter the DataFrame for the specific defect
                filtered_df = results_df[results_df.index == target]
                # Find the row where "Concentration (cm^-3)" is at its minimum or maximum
                if "min" in min_or_max:
                    min_value = filtered_df["Concentration (cm^-3)"].min()
                    target_chempot = results_df.loc[results_df["Concentration (cm^-3)"] == min_value][
                        chempots_labels
                    ]
                    target_dataframe = results_df[
                        results_df[chempots_labels].eq(target_chempot.iloc[0]).all(axis=1)
                    ]

                elif "max" in min_or_max:
                    max_value = filtered_df["Concentration (cm^-3)"].max()
                    target_chempot = results_df.loc[results_df["Concentration (cm^-3)"] == max_value][
                        chempots_labels
                    ]
                    target_dataframe = results_df[
                        results_df[chempots_labels].eq(target_chempot.iloc[0]).all(axis=1)
                    ]
                current_value = (
                    filtered_df["Concentration (cm^-3)"].min()
                    if "min" in min_or_max
                    else filtered_df["Concentration (cm^-3)"].max()
                )

            # Check if the change in the target value is less than the tolerance
            if (
                previous_value is not None
                and abs((current_value - previous_value) / previous_value) < tolerance
            ):
                break
            previous_value = current_value
            target_chempot = target_chempot.drop_duplicates(ignore_index=True)

            new_vertices = [  # get midpoint between current vertices and target_chempot
                (current_vertices + row[1]) / 2 for row in target_chempot.iterrows()
            ]
            # Generate a new grid around the target_chempot that
            # does not go outside the bounds of the starting grid
            new_vertices_df = pd.DataFrame(new_vertices[0], columns=chempots_labels)
            starting_grid = ChemicalPotentialGrid(new_vertices_df.to_dict("index"))

        return target_dataframe

    def _generate_dopant_for_py_sc_fermi(self, effective_dopant_concentration: float) -> "DefectSpecies":
        """
        Generate a dopant defect charge state object, for use with the ``py-sc-
        fermi`` functions.

        This method creates a defect charge state object representing an
        arbitrary dopant or impurity in the material, used to include in the
        charge neutrality condition and analyze the Fermi level/doping
        response under hypothetical doping conditions.

        Args:
            effective_dopant_concentration (float):
                The fixed concentration of the dopant or impurity in the
                material, specified in cm^-3. A positive value indicates
                donor doping (positive defect charge state), while a negative
                value indicates acceptor doping (negative defect charge state).

        Returns:
            DefectSpecies:
                An instance of the ``DefectSpecies`` class, representing the
                generated dopant with the specified charge state and concentration.

        Raises:
            ValueError:
                If ``effective_dopant_concentration`` is zero or if there is an
                issue with generating the dopant.
        """
        self._check_required_backend_and_error("py-sc-fermi")
        assert self._DefectChargeState
        assert self._DefectSpecies
        dopant = self._DefectChargeState(
            charge=np.sign(effective_dopant_concentration),
            fixed_concentration=abs(effective_dopant_concentration) / 1e24 * self.volume,
            degeneracy=1,
        )
        return self._DefectSpecies(
            nsites=1, charge_states={np.sign(effective_dopant_concentration): dopant}, name="Dopant"
        )

    def _generate_defect_system(
        self,
        single_chempot_dict: dict[str, float],
        el_refs: Optional[dict[str, float]] = None,
        temperature: float = 300,
        effective_dopant_concentration: Optional[float] = None,
        fixed_defects: Optional[dict[str, float]] = None,
    ) -> "DefectSystem":
        """
        Generates a ``DefectSystem`` object from ``self.defect_thermodynamics``
        and a set of chemical potentials.

        This method constructs a ``DefectSystem`` object, which encompasses all
        relevant defect species and their properties under the given conditions,
        including temperature, chemical potentials, and an optional dopant
        concentration.

        Args:
            single_chempot_dict (dict[str, float]):
                Dictionary of chemical potentials to use for calculating the equilibrium
                Fermi level position and defect/carrier concentrations. Here, this
                should be a dictionary of chemical potentials for a single limit
                (``limit``), in the format: ``{element symbol: chemical potential}``.
                If ``el_refs`` is provided or set in ``self.defect_thermodynamics.el_refs``
                then it is the formal chemical potentials (i.e. relative to the elemental
                reference energies) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
            el_refs (dict[str, float]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}``. Unnecessary if
                ``self.defect_thermodynamics.el_refs`` is set (i.e. if ``chempots`` was
                provided to ``self.defect_thermodynamics`` in the format generated by
                ``doped``).
                (Default: None)
            temperature (float):
                The temperature at which to perform the calculations, in Kelvin.
                Defaults to 300 K.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant or impurity
                in the material. This value is included in the charge neutrality
                condition to analyze the Fermi level and doping response under
                hypothetical doping conditions. A positive value corresponds to donor
                doping, while a negative value corresponds to acceptor doping.
                Defaults to ``None``, corresponding to no extrinsic dopant.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.

        Returns:
            DefectSystem:
                An initialized ``DefectSystem`` object, containing the defect species
                with their charge states, formation energies, and degeneracies, as well
                as the density of states (DOS), volume, and temperature of the system.
        """
        self._check_required_backend_and_error("py-sc-fermi")
        assert self._DefectSpecies
        assert self._DefectSystem
        entries = list(self.defect_thermodynamics.defect_entries.values())
        entries = sorted(entries, key=lambda x: x.name)
        labels = {_get_label_and_charge(entry.name)[0] for entry in entries}
        defect_species: dict[str, Any] = {
            label: {"charge_states": {}, "nsites": None, "name": label} for label in labels
        }
        dft_chempots = _get_dft_chempots(single_chempot_dict, el_refs)

        for entry in entries:
            label, charge = _get_label_and_charge(entry.name)
            defect_species[label]["nsites"] = entry.defect.multiplicity / self.multiplicity_scaling

            formation_energy = self.defect_thermodynamics.get_formation_energy(
                entry, chempots=dft_chempots, fermi_level=0
            )
            total_degeneracy = np.prod(list(entry.degeneracy_factors.values()))
            defect_species[label]["charge_states"][charge] = {
                "charge": charge,
                "energy": formation_energy,
                "degeneracy": total_degeneracy,
            }

        all_defect_species = [self._DefectSpecies.from_dict(v) for k, v in defect_species.items()]
        if effective_dopant_concentration is not None:
            dopant = self._generate_dopant_for_py_sc_fermi(effective_dopant_concentration)
            all_defect_species.append(dopant)

        if fixed_defects is not None:
            for k, v in fixed_defects.items():
                if k.split("_")[-1].strip("+-").isdigit():
                    q = int(k.split("_")[-1])
                    defect_name = "_".join(k.split("_")[:-1])
                    next(d for d in all_defect_species if d.name == defect_name).charge_states[
                        q
                    ].fix_concentration(v / 1e24 * self.volume)
                else:
                    next(d for d in all_defect_species if d.name == k).fix_concentration(
                        v / 1e24 * self.volume
                    )

        return self._DefectSystem(
            defect_species=all_defect_species,
            dos=self.py_sc_fermi_dos,
            volume=self.volume,
            temperature=temperature,
            convergence_tolerance=1e-20,
        )

    def _generate_annealed_defect_system(
        self,
        annealing_temperature: float,
        single_chempot_dict: dict[str, float],
        el_refs: Optional[dict[str, float]] = None,
        quenched_temperature: float = 300,
        fix_charge_states: bool = False,
        effective_dopant_concentration: Optional[float] = None,
        fixed_defects: Optional[dict[str, float]] = None,
        free_defects: Optional[list[str]] = None,
    ) -> "DefectSystem":
        """
        Generate a ``py-sc-fermi`` ``DefectSystem`` object that has defect
        concentrations fixed to the values determined at a high temperature
        (``annealing_temperature``), and then set to a lower temperature
        (``quenched_temperature``).

        This method creates a defect system where defect concentrations are
        initially calculated at an annealing temperature and then "frozen" as
        the system is cooled to a lower quenched temperature. It can optionally
        fix the concentrations of individual defect charge states or allow
        charge states to vary while keeping total defect concentrations fixed.

        Args:
            annealing_temperature (float):
                The higher temperature (in Kelvin) at which the system is annealed
                to set initial defect concentrations.
            single_chempot_dict (dict[str, float]):
                Dictionary of chemical potentials to use for calculating the equilibrium
                Fermi level position and defect/carrier concentrations. Here, this
                should be a dictionary of chemical potentials for a single limit
                (``limit``), in the format: ``{element symbol: chemical potential}``.
                If ``el_refs`` is provided or set in ``self.defect_thermodynamics.el_refs``
                then it is the formal chemical potentials (i.e. relative to the elemental
                reference energies) that should be given here, otherwise the absolute
                (DFT) chemical potentials should be given.
            el_refs (dict[str, float]):
                Dictionary of elemental reference energies for the chemical potentials
                in the format:
                ``{element symbol: reference energy}``. Unnecessary if
                ``self.defect_thermodynamics.el_refs`` is set (i.e. if ``chempots`` was
                provided to ``self.defect_thermodynamics`` in the format generated by
                ``doped``).
                (Default: None)
            quenched_temperature (float):
                The lower temperature (in Kelvin) to which the system is quenched.
                Defaults to 300 K.
            fix_charge_states (bool):
                Whether to fix the concentrations of individual defect charge states
                (``True``) or allow charge states to vary while keeping total defect
                concentrations fixed (``False``). Defaults to ``False``.
            effective_dopant_concentration (Optional[float]):
                The fixed concentration (in cm^-3) of an arbitrary dopant/impurity in
                the material. A positive value indicates donor doping, while a negative
                value indicates acceptor doping. Defaults to ``None``, corresponding to
                no extrinsic dopant.
            fixed_defects (Optional[dict[str, float]]):
                A dictionary of defect concentrations to fix at the quenched temperature,
                in the format: ``{defect_name: concentration}``. Concentrations should be
                given in cm^-3. The this can be used to fix the concentrations of specific
                defects regardless of the chemical potentials, or anneal-quench procedure
                (e.g. to simulate the effect of a fixed impurity concentration).
                If a fixed-concentration of a specific charge state is desired,
                the defect name should be formatted as ``"defect_name_charge"``.
                I.e. ``"v_O_+2"`` for a doubly positively charged oxygen vacancy.
                Defaults to ``None``.
            free_defects (Optional[list[str]]):
                A list of defects to be excluded from high-temperature concentration
                fixing, useful for highly mobile defects that are not expected to be
                "frozen-in." Defaults to ``None``.

        Returns:
            DefectSystem: A low-temperature defect system (`quenched_temperature`)
            with defect concentrations fixed to high-temperature (`annealing_temperature`) values.
        """
        self._check_required_backend_and_error("py-sc-fermi")
        if free_defects is None:
            free_defects = []

        defect_system = self._generate_defect_system(
            single_chempot_dict=single_chempot_dict,  # chempots handled in _generate_defect_system()
            el_refs=el_refs,
            temperature=annealing_temperature,
            effective_dopant_concentration=effective_dopant_concentration,
        )
        initial_conc_dict = defect_system.concentration_dict()  # concentrations at initial temperature

        # Exclude the free_defects, carrier concentrations and Fermi level from fixing
        all_free_defects = ["Fermi Energy", "n0", "p0"]
        all_free_defects.extend(free_defects)

        # Get the fixed concentrations of non-exceptional defects
        decomposed_conc_dict = defect_system.concentration_dict(decomposed=True)
        additional_data = {}
        for k, v in decomposed_conc_dict.items():
            if k not in all_free_defects:
                for k1, v1 in v.items():
                    additional_data[k + "_" + str(k1)] = v1
        initial_conc_dict.update(additional_data)

        fixed_concs = {k: v for k, v in initial_conc_dict.items() if k not in all_free_defects}

        # Apply the fixed concentrations
        for defect_species in defect_system.defect_species:
            if fix_charge_states:
                for k, v in defect_species.charge_states.items():
                    key = f"{defect_species.name}_{int(k)}"
                    if key in list(fixed_concs.keys()):
                        v.fix_concentration(fixed_concs[key] / 1e24 * defect_system.volume)

            elif defect_species.name in fixed_concs:
                defect_species.fix_concentration(
                    fixed_concs[defect_species.name] / 1e24 * defect_system.volume
                )

        fixed_charge_state_names = []
        fixed_species_names = []
        if fixed_defects is not None:
            for k, v in fixed_defects.items():
                if k.split("_")[-1].strip("+-").isdigit():
                    q = int(k.split("_")[-1])
                    defect_name = "_".join(k.split("_")[:-1])
                    next(d for d in defect_system.defect_species if d.name == defect_name).charge_states[
                        q
                    ].fix_concentration(v / 1e24 * self.volume)
                    fixed_charge_state_names.append(k)
                    if v > fixed_concs[defect_name]:
                        warnings.warn(
                            f"""Fixed concentration of {k} ({v}) is higher than
                            the total concentration of ({fixed_concs[defect_name]})
                            at the annealing temperature. Adjusting the total
                            concentration of {defect_name} to {v}. Check that
                            this is the behavior you expect."""
                        )
                        defect_system.defect_species_by_name(defect_name).fix_concentration(
                            v / 1e24 * self.volume
                        )
                else:
                    next(d for d in defect_system.defect_species if d.name == k).fix_concentration(
                        v / 1e24 * self.volume
                    )
                fixed_species_names.append(k)
            target_system = deepcopy(defect_system)
            target_system.temperature = quenched_temperature
            return target_system

        target_system = deepcopy(defect_system)
        target_system.temperature = quenched_temperature
        return target_system
