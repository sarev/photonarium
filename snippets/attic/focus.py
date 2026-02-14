#!/usr/bin/env python3

import cv2


def focus_measure_laplacian(fname: str) -> float:
    """
    Compute a focus metric for an image using the variance of the Laplacian.

    The image is read with OpenCV, converted to greyscale, and the Laplacian is
    computed. The variance of that Laplacian is returned as a sharpness score.

    Args:
        - fname: Path to the image file.

    Returns:
        float: Variance of the Laplacian; higher values generally indicate a sharper image.

    Notes:
        - If the file cannot be read, OpenCV may raise an error or return None for the image.
        - Input may be a string or any os.PathLike.
    """

    img = cv2.imread(fname)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return lap.var()
