"""Consumption models of the GRS: what a station should have let through,
given the weather — and where the metering disagrees.

The series modelled is `inflow − non-household consumption`: what is left for
households, plus losses. It comes from two 1С exports an operator uploads
(the daily balance by GRS and the daily consumption by category), not from
the line archives, because only 1С has the non-household side.

    parse.py   the two exports → rows
    core.py    cleaning, the weather model, the detector — pure pandas/numpy
"""
