"""I/O helper functions for pylinac."""
import os
import os.path as osp
import struct
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, List, Tuple, Union, BinaryIO, Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlretrieve, urlopen

import numpy as np
import pydicom
from tqdm import tqdm

from .profile import SingleProfile


def is_dicom(file: Union[str, Path]) -> bool:
    """Boolean specifying if file is a proper DICOM file.

    This function is a pared down version of read_preamble meant for a fast return.
    The file is read for a proper preamble ('DICM'), returning True if so,
    and False otherwise. This is a conservative approach.

    Parameters
    ----------
    file : str
        The path to the file.

    See Also
    --------
    pydicom.filereader.read_preamble
    pydicom.filereader.read_partial
    """
    with open(file, "rb") as fp:
        fp.read(0x80)
        prefix = fp.read(4)
        return prefix == b"DICM"


def is_dicom_image(file: Union[str, Path, BinaryIO]) -> bool:
    """Boolean specifying if file is a proper DICOM file with a image

    Parameters
    ----------
    file : str
        The path to the file.

    See Also
    --------
    pydicom.filereader.read_preamble
    pydicom.filereader.read_partial
    """
    result = False
    try:
        img = pydicom.dcmread(file, force=True)
        if "TransferSyntaxUID" not in img.file_meta:
            img.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
        img.pixel_array
        result = True
    except (AttributeError, TypeError, KeyError, struct.error):
        pass
    return result


def retrieve_dicom_file(file: Union[str, Path, BinaryIO]) -> pydicom.FileDataset:
    """Read and return the DICOM dataset.

    Parameters
    ----------
    file : str
        The path to the file.
    """
    ds = pydicom.dcmread(file, force=True)
    if "TransferSyntaxUID" not in ds.file_meta:
        ds.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
    return ds


class TemporaryZipDirectory(TemporaryDirectory):
    """Creates a temporary directory that unpacks a ZIP archive. Shockingly useful"""

    def __init__(self, zfile: Union[str, Path, BinaryIO]):
        """
        Parameters
        ----------
        zfile : str
            String that points to a ZIP archive.
        """
        super().__init__()
        zfiles = zipfile.ZipFile(zfile)
        zfiles.extractall(path=self.name)


def retrieve_filenames(
    directory: Union[str, Path], func: Optional[Callable] = None, recursive: bool = True, **kwargs
) -> List[str]:
    """Retrieve file names in a directory.

    Parameters
    ----------
    directory : str
        The directory to walk over recursively.
    func : function, None
        The function that validates if the file name should be kept.
        If None, no validation will be performed and all file names will be returned.
    recursive : bool
        Whether to search only the root directory.
    kwargs
        Additional arguments passed to the func parameter.
    """
    filenames = []
    if func is None:
        func = lambda x: True
    for pdir, _, files in os.walk(directory):
        for file in files:
            filename = osp.join(pdir, file)
            if func(filename, **kwargs):
                filenames.append(filename)
        if not recursive:
            break
    return filenames


def retrieve_demo_file(name: str, force: bool = False) -> Path:
    """Retrieve the demo file either by getting it from file or from a URL.

    If the file is already on disk it returns the file name. If the file isn't
    on disk, get the file from the URL and put it at the expected demo file location
    on disk for lazy loading next time.

    Parameters
    ----------
    name : str
        The suffix to the url (location within the S3 bucket) pointing to the demo file.
    """
    true_url = r"https://storage.googleapis.com/pylinac_demo_files/" + name
    demo_path = Path(__file__).parent.parent / "demo_files" / name
    # demo_file = osp.join(osp.dirname(osp.dirname(__file__)), "demo_files", name)
    demo_dir = demo_path.parent
    if not demo_dir.exists():
        os.makedirs(demo_dir)
    if force or not demo_path.exists():
        get_url(true_url, destination=demo_path)
    return demo_path


def is_url(url: str) -> bool:
    """Determine whether a given string is a valid URL.

    Parameters
    ----------
    url : str

    Returns
    -------
    bool
    """
    try:
        with urlopen(url) as r:
            return r.status == 200
    except:
        return False


def get_url(url: str, destination: Union[str, Path, None] = None, progress_bar: bool = True) -> str:
    """Download a URL to a local file.

    Parameters
    ----------
    url : str
        The URL to download.
    destination : str, None
        The destination of the file. If None is given the file is saved to a temporary directory.
    progress_bar : bool
        Whether to show a command-line progress bar while downloading.

    Returns
    -------
    filename : str
        The location of the downloaded file.

    Notes
    -----
    Progress bar use/example adapted from tqdm documentation: https://github.com/tqdm/tqdm
    """

    def my_hook(t):
        last_b = [0]

        def inner(b=1, bsize=1, tsize=None):
            if tsize is not None:
                t.total = tsize
            if b > 0:
                t.update((b - last_b[0]) * bsize)
            last_b[0] = b

        return inner

    try:
        if progress_bar:
            with tqdm(
                unit="B", unit_scale=True, miniters=1, desc=url.split("/")[-1]
            ) as t:
                filename, _ = urlretrieve(
                    url, filename=destination, reporthook=my_hook(t)
                )
        else:
            filename, _ = urlretrieve(url, filename=destination)
    except (HTTPError, URLError, ValueError) as e:
        raise e
    return filename


# this is easier with pandas, but I don't want that as a dependency at this point
class SNCProfiler:
    """Load a file from a Sun Nuclear Profiler device. This accepts .prs files."""

    def __init__(
        self,
        path: str,
        detector_row: int = 106,
        bias_row: int = 107,
        calibration_row: int = 108,
        data_row: int = -1,
        data_columns: slice = slice(5, 259),
    ):
        """
        Parameters
        ----------
        path : str
            Path to the .prs file.
        detector_row
        bias_row
        calibration_row
        data_row
        data_columns
            The range of columns that the data is in. Usually, there are some columns before and after the real data.
        """
        with open(path, encoding="cp437") as f:
            raw_data = f.read().splitlines()
            self.detectors = raw_data[detector_row].split("\t")[data_columns]
            self.bias = np.array(raw_data[bias_row].split("\t")[data_columns]).astype(
                float
            )
            self.calibration = np.array(
                raw_data[calibration_row].split("\t")[data_columns]
            ).astype(float)
            self.data = np.array(raw_data[data_row].split("\t")[data_columns]).astype(
                float
            )
            self.timetic = float(raw_data[bias_row].split("\t")[2])
            self.integrated_dose = self.calibration * (
                self.data - self.bias * self.timetic
            )

    def to_profiles(
        self, n_detectors_row: int = 63, **kwargs
    ) -> Tuple[SingleProfile, SingleProfile, SingleProfile, SingleProfile]:
        """Convert the SNC data to SingleProfiles. These can be analyzed directly or passed to other modules like flat/sym.

        Parameters
        ----------
        n_detectors_row : int
            The number of detectors in a given row. Note that they Y profile includes 2 extra detectors from the other 3.
        """
        def copy_cax_dose(array: np.ndarray, center_detector_idx: int = 31) -> np.ndarray:
            array = np.insert(array, center_detector_idx+1, array[center_detector_idx])
            array = np.insert(array, center_detector_idx-1, array[center_detector_idx])
            return array

        y_prof = SingleProfile(
            self.integrated_dose[n_detectors_row : 2 * n_detectors_row + 2], **kwargs
        )
        # for all but the y profile, we are missing detectors to the left and right of center because the center y-detector is too wide
        # for physical spacing purposes we have to fill those values in. we use the central value.
        x_prof = SingleProfile(copy_cax_dose(self.integrated_dose[:n_detectors_row]), **kwargs)
        pos_prof = SingleProfile(copy_cax_dose(
            self.integrated_dose[2 * n_detectors_row + 2 : 3 * n_detectors_row + 2]),
            **kwargs
        )
        neg_prof = SingleProfile(copy_cax_dose(
            self.integrated_dose[3 * n_detectors_row + 2 : 4 * n_detectors_row + 2]),
            **kwargs
        )
        return x_prof, y_prof, pos_prof, neg_prof
