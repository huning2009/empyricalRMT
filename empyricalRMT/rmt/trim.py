import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sbn

from numpy import ndarray
from pandas import DataFrame
from pathlib import Path
from pyod.models.hbos import HBOS

from empyricalRMT.rmt._constants import (
    EXPECTED_GOE_MEAN,
    EXPECTED_GOE_VARIANCE,
    DEFAULT_POLY_DEGREE,
    DEFAULT_SPLINE_SMOOTH,
    DEFAULT_SPLINE_DEGREE,
    DEFAULT_POLY_DEGREES,
    DEFAULT_SPLINE_SMOOTHS,
    DEFAULT_SPLINE_DEGREES,
)
from empyricalRMT.rmt.observables.step import stepFunctionVectorized
from empyricalRMT.rmt.plot import _setup_plotting
from empyricalRMT.rmt.smoother import Smoother
from empyricalRMT.rmt.unfold import Unfolded
from empyricalRMT.utils import find_first, find_last, mkdirp


class TrimReport:
    def __init__(
        self, eigenvalues: ndarray, max_trim=0.5, max_iters=7, outlier_tol=0.1
    ):
        eigenvalues = np.sort(eigenvalues)
        self._untrimmed = eigenvalues
        self._unfold_info = None
        self._all_unfolds = None

        self._trim_steps = self.__get_trim_iters(
            tolerance=outlier_tol, max_trim=max_trim, max_iters=max_iters
        )
        self.__unfold_across_trims()  # sets self._unfold_info, self._all_unfolds

    @property
    def untrimmed(self) -> ndarray:
        return self._untrimmed

    @property
    def unfold_info(self) -> DataFrame:
        return self._unfold_info

    @property
    def unfoldings(self) -> DataFrame:
        return self._all_unfolds

    def compare_trim_unfolds(
        self,
        poly_degrees=DEFAULT_POLY_DEGREES,
        spline_smooths=DEFAULT_SPLINE_SMOOTHS,
        spline_degrees=DEFAULT_SPLINE_DEGREES,
        gompertz=True,
    ):
        """Computes unfoldings for the smoothing parameters specified in the arguments, across the
        multiple trim regions.

        Returns
        -------
        report: DataFrame
            A pandas DataFrame with various summary information about the different trims
            and smoothing fits
        """
        self.__unfold_across_trims(
            poly_degrees, spline_smooths, spline_degrees, gompertz
        )
        return self.unfold_info

    def summarize_trim_unfoldings(self) -> (dict, pd.DataFrame, list):
        """Computes GOE fit scores for the unfoldings performed, and returns various "best" fits.

        Parameters
        ----------
        show_plot: boolean
            if True, shows a plot of the automated outlier detection results
        save_plot: Path
            if save_plot is a pathlib file Path, save the outlier detection plot to that
            location. Should be a .png, e.g. "save_plot = Path.home() / outlier_plot.png".
        poly_degrees: List[int]
            the polynomial degrees for which to compute fits. Default [3, 4, 5, 6, 7, 8, 9, 10, 11]
        spline_smooths: List[float]
            the smoothing factors passed into scipy.interpolate.UnivariateSpline fits.
            Default np.linspace(1, 2, num=11)
        spline_degrees: List[int]
            A list of ints determining the degrees of scipy.interpolate.UnivariateSpline
            fits. Default [3]

        Returns
        -------
        best_smoothers: Dict
            A dict with keys "best", "second", "third", (or equivalently "0", "1", "2",
            respectively) and the GOE fit scores
        best_unfoldeds: DataFrame
            a DataFrame with column names identifying the fit method, and columns
            corresponding to the unfolded eigenvalues using those methods. The first
            column has the "best" unfolded values, the second column the second best, and
            etc, up to the third best
        consistent: List
            a list of the "generally" best overall smoothers, across various possible
            trimmings. I.e. returns the smoothers with the best mean and median GOE fit
            scores across all trimmings. Useful for deciding on a single smoothing method
            to use across a dataset.
        """
        report, unfolds = self._unfold_info, self._all_unfolds
        scores = report.filter(regex=".*score.*").abs()

        # get column names so we don't have to deal with terrible Pandas return types
        score_cols = np.array(scores.columns.to_list())
        # gives column names of columns with lowest scores
        best_smoother_cols = list(scores.abs().min().sort_values()[:3].to_dict().keys())
        # indices of rows with best scores
        best_smoother_rows = report[best_smoother_cols].abs().idxmin().to_list()
        # best unfolded eigenvalues
        best_unfoldeds = unfolds[
            map(lambda s: s.replace("--score", ""), best_smoother_cols)
        ]

        # construct dict with trim amounts of best overall scoring smoothers
        best_smoothers = {}
        trim_cols = ["trim_percent", "trim_low", "trim_high"]
        for i, col in enumerate(best_smoother_cols):
            min_score_i = best_smoother_rows[i]
            cols = trim_cols + [
                col.replace("score", "mean_spacing"),
                col.replace("score", "var_spacing"),
                col,
            ]
            if i == 0:
                best_smoothers["best"] = report[cols].iloc[min_score_i, :]
            elif i == 1:
                best_smoothers["second"] = report[cols].iloc[min_score_i, :]
            elif i == 2:
                best_smoothers["third"] = report[cols].iloc[min_score_i, :]
            best_smoothers[i] = report[cols].iloc[min_score_i, :]

        median_scores = np.array(scores.median())
        mean_scores = np.array(scores.mean())

        # get most consistent 3 of each
        best_median_col_idx = np.argsort(median_scores)[:3]
        best_mean_col_idx = np.argsort(mean_scores)[:3]
        top_smoothers_median = set(score_cols[best_median_col_idx])
        top_smoothers_mean = set(score_cols[best_mean_col_idx])
        consistent = top_smoothers_mean.intersection(top_smoothers_median)
        consistent = list(map(lambda s: s.replace("--score", ""), consistent))

        return best_smoothers, best_unfoldeds, consistent

    def unfold(self) -> "Unfolded":
        raise NotImplementedError
        return

    def plot_trim_steps(
        self, title="Trim fits", mode="block", outfile: Path = None, log_info=True
    ):
        """Show which eigenvalues are trimmed at each iteration.

        Parameters
        ----------
        title: string
            The plot title string
        mode: "block" (default) | "noblock" | "save" | "return"
            If "block", call plot.plot() and display plot in a blocking fashion.
            If "noblock", attempt to generate plot in nonblocking fashion.
            If "save", save plot to pathlib Path specified in `outfile` argument
            If "return", return (fig, axes), the matplotlib figure and axes object for modification.
        outfile: Path
            If mode="save", save generated plot to Path specified in `outfile` argument.
            Intermediate directories will be created if needed.
        log_info: boolean
            If True, print additional information about each trimming to stdout.

        Returns
        -------
        (fig, axes): (Figure, Axes)
            The handles to the matplotlib objects, only if `mode` is "return".
        """
        trim_steps = self._trim_steps
        untrimmed = self._untrimmed

        if log_info:
            log = []
            for i, df in enumerate(trim_steps):
                if i == 0:
                    continue
                trim_percent = np.round(
                    100 * (1 - len(df["cluster"] == "inlier") / len(untrimmed)), 2
                )
                eigs_list = list(untrimmed)
                unfolded = df["unfolded"].to_numpy()
                spacings = unfolded[1:] - unfolded[:-1]
                info = "Iteration {:d}: {:4.1f}% trimmed. <s> = {:6.6f}, var(s) = {:04.5f} MSQE: {:5.5f}. Trim indices: ({:d},{:d})".format(
                    i,
                    trim_percent,
                    np.mean(spacings),
                    np.var(spacings, ddof=1),
                    np.mean(df["sqe"]),
                    eigs_list.index(list(df["eigs"])[0]),
                    eigs_list.index(list(df["eigs"])[-1]),
                )
                log.append(info)
            print("\n".join(log))
            print(
                "MSQE, average spacing <s>, and spacings variance var(s)"
                f"calculated for polynomial degree {DEFAULT_POLY_DEGREE} unfolding."
            )

        _setup_plotting()

        width = 5  # 5 plots
        height = np.ceil(len(trim_steps) / width)
        for i, df in enumerate(trim_steps):
            df = df.rename(index=str, columns={"eigs": "λ", "steps": "N(λ)"})
            trim_percent = np.round(
                100 * (1 - len(df["cluster"] == "inlier") / len(untrimmed)), 2
            )
            plt.subplot(height, width, i + 1)
            spacings = np.sort(np.array(df["unfolded"]))
            spacings = spacings[1:] - spacings[:-1]
            sbn.scatterplot(
                data=df,
                x="λ",
                y="N(λ)",
                hue="cluster",
                style="cluster",
                style_order=["inlier", "outlier"],
                linewidth=0,
                legend="brief",
                markers=[".", "X"],
                palette=["black", "red"],
                hue_order=["inlier", "outlier"],
            )
            subtitle = "No trim" if i == 0 else "Trim {:.2f}%".format(trim_percent)
            info = "<s> {:.4f} var(s) {:.4f}".format(
                np.mean(spacings), np.var(spacings, ddof=1)
            )
            plt.title(f"{subtitle}\n{info}")
        plt.subplots_adjust(wspace=0.8, hspace=0.8)
        plt.suptitle(title)

        if mode == "save":
            if outfile is None:
                raise ValueError("Path not specified for `outfile`.")
            try:
                outfile = Path(outfile)
            except BaseException as e:
                raise ValueError("Cannot interpret outfile path.") from e
            mkdirp(outfile.parent)
            fig = plt.gcf()
            fig.set_size_inches(width * 3, height * 3)
            plt.savefig(outfile, dpi=100)
            print(f"Saved {outfile.name} to {str(outfile.parent.absolute())}")
        elif mode == "block" or mode == "noblock":
            fig = plt.gcf()
            fig.set_size_inches(width * 3, height * 3)
            plt.show(block=mode == "block")
        elif mode == "return":
            return plt.gca(), plt.gcf()
        else:
            raise ValueError("Invalid plotting mode.")

    def __get_trim_iters(self, tolerance=0.1, max_trim=0.5, max_iters=7) -> [DataFrame]:
        """Helper function to iteratively perform histogram-based outlier detection
        until reaching either max_trim or max_iters, saving outliers identified at
        each step.

        Paramaters
        ----------
        tolerance: float
            tolerance level for HBOS
        max_trim: float
            Value in (0,1) representing the maximum allowable proportion of eigenvalues
            trimmed.
        max_iters: int
            Maximum number of iterations (times) to perform HBOS outlier detection.

        Returns
        -------
        trim_iters: [DataFrame]
            A list of pandas DataFrames with structure:
            ```
            {
                "eigs": np.array,
                "steps": np.array,
                "unfolded": np.array,
                "cluster": ["inlier" | "outlier"]
            }
            ```
            such that trim_iters[0] is the original values without trimming, and
            trim_iters[i] is a DataFrame of the eigenvalues, step function values,
            unfolded values, and inlier/outlier labels at iteration `i`.
        """
        eigs = self._untrimmed
        steps = stepFunctionVectorized(eigs, eigs)
        unfolded = Smoother(eigs).fit()[0]
        iter_results = [  # zeroth iteration is just the full set of values, none considered outliers
            pd.DataFrame(
                {
                    "eigs": eigs,
                    "steps": steps,
                    "unfolded": unfolded,
                    "sqe": (unfolded - steps) ** 2,
                    "cluster": ["inlier" for _ in eigs],
                }
            )
        ]
        # terminate if we have trimmed max_trim
        iters_run = 0
        while ((len(iter_results[-1]) / len(eigs)) > max_trim) and (
            iters_run < max_iters
        ):
            iters_run += 1
            # because eigs are sorted, HBOS will usually identify outliers at one of the
            # two ends of the eigenvalues, which is what we want
            df = iter_results[-1].copy(deep=True)
            df = df[df["cluster"] == "inlier"]
            hb = HBOS(tol=tolerance)
            is_outlier = np.array(
                hb.fit(df[["eigs", "steps"]]).labels_, dtype=bool
            )  # outliers get "1"

            # check we haven't removed middle values:
            if is_outlier[0]:
                start = find_first(is_outlier, False)
                for i in range(start, len(is_outlier)):
                    is_outlier[i] = False
            if is_outlier[-1]:
                stop = find_last(is_outlier, False)
                for i in range(stop):
                    is_outlier[i] = False
            if not is_outlier[0] and not is_outlier[-1]:  # force a break later
                is_outlier = np.zeros(is_outlier.shape, dtype=bool)

            df["cluster"] = ["outlier" if label else "inlier" for label in is_outlier]
            unfolded, steps = Smoother(df["eigs"]).fit()
            df["unfolded"] = unfolded
            df["sqe"] = (unfolded - steps) ** 2

            iter_results.append(df)
            if np.alltrue(~is_outlier):
                break

        return iter_results

    # TODO: make work with new layout
    def __unfold_across_trims(
        self,
        poly_degrees=DEFAULT_POLY_DEGREES,
        spline_smooths=DEFAULT_SPLINE_SMOOTHS,
        spline_degrees=DEFAULT_SPLINE_DEGREES,
        gompertz=True,
    ):
        """Generate a dataframe showing the unfoldings that results from different
        trim percentages, and different choices of smoothing functions.

        Parameters
        ----------
        poly_degrees: List[int]
            the polynomial degrees for which to compute fits. Default [3, 4, 5, 6, 7, 8, 9, 10, 11]
        spline_smooths: List[float]
            the smoothing factors passed into scipy.interpolate.UnivariateSpline fits.
            Default np.linspace(1, 2, num=11)
        spline_degrees: List[int]
            A list of ints determining the degrees of scipy.interpolate.UnivariateSpline
            fits. Default [3]
        """
        # save args for later
        self.__poly_degrees = poly_degrees
        self.__spline_smooths = spline_smooths
        self.__spline_degrees = spline_degrees

        trims = self._trim_steps
        eigs = self._untrimmed

        # trim_percents = [np.round(100*(1 - len(trim["eigs"]) / len(self.eigs)), 3) for trim in trims]
        col_names_base = Smoother(eigs).fit_all(
            dry_run=True,
            poly_degrees=poly_degrees,
            spline_smooths=spline_smooths,
            spline_degrees=spline_degrees,
            gompertz=gompertz,
        )
        height = len(trims)
        width = (
            len(col_names_base) * 3 + 3
        )  # entry for mean, var, score, plus trim_percent, trim_low, trim_high
        arr = np.empty([height, width], dtype=np.float32)
        for i, trim in enumerate(trims):
            trimmed = np.array(trim["eigs"])
            lower_trim_length = find_first(eigs, trimmed[0])
            upper_trim_length = len(eigs) - 1 - find_last(eigs, trimmed[-1])
            all_unfolds = Smoother(trimmed).fit_all(
                poly_degrees, spline_smooths, spline_degrees, gompertz
            )  # dataframe
            trim_percent = np.round(100 * (1 - len(trimmed) / len(eigs)), 3)
            lower_trim_percent = 100 * lower_trim_length / len(eigs)
            upper_trim_percent = 100 * upper_trim_length / len(eigs)
            arr[i, 0] = trim_percent
            arr[i, 1] = lower_trim_percent
            arr[i, 2] = upper_trim_percent

            for j, col in enumerate(
                all_unfolds
            ):  # get summary starts for each unfolding by smoother
                unfolded = np.array(all_unfolds[col])
                mean, var, score = self.__evaluate_unfolding(unfolded)
                arr[
                    i, 3 * j + 3
                ] = mean  # arr[i, 0] is trim_percent, [i,1] is trim_min, etc
                arr[i, 3 * j + 4] = var
                arr[i, 3 * j + 5] = score

        col_names_final = ["trim_percent", "trim_low", "trim_high"]
        for name in col_names_base:
            col_names_final.append(f"{name}--mean_spacing")
            col_names_final.append(f"{name}--var_spacing")
            col_names_final.append(f"{name}--score")
        trim_report = pd.DataFrame(data=arr, columns=col_names_final)
        self._unfold_info = trim_report
        self._all_unfolds = all_unfolds

    @staticmethod
    def __evaluate_unfolding(unfolded) -> [float, float, float]:
        """Calculate a naive unfolding score via comparison to the expected mean and
        variance of the level spacings of GOE matrices. Positive scores indicate
        there is too much variability in the unfolded eigenvalue spacings, negative
        scores indicate too little. Best score is zero.
        """
        spacings = unfolded[1:] - unfolded[:-1]
        mean, var = np.mean(spacings), np.var(spacings, ddof=1)
        # variance gets weight 1, i.e. mean is 0.05 times as important
        mean_weight = 0.05
        mean_norm = (mean - EXPECTED_GOE_MEAN) / EXPECTED_GOE_MEAN
        var_norm = (var - EXPECTED_GOE_VARIANCE) / EXPECTED_GOE_VARIANCE
        score = var_norm + mean_weight * mean_norm
        return mean, var, score
