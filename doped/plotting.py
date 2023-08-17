"""
Code to analyse VASP defect calculations.

These functions are built from a combination of useful modules from pymatgen
and AIDE (by Adam Jackson and Alex Ganose), alongside substantial modification,
in the efforts of making an efficient, user-friendly package for managing and
analysing defect calculations, with publication-quality outputs.
"""
import copy
import warnings
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import colormaps, rc, ticker
from pymatgen.util.string import latexify
from shakenbreak.plotting import _format_defect_name

default_fonts = [
    "Whitney Book Extended",
    "Whitney Pro",
    "Arial",
    "Whitney Book",
    "Helvetica",
    "Liberation Sans",
    "Andale Sans",
]


# TODO: Lean out the options for this function (inherited from AIDE)(particularly those that can just be
#  edited by the user with the returned Matplotlib object, or with rcParams - show example of this in
#  notebooks maybe?) -> add kwargs option to pass to matplotlib
# TODO: Add argument descriptions to docstrings
# TODO: Add option to only plot defect states that are stable at some point in the bandgap
# TODO: Add option to plot formation energies at the centroid of the chemical stability region? And make
#  this the default if no chempot_limits are specified? Or better default to plot both the most (
#  most-electronegative-)anion-rich and the (most-electropositive-)cation-rich chempot limits?
def formation_energy_plot(
    defect_phase_diagram,
    chempot_limits: Optional[Dict] = None,
    elt_refs: Optional[Dict] = None,
    fonts=None,
    xlim=None,
    ylim=None,
    ax_fontsize=1.0,
    lg_fontsize=1.0,
    lg_position=None,
    fermi_level=None,
    title: Optional[str] = None,
    saved=False,
    colormap="Dark2",
    frameon=False,
    chempot_table=True,
    pd_facets: Optional[List] = None,
    auto_labels: bool = False,
    filename: Optional[str] = None,
    emphasis=False,
):
    """
    Produce a defect formation energy vs Fermi energy plot (i.e. a defect
    transition level diagram).

    Args:
        defect_phase_diagram (DefectPhaseDiagram):
             DefectPhaseDiagram object (likely created from analysis.dpd_from_defect_dict)
        chempot_limits (dict):
            This can either be a dictionary of chosen absolute/DFT chemical potentials: {Elt:
            Energy} (giving a single formation energy table - recommended to use the elt_refs
            option with this to show the formal (relative) chemical potentials in the plot) or a
            dictionary including the key-value pair: {"facets": [{'facet': [chempot_dict]}]},
            following the format generated by doped: cpa.read_phase_diagram_and_chempots() (see
            example notebooks). If not specified, chemical potentials are not included in the
            formation energy calculation (all set to zero energy).
        elt_refs (dict):
            Dictionary of elemental reference energies for the chemical potentials in the format:
            {Elt: ref_energy} (to determine the formal chemical potentials, when chempot_limits
            is specified as a dictionary {Elt: Energy}). Unnecessary if chempot_limits is
            provided in format generated by doped: cpa.read_phase_diagram_and_chempots() (see
            example notebooks).
            Default: None
        fonts (list): List of fonts to use for the plot. (default: default_fonts)
        xlim:
            Tuple (min,max) giving the range of the x (fermi energy) axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ylim:
            Tuple (min,max) giving the range for the formation energy axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ax_fontsize:
            float  multiplier to change axis label fontsize
        lg_fontsize:
            float  multiplier to change legend label fontsize
        lg_position:
            Tuple (horizontal-position, vertical-position) giving the position
            to place the legend.
            Example: (0.5,-0.75) will likely put it below the x-axis.
        fermi_level (float):
            Fermi level to use for computing the defect formation energies. (default: 0 (i.e.
            at the VBM))
        title (str): Title for the plot. (default: None)
        saved (bool): Whether to save the plot to file. (default: False)
        colormap (str): Colormap to use for the plot. (default: "Dark2")
        frameon (bool): Whether to show a frame around the plot legend. (default: False)
        pd_facets (list):
            A list facet(s) / chemical potential limit(s) for which to print the defect formation
            energy tables. If not specified, will print formation energy tables for each facet in
            the phase diagram. (default: None)
        chempot_table (bool): Whether to print the chemical potential table above the plot. (default: True)
        auto_labels (bool):
            Whether to automatically label the transition levels with their charge states. (default: False)
        filename (str): Filename to save the plot to. (default: None)
        emphasis (bool):
            Whether to plot the full formation energy lines for all charge states in faded grey
            (default: False)


    Returns:
        a matplotlib object
    """
    # TODO: Refactor to use style sheet instead of all these options
    if chempot_limits and "facets" in chempot_limits:
        if pd_facets is None:
            pd_facets = chempot_limits["facets"].keys()  # Phase diagram facets to use for chemical
            # potentials, to calculate and plot formation energies
        for facet in pd_facets:
            mu_elts = chempot_limits["facets"][facet]
            elt_refs = chempot_limits["elemental_refs"]
            plot_title = title if title else facet
            plot_filename = filename if filename else plot_title + "_" + facet + ".pdf"

            plot = _aide_pmg_plot(
                defect_phase_diagram,
                mu_elts=mu_elts,
                elt_refs=elt_refs,
                fonts=fonts,
                xlim=xlim,
                ylim=ylim,
                ax_fontsize=ax_fontsize,
                lg_fontsize=lg_fontsize,
                lg_position=lg_position,
                fermi_level=fermi_level,
                title=plot_title,
                saved=saved,
                colormap=colormap,
                frameon=frameon,
                chempot_table=chempot_table,
                auto_labels=auto_labels,
                filename=plot_filename,
                emphasis=emphasis,
            )

        return plot

    # Else if you only want to give {Elt: Energy} dict for chempot_limits, or no chempot_limits
    return _aide_pmg_plot(
        defect_phase_diagram,
        mu_elts=chempot_limits,
        elt_refs=elt_refs,
        fonts=fonts,
        xlim=xlim,
        ylim=ylim,
        ax_fontsize=ax_fontsize,
        lg_fontsize=lg_fontsize,
        lg_position=lg_position,
        fermi_level=fermi_level,
        title=title,
        saved=saved,
        colormap=colormap,
        frameon=frameon,
        chempot_table=chempot_table,
        auto_labels=auto_labels,
        filename=filename,
        emphasis=emphasis,
    )


def _aide_pmg_plot(
    defect_phase_diagram,
    mu_elts=None,
    elt_refs=None,
    fonts=None,
    xlim=None,
    ylim=None,
    ax_fontsize=1.0,
    lg_fontsize=1.0,
    lg_position=None,
    fermi_level=None,
    title=None,
    saved=False,
    colormap="Dark2",
    frameon=False,
    chempot_table=True,
    auto_labels=False,
    filename=None,
    emphasis=False,
):
    """
    Produce defect Formation energy vs Fermi energy plot
    Args:
        mu_elts:
            a dictionary of {Element:value} giving the chemical
            potential of each element
        xlim:
            Tuple (min,max) giving the range of the x (fermi energy) axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ylim:
            Tuple (min,max) giving the range for the formation energy axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ax_fontsize:
            float  multiplier to change axis label fontsize
        lg_fontsize:
            float  multiplier to change legend label fontsize
        lg_position:
            Tuple (horizontal-position, vertical-position) giving the position
            to place the legend.
            Example: (0.5,-0.75) will likely put it below the x-axis.

    Returns:
        a matplotlib object.
    """
    if mu_elts is None:
        warnings.warn(
            "No chemical potentials specified, so chemical potentials are set to zero "
            "for each species. Note that this will give large errors in the absolute "
            "values of formation energies, but the transition level positions will be "
            "unaffected."
        )

    if xlim is None:
        xlim = (-0.4, defect_phase_diagram.band_gap + 0.4)
    xy = {}
    all_lines_xy = {}  # For emphasis plots with faded grey E_form lines for all charge states
    lower_cap = -100.0
    upper_cap = 100.0
    y_range_vals = []  # for finding max/min values on y-axis based on x-limits

    for defnom, def_tl in defect_phase_diagram.transition_level_map.items():
        xy[defnom] = [[], []]
        if emphasis:
            all_lines_xy[defnom] = [[], []]
            for chg_ent in defect_phase_diagram.stable_entries[defnom]:
                for x_extrem in [lower_cap, upper_cap]:
                    all_lines_xy[defnom][0].append(x_extrem)
                    all_lines_xy[defnom][1].append(
                        defect_phase_diagram._formation_energy(
                            chg_ent, chemical_potentials=mu_elts, fermi_level=x_extrem
                        )
                    )
                # for x_window in xlim:
                #    y_range_vals.append(
                #        defect_phase_diagram._formation_energy(chg_ent, chemical_potentials=mu_elts,
                #        fermi_level=x_window)
                #    )

        if def_tl:
            org_x = list(def_tl.keys())  # list of transition levels
            org_x.sort()  # sorted with lowest first

            # establish lower x-bound
            first_charge = max(def_tl[org_x[0]])
            for chg_ent in defect_phase_diagram.stable_entries[defnom]:
                if chg_ent.charge_state == first_charge:
                    form_en = defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=lower_cap
                    )
                    fe_left = defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=xlim[0]
                    )
            xy[defnom][0].append(lower_cap)
            xy[defnom][1].append(form_en)
            y_range_vals.append(fe_left)
            # iterate over stable charge state transitions
            for fl in org_x:
                charge = max(def_tl[fl])
                for chg_ent in defect_phase_diagram.stable_entries[defnom]:
                    if chg_ent.charge_state == charge:
                        form_en = defect_phase_diagram._formation_energy(
                            chg_ent, chemical_potentials=mu_elts, fermi_level=fl
                        )
                xy[defnom][0].append(fl)
                xy[defnom][1].append(form_en)
                y_range_vals.append(form_en)
            # establish upper x-bound
            last_charge = min(def_tl[org_x[-1]])
            for chg_ent in defect_phase_diagram.stable_entries[defnom]:
                if chg_ent.charge_state == last_charge:
                    form_en = defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=upper_cap
                    )
                    fe_right = defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=xlim[1]
                    )
            xy[defnom][0].append(upper_cap)
            xy[defnom][1].append(form_en)
            y_range_vals.append(fe_right)
        else:
            # no transition - just one stable charge
            chg_ent = defect_phase_diagram.stable_entries[defnom][0]
            for x_extrem in [lower_cap, upper_cap]:
                xy[defnom][0].append(x_extrem)
                xy[defnom][1].append(
                    defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=x_extrem
                    )
                )
            for x_window in xlim:
                y_range_vals.append(
                    defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=x_window
                    )
                )

    cmap = colormaps[colormap]
    colors = cmap(np.linspace(0, 1, len(xy)))
    if colormap == "Dark2" and len(xy) >= 8:
        warnings.warn(
            f"""
The chosen colormap is Dark2, which only has 8 colours, yet you have {len(xy)} defect species (so
some defects will have the same line colour). Recommended to change/set colormap to 'tab10' or
'tab20' (10 and 20 colours each)."""
        )
    plt.figure(dpi=600, figsize=(2.6, 1.95))  # Gives a final figure width of c. 3.5
    # inches, the standard single column width for publication (which is what we're about)
    plt.clf()
    width = 9
    ax = _pretty_axis(fonts=fonts)
    # plot formation energy lines
    for_legend = []
    for cnt, defnom in enumerate(xy.keys()):
        ax.plot(
            xy[defnom][0],
            xy[defnom][1],
            color=colors[cnt],
            markeredgecolor=colors[cnt],
            lw=1.2,
            markersize=3.5,
        )
        for_legend.append(copy.deepcopy(defect_phase_diagram.stable_entries[defnom][0]))
    # Redo for loop so grey 'all_lines_xy' not included in legend
    for cnt, defnom in enumerate(xy.keys()):
        if emphasis:
            ax.plot(
                all_lines_xy[defnom][0],
                all_lines_xy[defnom][1],
                color=(0.8, 0.8, 0.8),
                markeredgecolor=colors[cnt],
                lw=1.2,
                markersize=3.5,
                alpha=0.5,
            )
    # plot transition levels
    for cnt, defnom in enumerate(xy.keys()):
        x_trans, y_trans = [], []
        tl_labels = []
        tl_label_type = []
        for x_val, chargeset in defect_phase_diagram.transition_level_map[defnom].items():
            x_trans.append(x_val)
            for chg_ent in defect_phase_diagram.stable_entries[defnom]:
                if chg_ent.charge_state == chargeset[0]:
                    form_en = defect_phase_diagram._formation_energy(
                        chg_ent, chemical_potentials=mu_elts, fermi_level=x_val
                    )
            y_trans.append(form_en)
            tl_labels.append(
                rf"$\epsilon$({max(chargeset):{'+' if max(chargeset) else ''}}/"
                f"{min(chargeset):{'+' if min(chargeset) else ''}})"
            )
            tl_label_type.append("start_positive" if max(chargeset) > 0 else "end_negative")
        if x_trans:
            ax.plot(
                x_trans,
                y_trans,
                marker="o",
                color=colors[cnt],
                markeredgecolor=colors[cnt],
                lw=1.2,
                markersize=3.5,
                fillstyle="full",
            )
            if auto_labels:
                for index, coords in enumerate(zip(x_trans, y_trans)):
                    text_alignment = "right" if tl_label_type[index] == "start_positive" else "left"
                    ax.annotate(
                        tl_labels[index],  # this is the text
                        coords,  # this is the point to label
                        textcoords="offset points",  # how to position the text
                        xytext=(0, 5),  # distance from text to points (x,y)
                        ha=text_alignment,  # horizontal alignment of text
                        size=ax_fontsize * width * 0.9,
                        annotation_clip=True,
                    )  # only show label if coords in current axes

    # get latex-like legend titles
    legends_txt = []
    for defect_entry in for_legend:
        try:
            defect_name = (
                _format_defect_name(
                    defect_species=defect_entry.name,
                    include_site_num_in_name=False,
                ).rsplit("^", 1)[0]
                + "$"
            )  # exclude charge  # Format defect name for title and axis labels
        except Exception:  # if formatting fails, just use the defect_species name
            defect_name = defect_entry.name

        # add subscript labels for different configurations of same defect species
        if defect_name in legends_txt:
            defect_name = (
                _format_defect_name(
                    defect_species=defect_entry.name,
                    include_site_num_in_name=True,
                ).rsplit("^", 1)[0]
                + "$"
            )  # exclude charge
        if defect_name in legends_txt:
            i = 1
            while defect_name in legends_txt:
                i += 1
                defect_name = defect_name[:-3] + f"{chr(96+i)}" + defect_name[-3:]  # a, b c etc
            legends_txt.append(defect_name)
        else:
            legends_txt.append(defect_name)

    if not lg_position:
        ax.legend(
            legends_txt,
            fontsize=lg_fontsize * width,
            loc=2,
            bbox_to_anchor=(1, 1),
            frameon=frameon,
            prop=fonts,
        )
    else:
        ax.legend(
            legends_txt,
            fontsize=lg_fontsize * width,
            ncol=3,
            loc="lower center",
            bbox_to_anchor=lg_position,
        )

    if ylim is None:
        window = max(y_range_vals) - min(y_range_vals)
        spacer = 0.1 * window
        ylim = (0, max(y_range_vals) + spacer)
        if auto_labels:  # need to manually set xlim or ylim if labels cross axes!!
            ylim = (0, max(y_range_vals) * 1.17) if spacer / ylim[1] < 0.145 else ylim
            # Increase y_limit to give space for transition level labels

    # Show colourful band edges
    ax.imshow(
        [(0, 1), (0, 1)],
        cmap=plt.cm.Blues,
        extent=(xlim[0], 0, ylim[0], ylim[1]),
        vmin=0,
        vmax=3,
        interpolation="bicubic",
        rasterized=True,
        aspect="auto",
    )

    ax.imshow(
        [(1, 0), (1, 0)],
        cmap=plt.cm.Oranges,
        extent=(defect_phase_diagram.band_gap, xlim[1], ylim[0], ylim[1]),
        vmin=0,
        vmax=3,
        interpolation="bicubic",
        rasterized=True,
        aspect="auto",
    )

    ax.set_ylim(ylim)
    ax.set_xlim(xlim)
    # ax.plot([xlim[0], xlim[1]], [0, 0], "k-")  # black dashed line for E_formation = 0

    if fermi_level is not None:
        plt.axvline(
            x=fermi_level, linestyle="-.", color="k", linewidth=1
        )  # smaller dashed lines for gap edges
    ax.set_xlabel("Fermi Level (eV)", size=ax_fontsize * width)
    ax.set_ylabel("Formation Energy (eV)", size=ax_fontsize * width)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(4))
    ax.xaxis.set_minor_locator(ticker.AutoMinorLocator(2))
    ax.yaxis.set_major_locator(ticker.MaxNLocator(4))
    ax.yaxis.set_minor_locator(ticker.AutoMinorLocator(2))
    ax.xaxis.set_major_formatter(_CustomScalarFormatter(minus_symbol="-"))
    ax.yaxis.set_major_formatter(_CustomScalarFormatter(minus_symbol="-"))
    if chempot_table:
        if elt_refs is not None:
            facets_wrt_elt_refs = {elt: energy - elt_refs[elt] for elt, energy in mu_elts.items()}
            _plot_chemical_potential_table(
                plt,
                facets_wrt_elt_refs,
                fontsize=ax_fontsize * width,
                wrt_elt_refs=True,
            )
        elif mu_elts:
            _plot_chemical_potential_table(
                plt,
                mu_elts,
                fontsize=ax_fontsize * width,
                wrt_elt_refs=False,
            )

    if title and chempot_table:
        ax.set_title(
            latexify(title),
            size=1.2 * ax_fontsize * width,
            pad=28,
            fontdict={"fontweight": "bold"},
        )
    elif title:
        ax.set_title(latexify(title), size=ax_fontsize * width, fontdict={"fontweight": "bold"})
    if saved or filename:
        if filename:
            plt.savefig(filename, bbox_inches="tight", dpi=600)
        else:
            plt.savefig(str(title) + "_doped_plot.pdf", bbox_inches="tight", dpi=600)
    return ax


def _plot_chemical_potential_table(
    plot,
    chemical_potentials,
    chempot_label="",
    fontsize=9,
    loc="left",
    ax=None,
    wrt_elt_refs=False,
):
    if ax is None:
        ax = plot.gca()

    labels = [""] + [rf"$\mathregular{{\mu_{{{s}}}}}$," for s in sorted(chemical_potentials.keys())]
    # add if else here, to use 'facets' if no wrt_elts, and don't say wrt elt_refs etc.
    labels[1] = "(" + labels[1]
    labels[-1] = labels[-1][:-1] + ")"
    labels = ["Chemical Potentials", *labels, " Units:"]
    text = [[chempot_label]]

    for el in sorted(chemical_potentials.keys()):
        text[0].append(f"{chemical_potentials[el]:.2f},")

    text[0][1] = "(" + text[0][1]
    text[0][-1] = text[0][-1][:-1] + ")"
    if wrt_elt_refs:
        text[0] = ["(wrt Elemental refs)"] + text[0] + ["  [eV]"]
    else:
        text[0] = ["(from calculations)"] + text[0] + ["  [eV]"]
    widths = [0.1] + [0.9 / len(chemical_potentials)] * (len(chemical_potentials) + 2)
    tab = ax.table(cellText=text, colLabels=labels, colWidths=widths, loc="top", cellLoc=loc)
    tab.auto_set_font_size(False)
    tab.set_fontsize(fontsize)

    tab.auto_set_column_width(list(range(len(widths))))
    tab.scale(1.0, 1.0)  # Default spacing is based on fontsize, just bump it up
    for cell in tab.get_celld().values():
        cell.set_linewidth(0)

    return tab


class _CustomScalarFormatter(ticker.ScalarFormatter):
    """
    Derived matplotlib tick formatter for arbitrary minus signs.

    Args:
    minus_symbol (str): Symbol used in place of hyphen
    """

    def __init__(
        self,
        useOffset=None,
        useMathText=None,
        useLocale=None,
        minus_symbol="\N{MINUS SIGN}",
    ):
        self.minus_symbol = minus_symbol
        super().__init__(useOffset=useOffset, useMathText=useMathText, useLocale=useLocale)


def _pretty_axis(ax=None, fonts=None):
    ticklabelsize = 9
    ticksize = 8
    linewidth = 1.0

    if ax is None:
        ax = plt.gca()

    ax.tick_params(width=linewidth, size=ticksize)
    ax.tick_params(which="major", size=ticksize, width=linewidth, labelsize=ticklabelsize, pad=3)
    ax.tick_params(which="minor", size=ticksize / 2, width=linewidth)

    ax.set_title(ax.get_title(), size=9.5)
    for axis in ["top", "bottom", "left", "right"]:
        ax.spines[axis].set_linewidth(linewidth)

    labelsize = int(9)

    ax.set_xlabel(ax.get_xlabel(), size=labelsize)
    ax.set_ylabel(ax.get_ylabel(), size=labelsize)

    fonts = default_fonts if fonts is None else fonts + default_fonts  # TODO: Refactor this to only use
    # default fonts if no fonts have been chosen by the user

    rc("font", **{"family": "sans-serif", "sans-serif": fonts})
    rc("text", usetex=False)
    rc("pdf", fonttype=42)
    # rc('mathtext', fontset='stixsans')

    return ax


def all_lines_formation_energy_plot(
    defect_phase_diagram,
    chempot_limits: Optional[Dict] = None,
    elt_refs: Optional[Dict] = None,
    fonts=None,
    xlim=None,
    ylim=None,
    ax_fontsize=1.0,
    lg_fontsize=1.0,
    lg_position=None,
    fermi_level=None,
    title=None,
    saved=False,
    colormap="Dark2",
    frameon=False,
    chempot_table=True,
    pd_facets: Optional[List] = None,
    auto_labels: bool = False,
    filename: Optional[str] = None,
):
    """
    Produce a defect formation energy vs Fermi energy plot (i.e. a defect
    transition level diagram), showing the full formation energy lines for all
    defect species present.

    Args:
        defect_phase_diagram (DefectPhaseDiagram):
             DefectPhaseDiagram object (likely created from analysis.dpd_from_defect_dict)
        chempot_limits (dict):
            This can either be a dictionary of chosen absolute/DFT chemical potentials: {Elt:
            Energy} (giving a single formation energy table - recommended to use the elt_refs
            option with this to show the formal (relative) chemical potentials in the plot) or a
            dictionary including the key-value pair: {"facets": [{'facet': [chempot_dict]}]},
            following the format generated by doped: cpa.read_phase_diagram_and_chempots() (see
            example notebooks). If not specified, chemical potentials are not included in the
            formation energy calculation (all set to zero energy).
        elt_refs (dict):
            Dictionary of elemental reference energies for the chemical potentials in the format:
            {Elt: ref_energy} (to determine the formal chemical potentials, when chempot_limits
            is specified as a dictionary {Elt: Energy}). Unnecessary if chempot_limits is
            provided in format generated by doped: cpa.read_phase_diagram_and_chempots() (see
            example notebooks).
            Default: None
        fonts (list): List of fonts to use for the plot. (default: default_fonts)
        xlim:
            Tuple (min,max) giving the range of the x (fermi energy) axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ylim:
            Tuple (min,max) giving the range for the formation energy axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ax_fontsize:
            float  multiplier to change axis label fontsize
        lg_fontsize:
            float  multiplier to change legend label fontsize
        lg_position:
            Tuple (horizontal-position, vertical-position) giving the position
            to place the legend.
            Example: (0.5,-0.75) will likely put it below the x-axis.
        fermi_level (float):
            Fermi level to use for computing the defect formation energies. (default: 0 (i.e.
            at the VBM))
        title (str): Title for the plot. (default: None)
        saved (bool): Whether to save the plot to file. (default: False)
        colormap (str): Colormap to use for the plot. (default: "Dark2")
        frameon (bool): Whether to show a frame around the plot legend. (default: False)
        pd_facets (list):
            A list facet(s) / chemical potential limit(s) for which to print the defect formation
            energy tables. If not specified, will print formation energy tables for each facet in
            the phase diagram. (default: None)
        chempot_table (bool): Whether to print the chemical potential table above the plot. (default: True)
        auto_labels (bool):
            Whether to automatically label the transition levels with their charge states. (default: False)
        filename (str): Filename to save the plot to. (default: None)
        emphasis (bool):
            Whether to plot the full formation energy lines for all charge states in faded grey
            (default: False)

    Returns:
        a matplotlib object
    """
    if chempot_limits and "facets" in chempot_limits:
        if pd_facets is None:
            pd_facets = chempot_limits["facets"].keys()  # Phase diagram facets to use for chemical
            # potentials, to calculate and plot formation energies
        for facet in pd_facets:
            mu_elts = chempot_limits["facets"][facet]
            elt_refs = chempot_limits["elemental_refs"]
            plot_filename = filename
            if title:
                plot_title = title
                if not filename:
                    plot_filename = plot_title + "_" + facet + ".pdf"
            else:
                plot_title = facet

            return _all_lines_aide_pmg_plot(
                defect_phase_diagram,
                mu_elts=mu_elts,
                elt_refs=elt_refs,
                fonts=fonts,
                xlim=xlim,
                ylim=ylim,
                ax_fontsize=ax_fontsize,
                lg_fontsize=lg_fontsize,
                lg_position=lg_position,
                fermi_level=fermi_level,
                title=plot_title,
                saved=saved,
                colormap=colormap,
                frameon=frameon,
                chempot_table=chempot_table,
                auto_labels=auto_labels,
                filename=plot_filename,
            )
        return None

    # If you only want to give {Elt: Energy} dict for chempot_limits, or no chempot_limits
    return _all_lines_aide_pmg_plot(
        defect_phase_diagram,
        mu_elts=chempot_limits,
        elt_refs=elt_refs,
        fonts=fonts,
        xlim=xlim,
        ylim=ylim,
        ax_fontsize=ax_fontsize,
        lg_fontsize=lg_fontsize,
        lg_position=lg_position,
        fermi_level=fermi_level,
        title=title,
        saved=saved,
        colormap=colormap,
        frameon=frameon,
        chempot_table=chempot_table,
        auto_labels=auto_labels,
        filename=filename,
    )


def _all_lines_aide_pmg_plot(
    defect_phase_diagram,
    mu_elts=None,
    elt_refs=None,
    fonts=None,
    xlim=None,
    ylim=None,
    ax_fontsize=1.0,
    lg_fontsize=1.0,
    lg_position=None,
    fermi_level=None,
    title=None,
    saved=False,
    colormap="Dark2",
    frameon=False,
    chempot_table=True,
    auto_labels=False,
    filename=None,
):
    """
    Produce defect Formation energy vs Fermi energy plot
    Args:
        mu_elts:
            a dictionary of {Element:value} giving the chemical potential of each element
        xlim:
            Tuple (min,max) giving the range of the x (fermi energy) axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ylim:
            Tuple (min,max) giving the range for the formation energy axis. This may need to be
            set manually when including transition level labels, so that they don't cross the axes.
        ax_fontsize:
            float  multiplier to change axis label fontsize
        lg_fontsize:
            float  multiplier to change legend label fontsize
        lg_position:
            Tuple (horizontal-position, vertical-position) giving the position
            to place the legend.
            Example: (0.5,-0.75) will likely put it below the x-axis.

    Returns:
        a matplotlib object.
    """
    if xlim is None:
        xlim = (-0.4, defect_phase_diagram.band_gap + 0.4)
    xy = {}
    lower_cap = -100.0
    upper_cap = 100.0
    y_range_vals = []  # for finding max/min values on y-axis based on x-limits

    legends_txt = []
    for defect_entry in defect_phase_diagram.entries:
        try:
            defect_name = _format_defect_name(
                defect_species=defect_entry.name,
                include_site_num_in_name=False,
            )  # Format defect name for title and axis labels
        except Exception:  # if formatting fails, just use the defect_species name
            defect_name = defect_entry.name

        # add subscript labels for different configurations of same defect species
        if defect_name in legends_txt:
            defect_name = _format_defect_name(
                defect_species=defect_entry.name,
                include_site_num_in_name=True,
            )
        if defect_name in legends_txt:
            i = 1
            while defect_name in legends_txt:
                i += 1
                defect_name = defect_name[:-3] + f"{chr(96+i)}" + defect_name[-3:]  # a, b c etc
            legends_txt.append(defect_name)
        else:
            legends_txt.append(defect_name)

        xy[defect_name] = [[], []]
        for x_extrem in [lower_cap, upper_cap]:
            xy[defect_name][0].append(x_extrem)
            xy[defect_name][1].append(
                defect_phase_diagram._formation_energy(
                    defect_entry, chemical_potentials=mu_elts, fermi_level=x_extrem
                )
            )
        for x_window in xlim:
            y_range_vals.append(
                defect_phase_diagram._formation_energy(
                    defect_entry, chemical_potentials=mu_elts, fermi_level=x_window
                )
            )

    cmap = colormaps[colormap]
    colors = cmap(np.linspace(0, 1, len(xy)))
    if colormap == "Dark2" and len(xy) >= 8:
        warnings.warn(
            f"""
The chosen colormap is Dark2, which only has 8 colours, yet you have {len(xy)} defect species (so
some defects will have the same line colour). Recommended to change/set colormap to 'tab10' or
'tab20' (10 and 20 colours each)."""
        )
    plt.figure(dpi=600, figsize=(2.6, 1.95))  # Gives a final figure width of c. 3.5
    # inches, the standard single column width for publication (which is what we're about)
    plt.clf()
    width = 9
    ax = _pretty_axis(fonts=fonts)
    # plot formation energy lines

    for cnt, def_name in enumerate(xy.keys()):
        ax.plot(
            xy[def_name][0],
            xy[def_name][1],
            color=colors[cnt],
            markeredgecolor=colors[cnt],
            lw=1.2,
            markersize=3.5,
        )

    if not lg_position:
        ax.legend(
            legends_txt,
            fontsize=lg_fontsize * width,
            loc=2,
            bbox_to_anchor=(1, 1),
            frameon=frameon,
            prop=fonts,
        )
    else:
        ax.legend(
            legends_txt,
            fontsize=lg_fontsize * width,
            ncol=3,
            loc="lower center",
            bbox_to_anchor=lg_position,
        )

    if ylim is None:
        window = max(y_range_vals) - min(y_range_vals)
        spacer = 0.1 * window
        ylim = (0, max(y_range_vals) + spacer)
        if auto_labels:  # need to manually set xlim or ylim if labels cross axes!!
            ylim = (0, max(y_range_vals) * 1.17) if spacer / ylim[1] < 0.145 else ylim
            # Increase y_limit to give space for transition level labels

    # Show colourful band edges
    ax.imshow(
        [(0, 1), (0, 1)],
        cmap=plt.cm.Blues,
        extent=(xlim[0], 0, ylim[0], ylim[1]),
        vmin=0,
        vmax=3,
        interpolation="bicubic",
        rasterized=True,
        aspect="auto",
    )

    ax.imshow(
        [(1, 0), (1, 0)],
        cmap=plt.cm.Oranges,
        extent=(defect_phase_diagram.band_gap, xlim[1], ylim[0], ylim[1]),
        vmin=0,
        vmax=3,
        interpolation="bicubic",
        rasterized=True,
        aspect="auto",
    )

    ax.set_ylim(ylim)
    ax.set_xlim(xlim)
    # ax.plot([xlim[0], xlim[1]], [0, 0], "k-")  # black dashed line for E_formation = 0

    if fermi_level is not None:
        plt.axvline(
            x=fermi_level, linestyle="-.", color="k", linewidth=1
        )  # smaller dashed lines for gap edges
    ax.set_xlabel("Fermi Level (eV)", size=ax_fontsize * width)
    ax.set_ylabel("Formation Energy (eV)", size=ax_fontsize * width)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(4))
    ax.xaxis.set_minor_locator(ticker.AutoMinorLocator(2))
    ax.yaxis.set_major_locator(ticker.MaxNLocator(4))
    ax.yaxis.set_minor_locator(ticker.AutoMinorLocator(2))
    ax.xaxis.set_major_formatter(_CustomScalarFormatter(minus_symbol="-"))
    ax.yaxis.set_major_formatter(_CustomScalarFormatter(minus_symbol="-"))
    if chempot_table:
        if elt_refs is not None:
            facets_wrt_elt_refs = {elt: energy - elt_refs[elt] for elt, energy in mu_elts.items()}
            _plot_chemical_potential_table(
                plt,
                facets_wrt_elt_refs,
                "",
                fontsize=ax_fontsize * width,
                wrt_elt_refs=True,
            )
        elif mu_elts:
            _plot_chemical_potential_table(
                plt,
                mu_elts,
                "",
                fontsize=ax_fontsize * width,
                wrt_elt_refs=False,
            )

    if title and chempot_table:
        ax.set_title(
            latexify(title),
            size=1.2 * ax_fontsize * width,
            pad=28,
            fontdict={"fontweight": "bold"},
        )
    elif title:
        ax.set_title(latexify(title), size=ax_fontsize * width, fontdict={"fontweight": "bold"})

    if saved or filename:
        if filename:
            plt.savefig(filename, bbox_inches="tight", dpi=600)
        else:
            plt.savefig(str(title) + "_doped_plot.pdf", bbox_inches="tight", dpi=600)

    return ax
