"""The planar imaging module analyzes phantom images taken with the kV or MV imager in 2D.
The following phantoms are supported:

* Leeds TOR 18
* Standard Imaging QC-3
* Standard Imaging QC-kV
* Las Vegas
* Doselab MC2 MV
* Doselab MC2 kV
* SNC kV
* SNC MV
* PTW EPID QC

Features:

* **Automatic phantom localization** - Set up your phantom any way you like; automatic positioning,
  angle, and inversion correction mean you can set up how you like, nor will setup variations give you headache.
* **High and low contrast determination** - Analyze both low and high contrast ROIs. Set thresholds
  as you see fit.
"""
import copy
import dataclasses
import io
import math
import os.path as osp
import warnings
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple, Union, BinaryIO, Callable, Dict

import matplotlib.pyplot as plt
import numpy as np
from cached_property import cached_property
from skimage import feature, measure
from skimage.measure._regionprops import RegionProperties

from .core import image, pdf, geometry
from .core.decorators import lru_cache
from .core.geometry import Point, Rectangle, Circle, Vector
from .core.io import get_url, retrieve_demo_file
from .core.mtf import MTF
from .core.profile import Interpolation, CollapsedCircleProfile, SingleProfile
from .core.roi import LowContrastDiskROI, HighContrastDiskROI, bbox_center, Contrast
from .core.utilities import ResultBase
from .ct import get_regions


@dataclass
class PlanarResult(ResultBase):
    """This class should not be called directly. It is returned by the ``results_data()`` method.
    It is a dataclass under the hood and thus comes with all the dunder magic.

    Use the following attributes as normal class attributes."""

    analysis_type: str  #:
    median_contrast: float  #:
    median_cnr: float  #:
    num_contrast_rois_seen: int  #:
    phantom_center_x_y: Tuple[float, float]  #:
    mtf_lp_mm: Tuple[float, float, float] = None  #:


def _middle_of_bbox_region(region: RegionProperties) -> Tuple:
    return (
        (region.bbox[2] - region.bbox[0]) / 2 + region.bbox[0],
        (region.bbox[3] - region.bbox[1]) / 2 + region.bbox[1],
    )


def is_centered(region: RegionProperties, instance: object, rtol=0.3) -> bool:
    """Whether the region is centered on the image"""
    img_center = (instance.image.center.y, instance.image.center.x)
    # we don't want centroid because that could be offset by missing lengths of the outline. Center of bbox is more robust
    return np.allclose(_middle_of_bbox_region(region), img_center, rtol=rtol)


def is_right_size(region: RegionProperties, instance: object, rtol=0.1) -> bool:
    """Whether the region is close to the expected size of the phantom, given the SSD and physical phantom size."""
    return bool(
        np.isclose(
            region.bbox_area,
            instance.phantom_bbox_size_px / (instance._ssd / 1000) ** 2,
            rtol=rtol,
        )
    )


class ImagePhantomBase:
    """Base class for planar phantom classes.

    Attributes
    ----------
    common_name : str
        The human-readable name of the phantom. Used in plots and PDF report.
    phantom_outline_object : {None, 'Circle', 'Rectangle'}
        What type of outline to display on the plotted image. Helps to visually determine the accuracy of the
        phantom size, position, and scale.
    high_contrast_rois : list
        :class:`~pylinac.core.roi.HighContrastDiskROI` instances of the
        high contrast line pair regions.
    high_contrast_roi_settings : dict
        Settings of the placement of the high-contrast ROIs.
    low_contrast_rois : list
        :class:`~pylinac.core.roi.LowContrastDiskROI` instances of the low
        contrast ROIs, other than the reference ROI (below).
    low_contrast_roi_settings : dict
        Settings of the placement of the low-contrast ROIs.
    low_contrast_background_rois : list
        :class:`~pylinac.core.roi.LowContrastDiskROI` instances of the low
        contrast background ROIs.
    low_contrast_background_roi_settings : dict
        Settings of the placement of the background low-contrast ROIs.
    low_contrast_background_value : float
        The average pixel value of all the low-contrast background ROIs.
    detection_conditions: list of callables
        This should be a list of functions that return a boolean. It is used for finding the phantom outline in the image.
        E.g. is_at_center().
    phantom_bbox_size_mm2: float
        This is the expected size of the **BOUNDING BOX** of the phantom. Additionally, it is usually smaller than the
        physical bounding box because we sometimes detect an inner ring/square. Typically, x0.9-1.0 of the physical size.
    """

    _demo_filename: str
    common_name: str
    high_contrast_roi_settings = {}
    high_contrast_rois = []
    low_contrast_roi_settings = {}
    low_contrast_rois = []
    low_contrast_background_roi_settings = {}
    low_contrast_background_rois = []
    low_contrast_background_value = None
    phantom_outline_object = None
    detection_conditions: [List[Callable]] = [is_centered, is_right_size]
    detection_canny_settings = {"sigma": 2, "percentiles": (0.001, 0.01)}
    phantom_bbox_size_mm2: float

    def __init__(
        self,
        filepath: Union[str, BinaryIO, Path],
        normalize: bool = True,
        image_kwargs: Optional[dict] = None,
    ):
        """
        Parameters
        ----------
        filepath : str
            Path to the image file.
        normalize: bool
            Whether to "ground" and normalize the image. This can affect contrast measurements, but for
            backwards compatibility this is True. You may want to set this to False if trying to compare with other software.
        image_kwargs : dict
            Keywords passed to the image load function; this would include things like DPI or SID if applicable
        """
        img_kwargs = image_kwargs or {}
        self.image = image.load(filepath, **img_kwargs)
        if normalize:
            self.image.ground()
            self.image.normalize()
        self._angle_override = None
        self._size_override = None
        self._center_override = None
        self._high_contrast_threshold = None
        self._low_contrast_threshold = None
        self._ssd: float = 100
        self.mtf = None

    @classmethod
    def from_demo_image(cls):
        """Instantiate and load the demo image."""
        demo_file = retrieve_demo_file(name=cls._demo_filename)
        return cls(demo_file)

    @classmethod
    def from_url(cls, url: str):
        """
        Parameters
        ----------
        url : str
            The URL to the image.
        """
        image_file = get_url(url)
        return cls(image_file)

    def _preprocess(self):
        pass

    def _check_inversion(self):
        pass

    @property
    def phantom_bbox_size_px(self) -> float:
        """The phantom bounding box size in pixels^2."""
        return self.phantom_bbox_size_mm2 * (self.image.dpmm**2)

    @cached_property
    def phantom_ski_region(self) -> RegionProperties:
        """The skimage region of the phantom outline."""
        regions = self._get_canny_regions()
        # search through all the canny ROIs to see which ones pass the detection conditions
        blobs = []
        for phantom_idx, region in enumerate(regions):
            conditions_met = [
                condition(region, self) for condition in self.detection_conditions
            ]
            if all(conditions_met):
                blobs.append(phantom_idx)

        if not blobs:
            raise ValueError(
                "Unable to find the phantom in the image. Potential solutions: check the SSD was passed correctly, check that the phantom isn't at the edge of the field, check that the phantom is centered along the CAX."
            )

        # take the biggest ROI and call that the phantom outline
        big_roi_idx = np.argsort([regions[phan].area_bbox for phan in blobs])[-1]
        phantom_idx = blobs[big_roi_idx]

        return regions[phantom_idx]

    def analyze(
        self,
        low_contrast_threshold: float = 0.05,
        high_contrast_threshold: float = 0.5,
        invert: bool = False,
        angle_override: Optional[float] = None,
        center_override: Optional[tuple] = None,
        size_override: Optional[float] = None,
        ssd: float = 1000,
        low_contrast_method: Contrast = Contrast.MICHELSON,
        visibility_threshold: float = 100,
    ) -> None:
        """Analyze the phantom using the provided thresholds and settings.

        Parameters
        ----------
        low_contrast_threshold : float
            This is the contrast threshold value which defines any low-contrast ROI as passing or failing.
        high_contrast_threshold : float
            This is the contrast threshold value which defines any high-contrast ROI as passing or failing.
        invert : bool
            Whether to force an inversion of the image. This is useful if pylinac's automatic inversion algorithm fails
            to properly invert the image.
        angle_override : None, float
            A manual override of the angle of the phantom. If None, pylinac will automatically determine the angle. If
            a value is passed, this value will override the automatic detection.

            .. Note::

                0 is pointing from the center toward the right and positive values go counterclockwise.

        center_override : None, 2-element tuple
            A manual override of the center point of the phantom. If None, pylinac will automatically determine the center. If
            a value is passed, this value will override the automatic detection. Format is (x, y)/(col, row).
        size_override : None, float
            A manual override of the relative size of the phantom. This size value is used to scale the positions of
            the ROIs from the center. If None, pylinac will automatically determine the size.
            If a value is passed, this value will override the automatic sizing.

            .. Note::

                 This value is not necessarily the physical size of the phantom. It is an arbitrary value.
        ssd
            The SSD of the phantom itself in mm.
        low_contrast_method
            The equation to use for calculating low contrast.
        visibility_threshold
            The threshold for whether an ROI is "seen".
        """
        self._angle_override = angle_override
        self._center_override = center_override
        self._size_override = size_override
        self._high_contrast_threshold = high_contrast_threshold
        self._low_contrast_threshold = low_contrast_threshold
        self._low_contrast_method = low_contrast_method
        self.visibility_threshold = visibility_threshold
        self._ssd = ssd
        self._check_inversion()
        if invert:
            self.image.invert()
        self._preprocess()
        if self.high_contrast_roi_settings:
            self.high_contrast_rois = self._sample_high_contrast_rois()
            # generate rMTF
            spacings = [
                roi["lp/mm"] for roi in self.high_contrast_roi_settings.values()
            ]
            self.mtf = MTF.from_high_contrast_diskset(
                diskset=self.high_contrast_rois, spacings=spacings
            )
        if self.low_contrast_background_roi_settings:
            (
                self.low_contrast_background_rois,
                self.low_contrast_background_value,
            ) = self._sample_low_contrast_background_rois()
        if self.low_contrast_roi_settings:
            self.low_contrast_rois = self._sample_low_contrast_rois()

    def _sample_low_contrast_rois(self) -> List[LowContrastDiskROI]:
        """Sample the low-contrast sample regions for calculating contrast values."""
        lc_rois = []
        for stng in self.low_contrast_roi_settings.values():
            roi = LowContrastDiskROI(
                self.image,
                self.phantom_angle + stng["angle"],
                self.phantom_radius * stng["roi radius"],
                self.phantom_radius * stng["distance from center"],
                self.phantom_center,
                self._low_contrast_threshold,
                self.low_contrast_background_value,
                contrast_method=self._low_contrast_method,
                visibility_threshold=self.visibility_threshold,
            )
            lc_rois.append(roi)
        return lc_rois

    def _sample_low_contrast_background_rois(
        self,
    ) -> Tuple[List[LowContrastDiskROI], float]:
        """Sample the low-contrast background regions for calculating contrast values."""
        bg_rois = []
        for stng in self.low_contrast_background_roi_settings.values():
            roi = LowContrastDiskROI(
                self.image,
                self.phantom_angle + stng["angle"],
                self.phantom_radius * stng["roi radius"],
                self.phantom_radius * stng["distance from center"],
                self.phantom_center,
                self._low_contrast_threshold,
            )
            bg_rois.append(roi)
        avg_bg = np.mean([roi.pixel_value for roi in bg_rois])
        return bg_rois, avg_bg

    def _sample_high_contrast_rois(self) -> List[HighContrastDiskROI]:
        """Sample the high-contrast line pair regions."""
        hc_rois = []
        for stng in self.high_contrast_roi_settings.values():
            roi = HighContrastDiskROI(
                self.image,
                self.phantom_angle + stng["angle"],
                self.phantom_radius * stng["roi radius"],
                self.phantom_radius * stng["distance from center"],
                self.phantom_center,
                self._high_contrast_threshold,
            )
            hc_rois.append(roi)
        return hc_rois

    def save_analyzed_image(
        self,
        filename: Union[None, str, BinaryIO] = None,
        split_plots: bool = False,
        to_streams: bool = False,
        **kwargs,
    ) -> Optional[Union[Dict[str, BinaryIO], List[str]]]:
        """Save the analyzed image to disk or to stream. Kwargs are passed to plt.savefig()

        Parameters
        ----------
        filename : None, str, stream
            A string representing where to save the file to. If split_plots and to_streams are both true, leave as None as newly-created streams are returned.
        split_plots: bool
            If split_plots is True, multiple files will be created that append a name. E.g. `my_file.png` will become `my_file_image.png`, `my_file_mtf.png`, etc.
            If to_streams is False, a list of new filenames will be returned
        to_streams: bool
            This only matters if split_plots is True. If both of these are true, multiple streams will be created and returned as a dict.
        """
        if filename is None and to_streams is False:
            raise ValueError("Must pass in a filename unless saving to streams.")
        figs, names = self.plot_analyzed_image(
            show=False, split_plots=split_plots, **kwargs
        )
        # remove plot keywords as savefig complains about extra kwargs
        for key in (
            "image",
            "low_contrast",
            "high_contrast",
            "show",
        ):
            kwargs.pop(key, None)
        if not split_plots:
            plt.savefig(filename, **kwargs)
        else:
            # append names to filename if it's file-like
            if not to_streams:
                filenames = []
                f, ext = osp.splitext(filename)
                for name in names:
                    filenames.append(f + "_" + name + ext)
            else:  # it's a stream buffer
                filenames = [io.BytesIO() for _ in names]
            for fig, name in zip(figs, filenames):
                fig.savefig(name, **kwargs)
            if to_streams:
                return {name: stream for name, stream in zip(names, filenames)}
            if split_plots:
                return filenames

    def _get_canny_regions(self) -> List[RegionProperties]:
        """Compute the canny edges of the image and return the connected regions found."""
        # compute the canny edges with very low thresholds (detects nearly everything)
        canny_img = feature.canny(
            self.image.array,
            low_threshold=self.detection_canny_settings["percentiles"][0],
            high_threshold=self.detection_canny_settings["percentiles"][1],
            use_quantiles=True,
            sigma=self.detection_canny_settings["sigma"],
        )

        # label the canny edge regions
        labeled = measure.label(canny_img)
        regions = measure.regionprops(labeled, intensity_image=self.image.array)
        return regions

    def _create_phantom_outline_object(self) -> Tuple[Union[Rectangle, Circle], dict]:
        """Construct the phantom outline object which will be plotted on the image for visual inspection."""
        outline_type = list(self.phantom_outline_object)[0]
        outline_settings = list(self.phantom_outline_object.values())[0]
        settings = {}
        if outline_type == "Rectangle":
            side_a = self.phantom_radius * outline_settings["width ratio"]
            side_b = self.phantom_radius * outline_settings["height ratio"]
            half_hyp = np.sqrt(side_a**2 + side_b**2) / 2
            internal_angle = ia = np.rad2deg(np.arctan(side_b / side_a))
            new_x = self.phantom_center.x + half_hyp * (
                geometry.cos(ia) - geometry.cos(ia + self.phantom_angle)
            )
            new_y = self.phantom_center.y + half_hyp * (
                geometry.sin(ia) - geometry.sin(ia + self.phantom_angle)
            )
            obj = Rectangle(
                width=self.phantom_radius * outline_settings["width ratio"],
                height=self.phantom_radius * outline_settings["height ratio"],
                center=Point(new_x, new_y),
            )
            settings["angle"] = self.phantom_angle
        elif outline_type == "Circle":
            obj = Circle(
                center_point=self.phantom_center,
                radius=self.phantom_radius * outline_settings["radius ratio"],
            )
        else:
            raise ValueError(
                "An outline object was passed but was not a Circle or Rectangle."
            )
        return obj, settings

    def plot_analyzed_image(
        self,
        image: bool = True,
        low_contrast: bool = True,
        high_contrast: bool = True,
        show: bool = True,
        split_plots: bool = False,
        **plt_kwargs: dict,
    ) -> Tuple[List[plt.Figure], List[str]]:
        """Plot the analyzed image.

        Parameters
        ----------
        image : bool
            Show the image.
        low_contrast : bool
            Show the low contrast values plot.
        high_contrast : bool
            Show the high contrast values plot.
        show : bool
            Whether to actually show the image when called.
        split_plots : bool
            Whether to split the resulting image into individual plots. Useful for saving images into individual files.
        plt_kwargs : dict
            Keyword args passed to the plt.figure() method. Allows one to set things like figure size.
        """
        plot_low_contrast = low_contrast and any(self.low_contrast_rois)
        plot_high_contrast = high_contrast and any(self.high_contrast_rois)
        num_plots = sum((image, plot_low_contrast, plot_high_contrast))
        if num_plots < 1:
            warnings.warn(
                "Nothing was plotted because either all parameters were false or there were no actual high/low ROIs"
            )
            return
        # set up axes and make axes iterable
        figs = []
        names = []
        if split_plots:
            axes = []
            for n in range(num_plots):
                fig, axis = plt.subplots(1, **plt_kwargs)
                figs.append(fig)
                axes.append(axis)
        else:
            fig, axes = plt.subplots(1, num_plots, **plt_kwargs)
            fig.subplots_adjust(wspace=0.4)
        if num_plots < 2:
            axes = (axes,)
        axes = iter(axes)

        # plot the marked image
        if image:
            img_ax = next(axes)
            names.append("image")
            self.image.plot(ax=img_ax, show=False)
            img_ax.axis("off")
            img_ax.set_title(f"{self.common_name} Phantom Analysis")

            # plot the outline image
            if self.phantom_outline_object is not None:
                outline_obj, settings = self._create_phantom_outline_object()
                outline_obj.plot2axes(img_ax, edgecolor="b", **settings)
            # plot the low contrast background ROIs
            for roi in self.low_contrast_background_rois:
                roi.plot2axes(img_ax, edgecolor="b")
            # plot the low contrast ROIs
            for roi in self.low_contrast_rois:
                roi.plot2axes(img_ax, edgecolor=roi.plot_color)
            # plot the high-contrast ROIs along w/ pass/fail coloration
            if self.high_contrast_rois:
                for (roi, mtf) in zip(
                    self.high_contrast_rois, self.mtf.norm_mtfs.values()
                ):
                    color = "b" if mtf > self._high_contrast_threshold else "r"
                    roi.plot2axes(img_ax, edgecolor=color)

        # plot the low contrast value graph
        if plot_low_contrast:
            lowcon_ax = next(axes)
            names.append("low_contrast")
            self._plot_lowcontrast_graph(lowcon_ax)

        # plot the high contrast MTF graph
        if plot_high_contrast:
            hicon_ax = next(axes)
            names.append("high_contrast")
            self._plot_highcontrast_graph(hicon_ax)

        plt.tight_layout()
        if show:
            plt.show()
        return figs, names

    def _plot_lowcontrast_graph(self, axes: plt.Axes):
        """Plot the low contrast ROIs to an axes."""
        (line1,) = axes.plot(
            [roi.contrast for roi in self.low_contrast_rois],
            marker="o",
            color="m",
            label="Contrast",
        )
        axes.axhline(self._low_contrast_threshold, color="m")
        axes.grid(True)
        axes.set_title("Low-frequency Contrast")
        axes.set_xlabel("ROI #")
        axes.set_ylabel("Contrast")
        axes2 = axes.twinx()
        (line2,) = axes2.plot(
            [roi.contrast_to_noise for roi in self.low_contrast_rois],
            marker="^",
            label="CNR",
        )
        axes2.set_ylabel("CNR")
        axes.legend(handles=[line1, line2])

    def _plot_highcontrast_graph(self, axes: plt.Axes):
        """Plot the high contrast ROIs to an axes."""
        axes.plot(self.mtf.spacings, list(self.mtf.norm_mtfs.values()), marker="*")
        axes.axhline(self._high_contrast_threshold, color="k")
        axes.grid(True)
        axes.set_title("High-frequency rMTF")
        axes.set_xlabel("Line pairs / mm")
        axes.set_ylabel("relative MTF")

    def results(self) -> str:
        """Return the results of the analysis."""
        text = [f"{self.common_name} results:", f"File: {self.image.truncated_path}"]
        if self.low_contrast_rois:
            text += [
                f"Median Contrast: {np.median([roi.contrast for roi in self.low_contrast_rois]):2.2f}",
                f"Median CNR: {np.median([roi.contrast_to_noise for roi in self.low_contrast_rois]):2.1f}",
                f'# Low contrast ROIs "seen": {sum(roi.passed for roi in self.low_contrast_rois):2.0f} of {len(self.low_contrast_rois)}',
            ]
        if self.high_contrast_rois:
            text += [
                f"MTF 80% (lp/mm): {self.mtf.relative_resolution(80):2.2f}",
                f"MTF 50% (lp/mm): {self.mtf.relative_resolution(50):2.2f}",
                f"MTF 30% (lp/mm): {self.mtf.relative_resolution(30):2.2f}",
            ]
        text = "\n".join(text)
        return text

    def results_data(self, as_dict=False) -> Union[PlanarResult, dict]:
        data = PlanarResult(
            analysis_type=self.common_name,
            median_contrast=np.median([roi.contrast for roi in self.low_contrast_rois]),
            median_cnr=np.median(
                [roi.contrast_to_noise for roi in self.low_contrast_rois]
            ),
            num_contrast_rois_seen=sum(roi.passed for roi in self.low_contrast_rois),
            phantom_center_x_y=(self.phantom_center.x, self.phantom_center.y),
        )

        if self.mtf is not None:
            data.mtf_lp_mm = [
                {p: self.mtf.relative_resolution(p)} for p in (80, 50, 30)
            ]
        if as_dict:
            return dataclasses.asdict(data)
        return data

    def publish_pdf(
        self,
        filename: str,
        notes: str = None,
        open_file: bool = False,
        metadata: Optional[dict] = None,
    ):
        """Publish (print) a PDF containing the analysis, images, and quantitative results.

        Parameters
        ----------
        filename : (str, file-like object}
            The file to write the results to.
        notes : str, list of strings
            Text; if str, prints single line.
            If list of strings, each list item is printed on its own line.
        open_file : bool
            Whether to open the file using the default program after creation.
        metadata : dict
            Extra data to be passed and shown in the PDF. The key and value will be shown with a colon.
            E.g. passing {'Author': 'James', 'Unit': 'TrueBeam'} would result in text in the PDF like:
            --------------
            Author: James
            Unit: TrueBeam
            --------------
        """
        canvas = pdf.PylinacCanvas(
            filename,
            page_title=f"{self.common_name} Phantom Analysis",
            metadata=metadata,
        )

        # write the text/numerical values
        text = self.results()
        canvas.add_text(text=text, location=(1.5, 25), font_size=14)
        if notes is not None:
            canvas.add_text(text="Notes:", location=(1, 5.5), font_size=12)
            canvas.add_text(text=notes, location=(1, 5))

        # plot the image
        data = io.BytesIO()
        self.save_analyzed_image(
            data, image=True, low_contrast=False, high_contrast=False
        )
        canvas.add_image(data, location=(1, 3.5), dimensions=(19, 19))
        # plot the high contrast
        if self.high_contrast_rois:
            canvas.add_new_page()
            data = io.BytesIO()
            self.save_analyzed_image(
                data, image=False, low_contrast=False, high_contrast=True
            )
            canvas.add_image(data, location=(1, 7), dimensions=(19, 19))
        # plot the low contrast
        if self.low_contrast_rois:
            canvas.add_new_page()
            data = io.BytesIO()
            self.save_analyzed_image(
                data, image=False, low_contrast=True, high_contrast=False
            )
            canvas.add_image(data, location=(1, 7), dimensions=(19, 19))

        canvas.finish()
        if open_file:
            webbrowser.open(filename)

    @property
    def phantom_center(self) -> Point:
        return (
            Point(self._center_override)
            if self._center_override is not None
            else self._phantom_center_calc()
        )

    @property
    def phantom_radius(self) -> float:
        return (
            self._size_override
            if self._size_override is not None
            else self._phantom_radius_calc()
        )

    @property
    def phantom_angle(self) -> float:
        return (
            self._angle_override
            if self._angle_override is not None
            else self._phantom_angle_calc()
        )

    def _phantom_center_calc(self):
        return bbox_center(self.phantom_ski_region)

    def _phantom_angle_calc(self):
        pass

    def _phantom_radius_calc(self):
        pass


@dataclass
class LightRadResult(ResultBase):
    """This class should not be called directly. It is returned by the ``results_data()`` method.
    It is a dataclass under the hood and thus comes with all the dunder magic.

    Use the following attributes as normal class attributes."""

    field_size_x_mm: float  #:
    field_size_y_mm: float  #:
    field_epid_offset_x_mm: float  #:
    field_epid_offset_y_mm: float  #:
    field_bb_offset_x_mm: float  #:
    field_bb_offset_y_mm: float  #:


class StandardImagingFC2(ImagePhantomBase):
    common_name = "SI FC-2"
    _demo_filename = "fc2.dcm"
    # these positions are the offset in mm from the center of the image to the nominal position of the BBs
    bb_positions_10x10 = {
        "TL": [-40, -40],
        "BL": [-40, 40],
        "TR": [40, -40],
        "BR": [40, 40],
    }
    bb_positions_15x15 = {
        "TL": [-65, -65],
        "BL": [-65, 65],
        "TR": [65, -65],
        "BR": [65, 65],
    }
    bb_sampling_box_size_mm = 10
    field_strip_width_mm = 5

    @staticmethod
    def run_demo() -> None:
        """Run the Standard Imaging FC-2 phantom analysis demonstration."""
        fc2 = StandardImagingFC2.from_demo_image()
        fc2.analyze()
        fc2.plot_analyzed_image()

    def analyze(self, invert: bool = False, fwxm: int = 50) -> None:
        """Analyze the FC-2 phantom to find the BBs and the open field and compare to each other as well as the EPID.

        Parameters
        ----------
        invert : bool
            Whether to force-invert the image from the auto-detected inversion.
        fwxm : int
            The FWXM value to use to detect the field. For flattened fields, the default of 50 should be fine.
            For FFF fields, consider using a lower value such as 25-30.
        """
        self._check_inversion()
        if invert:
            self.image.invert()
        self.bb_center = self._find_overall_bb_centroid(fwxm=fwxm)
        (
            self.field_center,
            self.field_width_x,
            self.field_width_y,
        ) = self._find_field_info(fwxm=fwxm)
        self.epid_center = self.image.center

    def results(self, as_list: bool = False) -> Union[str, list]:
        """Return the results of the analysis."""
        text = [
            f"{self.common_name} results:",
            f"File: {self.image.truncated_path}",
            f"The detected inplane field size was {self.field_width_y:2.1f}mm",
            f"The detected crossplane field size was {self.field_width_x:2.1f}mm",
            f"The inplane field was {self.field_epid_offset_mm.y:2.1f}mm from the EPID CAX",
            f"The crossplane field was {self.field_epid_offset_mm.x:2.1f}mm from the EPID CAX",
            f"The inplane field was {self.field_bb_offset_mm.y:2.1f}mm from the BB inplane center",
            f"The crossplane field was {self.field_bb_offset_mm.x:2.1f}mm from the BB crossplane center",
        ]
        if as_list:
            return text
        else:
            text = "\n".join(text)
            return text

    @property
    def field_epid_offset_mm(self) -> Vector:
        """Field offset from CAX using vector difference"""
        return (
            self.epid_center.as_vector() - self.field_center.as_vector()
        ) / self.image.dpmm

    @property
    def field_bb_offset_mm(self) -> Vector:
        """Field offset from BB centroid using vector difference"""
        return (
            self.bb_center.as_vector() - self.field_center.as_vector()
        ) / self.image.dpmm

    def results_data(self, as_dict: bool = False) -> Union[LightRadResult, dict]:
        """Return the results as a dict or dataclass"""
        data = LightRadResult(
            field_size_x_mm=self.field_width_x,
            field_size_y_mm=self.field_width_y,
            field_epid_offset_x_mm=self.field_epid_offset_mm.x,
            field_epid_offset_y_mm=self.field_epid_offset_mm.y,
            field_bb_offset_x_mm=self.field_bb_offset_mm.x,
            field_bb_offset_y_mm=self.field_bb_offset_mm.y,
        )
        if as_dict:
            return dataclasses.asdict(data)
        return data

    def _check_inversion(self):
        """Perform a normal corner-check inversion. Since these are always 10x10 or 15x15 fields it seems unlikely the corners will be exposed."""
        self.image.check_inversion()

    def _find_field_info(self, fwxm: int) -> (Point, float, float):
        """Determine the center and field widths of the detected field by sampling a strip through the center of the image in inplane and crossplane"""
        sample_width = self.field_strip_width_mm / 2 * self.image.dpmm
        # sample the strip (nominally 5mm) centered about the image center. Average the strip to reduce noise.
        x_bounds = (
            int(self.image.center.x - sample_width),
            int(self.image.center.x + sample_width),
        )
        y_img = np.mean(self.image[:, x_bounds[0] : x_bounds[1]], 1)
        y_prof = SingleProfile(
            y_img, interpolation=Interpolation.NONE, dpmm=self.image.dpmm
        )
        y_bounds = (
            int(self.image.center.y - sample_width),
            int(self.image.center.y + sample_width),
        )
        x_img = np.mean(self.image[y_bounds[0] : y_bounds[1], :], 0)
        x_prof = SingleProfile(
            x_img, interpolation=Interpolation.NONE, dpmm=self.image.dpmm
        )
        x = x_prof.fwxm_data(x=fwxm)["center index (exact)"]
        y = y_prof.fwxm_data(x=fwxm)["center index (exact)"]
        field_width_x = x_prof.fwxm_data(x=fwxm)["width (exact) mm"]
        field_width_y = y_prof.fwxm_data(x=fwxm)["width (exact) mm"]
        return Point(x=x, y=y), field_width_x, field_width_y

    def _find_overall_bb_centroid(self, fwxm: int) -> Point:
        """Determine the geometric center of the 4 BBs"""
        self.bb_centers = bb_centers = self._detect_bb_centers(fwxm)
        central_x = np.mean([p.x for p in bb_centers.values()])
        central_y = np.mean([p.y for p in bb_centers.values()])
        return Point(x=central_x, y=central_y)

    def _detect_bb_centers(self, fwxm: int) -> dict:
        """Sample a 10x10mm square about each BB to detect it. Adjustable using self.bb_sampling_box_size_mm"""
        bb_positions = {}
        nominal_positions = self._determine_bb_set(fwxm=fwxm)
        dpmm = self.image.dpmm
        sample_box_size = self.bb_sampling_box_size_mm / 2 * dpmm
        # invert the image so that the BB marks increase in intensity
        inverted_img = copy.copy(self.image)
        inverted_img.invert()
        # sample the square, use skimage to find the ROI weighted centroid of the BBs
        for key, position in nominal_positions.items():
            x_bounds = (
                int(self.image.center.x + (position[0] * dpmm) - sample_box_size),
                int(self.image.center.x + (position[0] * dpmm) + sample_box_size),
            )
            y_bounds = (
                int(self.image.center.y + (position[1] * dpmm) - sample_box_size),
                int(self.image.center.y + (position[1] * dpmm) + sample_box_size),
            )
            bb_sample = image.load(
                inverted_img[y_bounds[0] : y_bounds[1], x_bounds[0] : x_bounds[1]]
            )
            bb_sample.ground()
            _, rprops, num_roi = get_regions(
                bb_sample.array, fill_holes=True, clear_borders=False
            )
            if num_roi < 1:
                raise ValueError("Did not find the BB in the expected location")
            center_roi = take_centermost_roi(rprops, bb_sample.array.shape)
            # due to the sloping field values, the centroid is usually a better representation.
            # the weighted centroid will bias towards the center of the field. Even though this isn't technically a problem,
            # I know people will complain it's not aligned with the BB.
            bb_positions[key] = Point(
                x=center_roi.centroid[1] + x_bounds[0],
                y=center_roi.centroid[0] + y_bounds[0],
            )
        return bb_positions

    def _determine_bb_set(self, fwxm: int) -> dict:
        """This finds the approximate field size to determine whether to check for the 10x10 BBs or the 15x15. Returns the BB positions"""
        x_prof = SingleProfile(
            self.image[int(self.image.center.y), :], dpmm=self.image.dpmm
        )
        y_prof = SingleProfile(
            self.image[:, int(self.image.center.x)], dpmm=self.image.dpmm
        )
        x_width = x_prof.fwxm_data(x=fwxm)["width (exact) mm"]
        y_width = y_prof.fwxm_data(x=fwxm)["width (exact) mm"]
        if not np.allclose(x_width, y_width, atol=10):
            raise ValueError(
                f"The detected y and x field sizes were too different from one another. They should be within 1cm from each other. Detected field sizes: x={x_width}, y={y_width}"
            )
        if x_width > 140:
            return self.bb_positions_15x15
        else:
            return self.bb_positions_10x10

    def plot_analyzed_image(
        self, show: bool = True, **kwargs
    ) -> Tuple[List[plt.Figure], List[str]]:
        """Plot the analyzed image.

        Parameters
        ----------
        show : bool
            Whether to actually show the image when called.
        """
        figs = []
        names = []
        fig, axes = plt.subplots(1)
        figs.append(fig)
        names.append("image")
        self.image.plot(ax=axes, show=False, **kwargs)
        axes.axis("off")
        axes.set_title(f"{self.common_name} Phantom Analysis")

        # plot the bb marks
        for name, data in self.bb_centers.items():
            axes.plot(data.x, data.y, "go", label=name)

        # plot the bb center as small lines
        axes.axhline(
            y=self.bb_center.y, color="g", xmin=0.25, xmax=0.75, label="BB Centroid"
        )
        axes.axvline(x=self.bb_center.x, color="g", ymin=0.25, ymax=0.75)

        # plot the epid center as image-sized lines
        axes.axhline(y=self.epid_center.y, color="b", label="EPID Center")
        axes.axvline(x=self.epid_center.x, color="b")

        # plot the field center as field-sized lines
        axes.axhline(
            y=self.field_center.y,
            xmin=0.15,
            xmax=0.85,
            color="red",
            label="Field Center",
        )
        axes.axvline(x=self.field_center.x, ymin=0.15, ymax=0.85, color="red")

        axes.legend()

        if show:
            plt.show()
        return figs, names

    def save_analyzed_image(
        self,
        filename: Union[None, str, BinaryIO] = None,
        to_streams: bool = False,
        **kwargs,
    ) -> Optional[Union[Dict[str, BinaryIO], List[str]]]:
        """Save the analyzed image to disk or to stream. Kwargs are passed to plt.savefig()

        Parameters
        ----------
        filename : None, str, stream
            A string representing where to save the file to. If split_plots and to_streams are both true, leave as None as newly-created streams are returned.
        to_streams: bool
            This only matters if split_plots is True. If both of these are true, multiple streams will be created and returned as a dict.
        """
        if filename is None and to_streams is False:
            raise ValueError("Must pass in a filename unless saving to streams.")
        figs, names = self.plot_analyzed_image(show=False, **kwargs)
        if not to_streams:
            plt.savefig(filename, **kwargs)
        else:
            # append names to filename if it's file-like
            if not to_streams:
                filenames = []
                f, ext = osp.splitext(filename)
                for name in names:
                    filenames.append(f + "_" + name + ext)
            else:  # it's a stream buffer
                filenames = [io.BytesIO() for _ in names]
            for fig, name in zip(figs, filenames):
                fig.savefig(name, **kwargs)
            if to_streams:
                return {name: stream for name, stream in zip(names, filenames)}

    def publish_pdf(
        self,
        filename: str,
        notes: str = None,
        open_file: bool = False,
        metadata: Optional[dict] = None,
    ):
        """Publish (print) a PDF containing the analysis, images, and quantitative results.

        Parameters
        ----------
        filename : (str, file-like object}
            The file to write the results to.
        notes : str, list of strings
            Text; if str, prints single line.
            If list of strings, each list item is printed on its own line.
        open_file : bool
            Whether to open the file using the default program after creation.
        metadata : dict
            Extra data to be passed and shown in the PDF. The key and value will be shown with a colon.
            E.g. passing {'Author': 'James', 'Unit': 'TrueBeam'} would result in text in the PDF like:
            --------------
            Author: James
            Unit: TrueBeam
            --------------
        """
        canvas = pdf.PylinacCanvas(
            filename,
            page_title=f"{self.common_name} Phantom Analysis",
            metadata=metadata,
        )

        # write the text/numerical values
        text = self.results(as_list=True)
        canvas.add_text(text=text, location=(1.5, 25), font_size=14)
        if notes is not None:
            canvas.add_text(text="Notes:", location=(1, 5.5), font_size=12)
            canvas.add_text(text=notes, location=(1, 5))

        data = io.BytesIO()
        self.save_analyzed_image(data)
        canvas.add_image(data, location=(1, 3.5), dimensions=(19, 19))

        canvas.finish()
        if open_file:
            webbrowser.open(filename)


class IMTLRad(StandardImagingFC2):
    """The IMT light/rad phantom: https://www.imtqa.com/products/l-rad"""
    common_name = "IMT L-Rad"
    _demo_filename = "imtlrad.dcm"
    center_only_bb = {"Center": [0, 0]}
    bb_sampling_box_size_mm = 12
    field_strip_width_mm = 5

    def _determine_bb_set(self, fwxm: int) -> dict:
        return self.center_only_bb


class SNCFSQA(StandardImagingFC2):
    """SNC light/rad phantom. See the 'FSQA' phantom and specs: https://www.sunnuclear.com/products/suncheck-machine.

    Unlike other light/rad phantoms, this does not have at least a centered BB. The edge markers are in the penumbra
    and thus detecting them is difficult. We thus detect the one offset marker in the top right of the image.
    This is offset by 4cm in each direction. We can then assume that the phantom center is -4cm from this point,
    creating a 'virtual center' so we have an apples-to-apples comparison.
    """
    common_name = "SNC FSQA"
    _demo_filename = "FSQA_15x15.dcm"
    center_only_bb = {"TR": [40, -40]}
    bb_sampling_box_size_mm = 12
    field_strip_width_mm = 5

    def _determine_bb_set(self, fwxm: int) -> dict:
        return self.center_only_bb

    def _find_overall_bb_centroid(self, fwxm: int) -> Point:
        """Determine the geometric center of the 4 BBs"""
        # detect the upper right BB
        self.bb_centers = self._detect_bb_centers(fwxm)
        # add another virtual bb at the center of the phantom, knowing it's offset by 4cm in each direction
        self.bb_centers['Virtual Center'] = self.bb_centers['TR'] - Point(40*self.image.dpmm, -40*self.image.dpmm)
        return self.bb_centers['Virtual Center']


class LasVegas(ImagePhantomBase):
    _demo_filename = "lasvegas.dcm"
    common_name = "Las Vegas"
    phantom_bbox_size_mm2 = 20260
    detection_conditions = [is_centered, is_right_size]
    phantom_outline_object = {"Rectangle": {"width ratio": 0.62, "height ratio": 0.62}}
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.24, "angle": 0, "roi radius": 0.03},
        "roi 2": {"distance from center": 0.24, "angle": 90, "roi radius": 0.03},
        "roi 3": {"distance from center": 0.24, "angle": 180, "roi radius": 0.03},
        "roi 4": {"distance from center": 0.24, "angle": 270, "roi radius": 0.03},
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 0.107, "angle": 0.5, "roi radius": 0.028},
        "roi 2": {"distance from center": 0.141, "angle": 39.5, "roi radius": 0.028},
        "roi 3": {"distance from center": 0.205, "angle": 58, "roi radius": 0.028},
        "roi 4": {"distance from center": 0.179, "angle": -76.5, "roi radius": 0.016},
        "roi 5": {"distance from center": 0.095, "angle": -63.5, "roi radius": 0.016},
        "roi 6": {"distance from center": 0.042, "angle": 0.5, "roi radius": 0.016},
        "roi 7": {"distance from center": 0.097, "angle": 65.5, "roi radius": 0.016},
        "roi 8": {"distance from center": 0.178, "angle": 76.5, "roi radius": 0.016},
        "roi 9": {"distance from center": 0.174, "angle": -97.5, "roi radius": 0.012},
        "roi 10": {"distance from center": 0.088, "angle": -105.5, "roi radius": 0.012},
        "roi 11": {"distance from center": 0.024, "angle": -183.5, "roi radius": 0.012},
        "roi 12": {"distance from center": 0.091, "angle": 105.5, "roi radius": 0.012},
        "roi 13": {"distance from center": 0.179, "angle": 97.5, "roi radius": 0.012},
        "roi 14": {"distance from center": 0.189, "angle": -113.5, "roi radius": 0.007},
        "roi 15": {"distance from center": 0.113, "angle": -131.5, "roi radius": 0.007},
        "roi 16": {
            "distance from center": 0.0745,
            "angle": -181.5,
            "roi radius": 0.007,
        },
        "roi 17": {"distance from center": 0.115, "angle": 130, "roi radius": 0.007},
        "roi 18": {"distance from center": 0.191, "angle": 113, "roi radius": 0.007},
        "roi 19": {
            "distance from center": 0.2085,
            "angle": -124.6,
            "roi radius": 0.003,
        },
        "roi 20": {"distance from center": 0.146, "angle": -144.3, "roi radius": 0.003},
    }

    @staticmethod
    def run_demo():
        """Run the Las Vegas phantom analysis demonstration."""
        lv = LasVegas.from_demo_image()
        lv.analyze()
        lv.plot_analyzed_image()

    def _preprocess(self):
        self._check_direction()

    def _check_inversion(self):
        """Check the inversion by using the histogram of the phantom region"""
        roi = self.phantom_ski_region
        phantom_array = self.image.array[
            roi.bbox[0] : roi.bbox[2], roi.bbox[1] : roi.bbox[3]
        ]
        phantom_sub_image = image.load(phantom_array)
        phantom_sub_image.crop(int(phantom_sub_image.shape[0] * 0.1))
        p5 = np.percentile(phantom_sub_image, 0.5)
        p50 = np.percentile(phantom_sub_image, 50)
        p95 = np.percentile(phantom_sub_image, 99.5)
        dist_to_5 = abs(p50 - p5)
        dist_to_95 = abs(p50 - p95)
        if dist_to_5 > dist_to_95:
            self.image.invert()

    def _check_direction(self) -> None:
        """Check that the phantom is facing the right direction and if not perform a left-right flip of the array."""
        circle = CollapsedCircleProfile(
            self.phantom_center,
            self.phantom_radius * 0.175,
            self.image,
            ccw=False,
            width_ratio=0.16,
            num_profiles=5,
        )
        roll_amount = np.where(circle.values == circle.values.min())[0][0]
        circle.roll(roll_amount)
        circle.filter(size=0.015, kind="median")
        valley_idxs, _ = circle.find_peaks(max_number=2)
        if valley_idxs[0] > valley_idxs[1]:
            self.image.array = np.fliplr(self.image.array)
            self._phantom_ski_region = None

    def _phantom_radius_calc(self) -> float:
        return math.sqrt(self.phantom_ski_region.area_bbox) * 1.626

    def _phantom_angle_calc(self) -> float:
        return 0.0

    def _plot_lowcontrast_graph(self, axes: plt.Axes):
        """Plot the low contrast ROIs to an axes, including visibility"""
        # plot contrast
        (line1,) = axes.plot(
            [roi.contrast for roi in self.low_contrast_rois],
            marker="o",
            color="m",
            label="Contrast",
        )
        axes.axhline(self._low_contrast_threshold, color="m")
        axes.grid(True)
        axes.set_title("Low-frequency Contrast")
        axes.set_xlabel("ROI #")
        axes.set_ylabel("Contrast")
        # plot CNR
        axes2 = axes.twinx()
        axes2.set_ylabel("CNR")
        (line2,) = axes2.plot(
            [roi.contrast_to_noise for roi in self.low_contrast_rois],
            marker="^",
            label="CNR",
        )
        # plot visibility; here's what different from the base method
        axes3 = axes.twinx()
        axes3.set_ylabel("Visibility")
        (line3,) = axes3.plot(
            [roi.visibility for roi in self.low_contrast_rois],
            marker="*",
            color="blue",
            label="Visibility",
        )
        axes3.axhline(self.visibility_threshold, color="blue")
        axes3.spines.right.set_position(("axes", 1.2))

        axes.legend(handles=[line1, line2, line3])

    def results(self) -> str:
        """Return the results of the analysis. Overridden because ROIs seen is based on visibility, not CNR"""
        text = [f"{self.common_name} results:", f"File: {self.image.truncated_path}"]
        text += [
            f"Median Contrast: {np.median([roi.contrast for roi in self.low_contrast_rois]):2.2f}",
            f"Median CNR: {np.median([roi.contrast_to_noise for roi in self.low_contrast_rois]):2.1f}",
            f'# Low contrast ROIs "seen": {sum(roi.passed_visibility for roi in self.low_contrast_rois):2.0f} of {len(self.low_contrast_rois)}',
        ]
        text = "\n".join(text)
        return text

    def results_data(self, as_dict=False) -> Union[PlanarResult, dict]:
        """Overridden because ROIs seen is based on visibility, not CNR"""
        data = PlanarResult(
            analysis_type=self.common_name,
            median_contrast=np.median([roi.contrast for roi in self.low_contrast_rois]),
            median_cnr=np.median(
                [roi.contrast_to_noise for roi in self.low_contrast_rois]
            ),
            num_contrast_rois_seen=sum(
                roi.passed_visibility for roi in self.low_contrast_rois
            ),
            phantom_center_x_y=(self.phantom_center.x, self.phantom_center.y),
        )
        if as_dict:
            return dataclasses.asdict(data)
        return data


class PTWEPIDQC(ImagePhantomBase):
    _demo_filename = "PTW-EPID-QC.dcm"
    common_name = "PTW EPID QC"
    phantom_bbox_size_mm2 = 250**2
    detection_conditions = [is_centered, is_right_size]
    detection_canny_settings = {"sigma": 4, "percentiles": (0.001, 0.01)}
    phantom_outline_object = {"Rectangle": {"width ratio": 8.55, "height ratio": 8.55}}
    high_contrast_roi_settings = {
        # angled rois
        "roi 1": {
            "distance from center": 1.5,
            "angle": -135,
            "roi radius": 0.35,
            "lp/mm": 0.15,
        },
        "roi 2": {
            "distance from center": 3.1,
            "angle": -109,
            "roi radius": 0.35,
            "lp/mm": 0.21,
        },
        "roi 3": {
            "distance from center": 3.4,
            "angle": -60,
            "roi radius": 0.3,
            "lp/mm": 0.27,
        },
        "roi 4": {
            "distance from center": 1.9,
            "angle": -60,
            "roi radius": 0.25,
            "lp/mm": 0.33,
        },
        # vertical rois
        "roi 5": {
            "distance from center": 3.68,
            "angle": -90,
            "roi radius": 0.18,
            "lp/mm": 0.5,
        },
        "roi 6": {
            "distance from center": 2.9,
            "angle": -90,
            "roi radius": 0.08,
            "lp/mm": 2,
        },
        "roi 7": {
            "distance from center": 2.2,
            "angle": -90,
            "roi radius": 0.04,
            "lp/mm": 3,
        },
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 3.87, "angle": 31, "roi radius": 0.3},
        "roi 2": {"distance from center": 3.48, "angle": 17, "roi radius": 0.3},
        "roi 3": {"distance from center": 3.3, "angle": 0, "roi radius": 0.3},
        "roi 4": {"distance from center": 3.48, "angle": -17, "roi radius": 0.3},
        "roi 5": {"distance from center": 3.87, "angle": -31, "roi radius": 0.3},
        "roi 6": {"distance from center": 3.87, "angle": 180 - 31, "roi radius": 0.3},
        "roi 7": {"distance from center": 3.48, "angle": 180 - 17, "roi radius": 0.3},
        "roi 8": {"distance from center": 3.3, "angle": 180, "roi radius": 0.3},
        "roi 9": {"distance from center": 3.48, "angle": 180 + 17, "roi radius": 0.3},
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 3.85, "angle": -148, "roi radius": 0.3},
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Standard Imaging QC-3 phantom analysis demonstration."""
        ptw = PTWEPIDQC.from_demo_image()
        ptw.analyze()
        ptw.plot_analyzed_image()

    def _phantom_radius_calc(self) -> float:
        """The radius of the phantom in pixels; the value itself doesn't matter, it's just
        used for relative distances to ROIs.

        Returns
        -------
        radius : float
        """
        return math.sqrt(self.phantom_ski_region.area_bbox) * 0.116

    def _phantom_angle_calc(self) -> float:
        """The angle of the phantom. This assumes the user has placed the phantom with the high-contrast line pairs at the top
        and low contrast at the bottom at a fixed rotation of 0 degrees.

        Returns
        -------
        angle : float
            The angle in degrees.
        """
        return 0


class StandardImagingQC3(ImagePhantomBase):
    _demo_filename = "qc3.dcm"
    common_name = "SI QC-3"
    phantom_bbox_size_mm2 = 168**2
    detection_conditions = [is_centered, is_right_size]
    phantom_outline_object = {"Rectangle": {"width ratio": 7.5, "height ratio": 6}}
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 2.8,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.1,
        },
        "roi 2": {
            "distance from center": -2.8,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.2,
        },
        "roi 3": {
            "distance from center": 1.45,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.25,
        },
        "roi 4": {
            "distance from center": -1.45,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.45,
        },
        "roi 5": {
            "distance from center": 0,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.76,
        },
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 2, "angle": -90, "roi radius": 0.5},
        "roi 2": {"distance from center": 2.4, "angle": 55, "roi radius": 0.5},
        "roi 3": {"distance from center": 2.4, "angle": -55, "roi radius": 0.5},
        "roi 4": {"distance from center": 2.4, "angle": 128, "roi radius": 0.5},
        "roi 5": {"distance from center": 2.4, "angle": -128, "roi radius": 0.5},
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 2, "angle": 90, "roi radius": 0.5},
    }

    @classmethod
    def from_demo_image(cls):
        """Instantiate and load the demo image."""
        demo_file = retrieve_demo_file(name=cls._demo_filename)
        inst = cls(demo_file)
        inst.image.invert()
        return inst

    @staticmethod
    def run_demo() -> None:
        """Run the Standard Imaging QC-3 phantom analysis demonstration."""
        qc3 = StandardImagingQC3.from_demo_image()
        qc3.analyze()
        qc3.plot_analyzed_image()

    def _phantom_radius_calc(self) -> float:
        """The radius of the phantom in pixels; the value itself doesn't matter, it's just
        used for relative distances to ROIs.

        Returns
        -------
        radius : float
        """
        return math.sqrt(self.phantom_ski_region.area_bbox) * 0.0896

    @lru_cache()
    def _phantom_angle_calc(self) -> float:
        """The angle of the phantom. This assumes the user is using the stand that comes with the phantom,
        which angles the phantom at 45 degrees.

        Returns
        -------
        angle : float
            The angle in degrees.
        """
        angle = np.degrees(self.phantom_ski_region.orientation)
        if np.isclose(angle, 45, atol=5):
            return 45
        elif np.isclose(angle, -45, atol=5):
            return -45
        else:
            raise ValueError(
                "The phantom angle was not near +/-45 degrees. Please adjust the phantom."
            )


class StandardImagingQCkV(StandardImagingQC3):
    _demo_filename = "SI-QC-kV.dcm"
    common_name = "SI QC-kV"
    phantom_bbox_size_mm2 = 142**2
    detection_conditions = [is_centered, is_right_size]
    phantom_outline_object = {"Rectangle": {"width ratio": 7.8, "height ratio": 6.4}}
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 2.8,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.66,
        },
        "roi 2": {
            "distance from center": -2.8,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 0.98,
        },
        "roi 3": {
            "distance from center": 1.45,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 1.50,
        },
        "roi 4": {
            "distance from center": -1.45,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 2.00,
        },
        "roi 5": {
            "distance from center": 0,
            "angle": 0,
            "roi radius": 0.5,
            "lp/mm": 2.46,
        },
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 2, "angle": -90, "roi radius": 0.5},
        "roi 2": {"distance from center": 2.4, "angle": 55, "roi radius": 0.5},
        "roi 3": {"distance from center": 2.4, "angle": -55, "roi radius": 0.5},
        "roi 4": {"distance from center": 2.4, "angle": 128, "roi radius": 0.5},
        "roi 5": {"distance from center": 2.4, "angle": -128, "roi radius": 0.5},
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 2, "angle": 90, "roi radius": 0.5},
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Standard Imaging QC-3 phantom analysis demonstration."""
        qc3 = StandardImagingQCkV.from_demo_image()
        qc3.analyze()
        qc3.plot_analyzed_image()

    def _phantom_radius_calc(self) -> float:
        """The radius of the phantom in pixels; the value itself doesn't matter, it's just
        used for relative distances to ROIs.

        Returns
        -------
        radius : float
        """
        return math.sqrt(self.phantom_ski_region.area_bbox) * 0.0989


class SNCkV(ImagePhantomBase):
    _demo_filename = "SNC-kV.dcm"
    common_name = "SNC kV-QA"
    phantom_bbox_size_mm2 = 134**2
    detection_conditions = [is_centered, is_right_size]
    phantom_outline_object = {"Rectangle": {"width ratio": 7.7, "height ratio": 5.6}}
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 1.8,
            "angle": 0,
            "roi radius": 0.7,
            "lp/mm": 0.6,
        },
        "roi 2": {
            "distance from center": -1.8,
            "angle": 90,
            "roi radius": 0.7,
            "lp/mm": 1.2,
        },
        "roi 3": {
            "distance from center": -1.8,
            "angle": 0,
            "roi radius": 0.7,
            "lp/mm": 1.8,
        },
        "roi 4": {
            "distance from center": 1.8,
            "angle": 90,
            "roi radius": 0.7,
            "lp/mm": 2.4,
        },
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 2.6, "angle": -45, "roi radius": 0.6},
        "roi 2": {"distance from center": 2.6, "angle": -135, "roi radius": 0.6},
        "roi 3": {"distance from center": 2.6, "angle": 45, "roi radius": 0.6},
        "roi 4": {"distance from center": 2.6, "angle": 135, "roi radius": 0.6},
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.5, "angle": 90, "roi radius": 0.25},
        "roi 2": {"distance from center": 0.5, "angle": -90, "roi radius": 0.25},
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Sun Nuclear kV-QA phantom analysis demonstration."""
        snc = SNCkV.from_demo_image()
        snc.analyze()
        snc.plot_analyzed_image()

    def _phantom_radius_calc(self) -> float:
        """The radius of the phantom in pixels; the value itself doesn't matter, it's just
        used for relative distances to ROIs.

        Returns
        -------
        radius : float
        """
        return math.sqrt(self.phantom_ski_region.area_bbox) * 0.1071

    def _phantom_angle_calc(self) -> float:
        """The angle of the phantom. This assumes the user is using the stand that comes with the phantom,
        which angles the phantom at 45 degrees.

        Returns
        -------
        angle : float
            The angle in degrees.
        """
        return 135


class SNCMV(SNCkV):
    _demo_filename = "SNC-MV.dcm"
    common_name = "SNC MV-QA"
    phantom_bbox_size_mm2 = 118**2
    detection_conditions = [is_centered, is_right_size]
    phantom_outline_object = {"Rectangle": {"width ratio": 7.5, "height ratio": 7.5}}
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": -2.3,
            "angle": 0,
            "roi radius": 0.8,
            "lp/mm": 0.1,
        },
        "roi 2": {
            "distance from center": 2.3,
            "angle": 90,
            "roi radius": 0.8,
            "lp/mm": 0.2,
        },
        "roi 3": {
            "distance from center": 2.3,
            "angle": 0,
            "roi radius": 0.8,
            "lp/mm": 0.5,
        },
        "roi 4": {
            "distance from center": -2.3,
            "angle": 90,
            "roi radius": 0.8,
            "lp/mm": 1.0,
        },
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 3.4, "angle": -45, "roi radius": 0.7},
        "roi 2": {"distance from center": 3.4, "angle": 45, "roi radius": 0.7},
        "roi 3": {"distance from center": 3.4, "angle": 135, "roi radius": 0.7},
        "roi 4": {"distance from center": 3.4, "angle": -135, "roi radius": 0.7},
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.7, "angle": 0, "roi radius": 0.2},
        "roi 2": {"distance from center": -0.7, "angle": 0, "roi radius": 0.2},
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Sun Nuclear MV-QA phantom analysis demonstration."""
        snc = SNCMV.from_demo_image()
        snc.analyze()
        snc.plot_analyzed_image()

    def _phantom_angle_calc(self) -> float:
        """The angle of the phantom. This assumes the user is using the stand that comes with the phantom,
        which angles the phantom at 45 degrees.

        Returns
        -------
        angle : float
            The angle in degrees.
        """
        return 45


class LeedsTOR(ImagePhantomBase):
    _demo_filename = "leeds.dcm"
    common_name = "Leeds"
    phantom_bbox_size_mm2 = 148**2
    _is_ccw = False
    phantom_outline_object = {"Circle": {"radius ratio": 0.97}}
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 0.3,
            "angle": 54.8,
            "roi radius": 0.04,
            "lp/mm": 0.5,
        },
        "roi 2": {
            "distance from center": 0.187,
            "angle": 25.1,
            "roi radius": 0.04,
            "lp/mm": 0.56,
        },
        "roi 3": {
            "distance from center": 0.187,
            "angle": -27.5,
            "roi radius": 0.04,
            "lp/mm": 0.63,
        },
        "roi 4": {
            "distance from center": 0.252,
            "angle": 79.7,
            "roi radius": 0.03,
            "lp/mm": 0.71,
        },
        "roi 5": {
            "distance from center": 0.092,
            "angle": 63.4,
            "roi radius": 0.03,
            "lp/mm": 0.8,
        },
        "roi 6": {
            "distance from center": 0.094,
            "angle": -65,
            "roi radius": 0.02,
            "lp/mm": 0.9,
        },
        "roi 7": {
            "distance from center": 0.252,
            "angle": -263,
            "roi radius": 0.02,
            "lp/mm": 1.0,
        },
        "roi 8": {
            "distance from center": 0.094,
            "angle": -246,
            "roi radius": 0.018,
            "lp/mm": 1.12,
        },
        "roi 9": {
            "distance from center": 0.0958,
            "angle": -117,
            "roi radius": 0.018,
            "lp/mm": 1.25,
        },
        "roi 10": {
            "distance from center": 0.27,
            "angle": 112.5,
            "roi radius": 0.015,
            "lp/mm": 1.4,
        },
        "roi 11": {
            "distance from center": 0.13,
            "angle": 145,
            "roi radius": 0.015,
            "lp/mm": 1.6,
        },
        "roi 12": {
            "distance from center": 0.135,
            "angle": -142,
            "roi radius": 0.011,
            "lp/mm": 1.8,
        },
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.65, "angle": 30, "roi radius": 0.025},
        "roi 2": {"distance from center": 0.65, "angle": 120, "roi radius": 0.025},
        "roi 3": {"distance from center": 0.65, "angle": 210, "roi radius": 0.025},
        "roi 4": {"distance from center": 0.65, "angle": 300, "roi radius": 0.025},
    }
    low_contrast_roi_settings = {
        # set 1
        "roi 1": {"distance from center": 0.785, "angle": 30, "roi radius": 0.025},
        "roi 2": {"distance from center": 0.785, "angle": 45, "roi radius": 0.025},
        "roi 3": {"distance from center": 0.785, "angle": 60, "roi radius": 0.025},
        "roi 4": {"distance from center": 0.785, "angle": 75, "roi radius": 0.025},
        "roi 5": {"distance from center": 0.785, "angle": 90, "roi radius": 0.025},
        "roi 6": {"distance from center": 0.785, "angle": 105, "roi radius": 0.025},
        "roi 7": {"distance from center": 0.785, "angle": 120, "roi radius": 0.025},
        "roi 8": {"distance from center": 0.785, "angle": 135, "roi radius": 0.025},
        "roi 9": {"distance from center": 0.785, "angle": 150, "roi radius": 0.025},
        # set 2
        "roi 10": {"distance from center": 0.785, "angle": 210, "roi radius": 0.025},
        "roi 11": {"distance from center": 0.785, "angle": 225, "roi radius": 0.025},
        "roi 12": {"distance from center": 0.785, "angle": 240, "roi radius": 0.025},
        "roi 13": {"distance from center": 0.785, "angle": 255, "roi radius": 0.025},
        "roi 14": {"distance from center": 0.785, "angle": 270, "roi radius": 0.025},
        "roi 15": {"distance from center": 0.785, "angle": 285, "roi radius": 0.025},
        "roi 16": {"distance from center": 0.785, "angle": 300, "roi radius": 0.025},
        "roi 17": {"distance from center": 0.785, "angle": 315, "roi radius": 0.025},
        "roi 18": {"distance from center": 0.785, "angle": 330, "roi radius": 0.025},
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Leeds TOR phantom analysis demonstration."""
        leeds = LeedsTOR.from_demo_image()
        leeds.analyze()
        leeds.plot_analyzed_image()

    @lru_cache()
    def _phantom_angle_calc(self) -> float:
        """Determine the angle of the phantom.

        This is done by searching for square-like boxes of the canny image. There are usually two: one lead and
        one copper. The box with the highest intensity (lead) is identified. The angle from the center of the lead
        square bounding box and the phantom center determines the phantom angle.

        Returns
        -------
        angle : float
            The angle in degrees
        """
        start_angle_deg = self._determine_start_angle_for_circle_profile()
        circle = self._circle_profile_for_phantom_angle(start_angle_deg, is_ccw=True)
        peak_idx, _ = circle.find_fwxm_peaks(threshold=0.6, max_number=1)

        shift_percent = peak_idx[0] / len(circle.values)
        shift_radians = shift_percent * 2 * np.pi
        shift_radians_corrected = 2 * np.pi - shift_radians

        angle = np.degrees(shift_radians_corrected) + start_angle_deg
        return angle

    def _phantom_radius_calc(self) -> float:
        """Determine the radius of the phantom.

        The radius is determined by finding the largest of the detected blobs of the canny image and taking
        its major axis length.

        Returns
        -------
        radius : float
            The radius of the phantom in pixels. The actual value is not important; it is used for scaling the
            distances to the low and high contrast ROIs.
        """
        return math.sqrt(self.phantom_ski_region.area_bbox) * 0.515

    def _determine_start_angle_for_circle_profile(self) -> float:
        """Determine an appropriate angle for starting the circular profile
        used to determine the phantom angle.

        In most cases we can just use 0 degs but for the case where the phantom
        is set up near 0 degs, the peak of the circular profile will be split
        between the left and right sides of the profile.  We can check for this
        case by looking at a few of the peak indexes and determining whether
        they are all on the left or right side of the profile or split left and
        right.  If they're split left and right, then we we need to use a
        different circular profile start angle  to get an accurate angle
        determination

        Returns
        -------
        start_angle_deg: float
            The start angle to be used for the circular profile used to determine the
            phantom rotation.
        """

        circle = self._circle_profile_for_phantom_angle(0)
        peak_idxs, _ = circle.find_fwxm_peaks(threshold=0.6, max_number=4)
        on_left_half = [x < len(circle.values) / 2 for x in peak_idxs]
        aligned_to_zero_deg = not (all(on_left_half) or not any(on_left_half))
        return 90 if aligned_to_zero_deg else 0

    def _preprocess(self) -> None:
        self._check_if_counter_clockwise()

    def _check_if_counter_clockwise(self) -> None:
        """Determine if the low-contrast bubbles go from high to low clockwise or counter-clockwise."""
        circle = self._circle_profile_for_phantom_angle(0)
        peak_idx, _ = circle.find_fwxm_peaks(threshold=0.6, max_number=1)
        circle.values = np.roll(circle.values, -peak_idx[0])
        _, first_set = circle.find_peaks(
            search_region=(0.05, 0.45), threshold=0, min_distance=0.025, max_number=9
        )
        _, second_set = circle.find_peaks(
            search_region=(0.55, 0.95), threshold=0, min_distance=0.025, max_number=9
        )
        self._is_ccw = max(first_set) > max(second_set)
        if not self._is_ccw:
            self.image.fliplr()
            del (
                self.phantom_ski_region
            )  # clear the property to calculate it again since we flipped it

    def _circle_profile_for_phantom_angle(
        self, start_angle_deg: float, is_ccw: bool = False
    ) -> CollapsedCircleProfile:
        """Create a circular profile centered at phantom origin

        Parameters
        ----------
        start_angle_deg: float

            Angle in degrees at which to start the profile
        Returns
        -------
        circle : CollapsedCircleProfile
            The circular profile centered on the phantom center and origin set to the given start angle.
        """
        circle = CollapsedCircleProfile(
            self.phantom_center,
            self.phantom_radius * 0.79,
            self.image.array,
            width_ratio=0.04,
            ccw=is_ccw,
            start_angle=np.deg2rad(start_angle_deg),
        )
        circle.ground()
        circle.filter(size=0.01)
        circle.invert()
        return circle


class LeedsTORBlue(LeedsTOR):
    """The Leeds TOR (Blue) is for analyzing older Leeds phantoms which have slightly offset ROIs compared to the newer, red-ring variant."""
    common_name = "Leeds (Blue)"
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 0.3,
            "angle": 54.8,
            "roi radius": 0.04,
            "lp/mm": 0.5,
        },
        "roi 2": {
            "distance from center": 0.187,
            "angle": 25.1,
            "roi radius": 0.04,
            "lp/mm": 0.56,
        },
        "roi 3": {
            "distance from center": 0.187,
            "angle": -27.5,
            "roi radius": 0.04,
            "lp/mm": 0.63,
        },
        "roi 4": {
            "distance from center": 0.252,
            "angle": 79.7,
            "roi radius": 0.03,
            "lp/mm": 0.71,
        },
        "roi 5": {
            "distance from center": 0.092,
            "angle": 63.4,
            "roi radius": 0.03,
            "lp/mm": 0.8,
        },
        "roi 6": {
            "distance from center": 0.094,
            "angle": -65,
            "roi radius": 0.02,
            "lp/mm": 0.9,
        },
        "roi 7": {
            "distance from center": 0.252,
            "angle": -260,
            "roi radius": 0.02,
            "lp/mm": 1.0,
        },
        "roi 8": {
            "distance from center": 0.094,
            "angle": -240,
            "roi radius": 0.018,
            "lp/mm": 1.12,
        },
        "roi 9": {
            "distance from center": 0.0958,
            "angle": -120,
            "roi radius": 0.018,
            "lp/mm": 1.25,
        },
        "roi 10": {
            "distance from center": 0.27,
            "angle": 115,
            "roi radius": 0.015,
            "lp/mm": 1.4,
        },
        "roi 11": {
            "distance from center": 0.13,
            "angle": 150,
            "roi radius": 0.011,
            "lp/mm": 1.6,
        },
        "roi 12": {
            "distance from center": 0.135,
            "angle": -150,
            "roi radius": 0.011,
            "lp/mm": 1.8,
        },
    }
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.6, "angle": 30, "roi radius": 0.025},
        "roi 2": {"distance from center": 0.6, "angle": 120, "roi radius": 0.025},
        "roi 3": {"distance from center": 0.6, "angle": 210, "roi radius": 0.025},
        "roi 4": {"distance from center": 0.6, "angle": 300, "roi radius": 0.025},
    }
    low_contrast_roi_settings = {
        # set 1
        "roi 1": {"distance from center": 0.83, "angle": 30, "roi radius": 0.025},
        "roi 2": {"distance from center": 0.83, "angle": 45, "roi radius": 0.025},
        "roi 3": {"distance from center": 0.83, "angle": 60, "roi radius": 0.025},
        "roi 4": {"distance from center": 0.83, "angle": 75, "roi radius": 0.025},
        "roi 5": {"distance from center": 0.83, "angle": 90, "roi radius": 0.025},
        "roi 6": {"distance from center": 0.83, "angle": 105, "roi radius": 0.025},
        "roi 7": {"distance from center": 0.83, "angle": 120, "roi radius": 0.025},
        "roi 8": {"distance from center": 0.83, "angle": 135, "roi radius": 0.025},
        "roi 9": {"distance from center": 0.83, "angle": 150, "roi radius": 0.025},
        # set 2
        "roi 10": {"distance from center": 0.83, "angle": 210, "roi radius": 0.025},
        "roi 11": {"distance from center": 0.83, "angle": 225, "roi radius": 0.025},
        "roi 12": {"distance from center": 0.83, "angle": 240, "roi radius": 0.025},
        "roi 13": {"distance from center": 0.83, "angle": 255, "roi radius": 0.025},
        "roi 14": {"distance from center": 0.83, "angle": 270, "roi radius": 0.025},
        "roi 15": {"distance from center": 0.83, "angle": 285, "roi radius": 0.025},
        "roi 16": {"distance from center": 0.83, "angle": 300, "roi radius": 0.025},
        "roi 17": {"distance from center": 0.83, "angle": 315, "roi radius": 0.025},
        "roi 18": {"distance from center": 0.83, "angle": 330, "roi radius": 0.025},
    }

    @classmethod
    def from_demo_image(cls):
        raise NotImplementedError("There is no demo file for this analysis")


class DoselabMC2kV(ImagePhantomBase):
    common_name = "Doselab MC2 kV"
    _demo_filename = "Doselab_kV.dcm"
    phantom_bbox_size_mm2 = 26300
    detection_conditions = [is_right_size]
    phantom_outline_object = {"Rectangle": {"width ratio": 0.55, "height ratio": 0.63}}
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.27, "angle": 48.5, "roi radius": 0.025},
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 0.27, "angle": -48.5, "roi radius": 0.025},
        "roi 2": {"distance from center": 0.225, "angle": -65, "roi radius": 0.025},
        "roi 3": {"distance from center": 0.205, "angle": -88.5, "roi radius": 0.025},
        "roi 4": {"distance from center": 0.22, "angle": -110, "roi radius": 0.025},
        "roi 5": {"distance from center": 0.22, "angle": 110, "roi radius": 0.025},
        "roi 6": {"distance from center": 0.205, "angle": 88.5, "roi radius": 0.025},
        "roi 7": {"distance from center": 0.225, "angle": 65, "roi radius": 0.025},
    }
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 0.17,
            "angle": -20,
            "roi radius": 0.013,
            "lp/mm": 0.6,
        },
        "roi 2": {
            "distance from center": 0.16,
            "angle": -2,
            "roi radius": 0.007,
            "lp/mm": 1.2,
        },
        "roi 3": {
            "distance from center": 0.164,
            "angle": 12.8,
            "roi radius": 0.005,
            "lp/mm": 1.8,
        },
        "roi 4": {
            "distance from center": 0.175,
            "angle": 24.7,
            "roi radius": 0.0035,
            "lp/mm": 2.4,
        },
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Doselab MC2 kV-area phantom analysis demonstration."""
        leeds = DoselabMC2kV.from_demo_image()
        leeds.analyze()
        leeds.plot_analyzed_image()

    def _phantom_radius_calc(self) -> float:
        return math.sqrt(self.phantom_ski_region.area_bbox) * 1.214

    def _phantom_angle_calc(self) -> float:
        roi = self.phantom_ski_region
        angle = np.degrees(roi.orientation) + 90
        if not np.isclose(angle, 45, atol=5):
            raise ValueError(
                "Angles not close enough to the ideal 45 degrees. Check phantom setup or override angle."
            )
        return angle


class DoselabMC2MV(DoselabMC2kV):
    common_name = "Doselab MC2 MV"
    _demo_filename = "Doselab_MV.dcm"
    low_contrast_background_roi_settings = {
        "roi 1": {"distance from center": 0.27, "angle": 48.5, "roi radius": 0.025},
    }
    low_contrast_roi_settings = {
        "roi 1": {"distance from center": 0.27, "angle": -48.5, "roi radius": 0.025},
        "roi 2": {"distance from center": 0.225, "angle": -65, "roi radius": 0.025},
        "roi 3": {"distance from center": 0.205, "angle": -88.5, "roi radius": 0.025},
        "roi 4": {"distance from center": 0.22, "angle": -110, "roi radius": 0.025},
        "roi 5": {"distance from center": 0.22, "angle": 110, "roi radius": 0.025},
        "roi 6": {"distance from center": 0.205, "angle": 88.5, "roi radius": 0.025},
        "roi 7": {"distance from center": 0.225, "angle": 65, "roi radius": 0.025},
    }
    high_contrast_roi_settings = {
        "roi 1": {
            "distance from center": 0.23,
            "angle": -135.3,
            "roi radius": 0.012,
            "lp/mm": 0.1,
        },
        "roi 2": {
            "distance from center": 0.173,
            "angle": 161,
            "roi radius": 0.012,
            "lp/mm": 0.2,
        },
        "roi 3": {
            "distance from center": 0.237,
            "angle": 133,
            "roi radius": 0.012,
            "lp/mm": 0.4,
        },
        "roi 4": {
            "distance from center": 0.298,
            "angle": 122.9,
            "roi radius": 0.01,
            "lp/mm": 0.8,
        },
    }

    @staticmethod
    def run_demo() -> None:
        """Run the Doselab MC2 MV-area phantom analysis demonstration."""
        leeds = DoselabMC2MV.from_demo_image()
        leeds.analyze()
        leeds.plot_analyzed_image()


def take_centermost_roi(rprops: List[RegionProperties], image_shape: Tuple[int, int]):
    """Return the ROI that is closest to the center."""
    larger_rois = [
        rprop for rprop in rprops if rprop.area > 20 and rprop.eccentricity < 0.9
    ]  # drop stray pixel ROIs and line-like ROIs
    center_roi = sorted(
        larger_rois,
        key=lambda p: abs(p.centroid[0] - image_shape[0] / 2)
        + abs(p.centroid[1] - image_shape[1] / 2),
    )[0]
    return center_roi
