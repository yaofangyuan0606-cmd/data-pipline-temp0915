"""Acquisition from cloud precomputed sources: pick an ROI, judge it before paying for it, then read or materialise it.

Ported from the standalone H01 crawler (benchmarked 2026-09-08). Three of its seven stages belong in a
data-cleaning platform and are here; the render / screenshot / Neuroglancer-export stages do not and are not.

    index     segment_properties -> candidate ROIs        (cheap, a few MB)
    precheck  tissue-type mask   -> is this ROI worth it   (seconds)
    register  ROI -> a platform dataset, read on demand    (no download)
    fetch     ROI -> an image stack in data_root           (download, resumable job)
"""
